from __future__ import annotations
import math
import numpy as np
from .config import Layer7Config
from .models import CandidateUpdate, EvidenceType, ResidualBundle, RouteDecision, TDOCInput, TriggerDecision, AttributionResult
from .utils import clip, project_vector
from .graph import TopologyManager, cycle_attribution
from .reliability import BayesianReliabilityStore


class AdaptationEngine:
    def __init__(self, config: Layer7Config, reliability: BayesianReliabilityStore | None = None):
        self.cfg = config
        self.reliability = reliability or BayesianReliabilityStore(config.knowledge)
        self.topology = TopologyManager(max_edges=config.edge.max_edges, dwell_ticks=config.edge.topology_dwell_ticks)
        self.coupling_var: dict[str, float] = {}

    @staticmethod
    def _field_ok(inp: TDOCInput) -> bool:
        return inp.execution.evidence_type == EvidenceType.FIELD

    def _context_scale(self, inp: TDOCInput) -> tuple[bool, float, dict]:
        h = inp.hints
        present = bool(h.crp_id and h.sarg_id)
        q = clip(h.crp_match_quality, 0.0, 1.0)
        m = clip(h.mrg_confidence, 0.0, 1.0)
        scale = min(q, m)
        ctx = {"crp_id": h.crp_id, "sarg_id": h.sarg_id,
               "crp_match_quality": q, "sarg_deviation": float(h.sarg_deviation),
               "mrg_confidence": m, "mrg": h.mrg}
        return present, scale, ctx

    @staticmethod
    def _bounded_delta(current: float, delta: float, bounds, rate: float) -> float | None:
        if bounds is None:
            return None
        lo, hi = bounds
        return clip(current + rate * float(delta), lo, hi) - current

    def build(self, inp: TDOCInput, r: ResidualBundle, route: RouteDecision,
              trig: TriggerDecision, attr: AttributionResult, execution_dominant: bool = False,
              collect_only: bool = False) -> CandidateUpdate:
        c = CandidateUpdate(); h = inp.hints; rates = self.cfg.rates
        field_ok = self._field_ok(inp)
        context_present, context_scale, context = self._context_scale(inp)
        c.knowledge_context = context

        # Fast bounded state correction (L7-156).  Missing physical bounds => no correction.
        if trig.fast and route.layer2:
            for key, e in r.state_residual.items():
                if key not in h.state_bounds:
                    c.reasons.append(f"state_correction_skipped_missing_bounds:{key}")
                    continue
                gain = clip(h.state_gain.get(key, rates.fast_state), 0.0, rates.fast_state)
                lo, hi = h.state_bounds[key]; current = float(inp.network.std.get(key, 0.0))
                c.delta_x[key] = clip(current + gain * float(e), lo, hi) - current
            if c.delta_x: c.reasons.append("fast_state_correction")

        # Corrected probabilistic output/uncertainty are populated by pipeline after GP inference.

        # Supervisory guidance is transient and bypasses persistent commit (L7-169).
        if trig.slow_boundary and h.mrg:
            c.delta_gamma = {"mrg": h.mrg, "transient": True, "confidence": h.mrg_confidence}
            c.reasons.append("supervisory_mrg")

        # Typed Layer 3 feedback: transforms and scheduler policies remain TDOC-owned.
        if route.layer31:
            c.alignment_update = {
                "DeltaT_unit": h.alignment_unit_delta,
                "DeltaT_frame": h.alignment_frame_delta,
                "Delta_h": h.semantic_map_delta,
                "DeltaSigma_STD": h.shared_uncertainty_delta,
                "DeltaSemanticMap": h.semantic_map_delta,
                "DeltaFreshnessPolicy": h.freshness_policy_delta,
            }
        if route.layer33:
            c.temporal_update = {
                # Canonical Formalism_layer7_v2 names.
                "DeltaDelta_t": h.fast_period_delta,
                "DeltaDelta_t_m": h.meso_period_delta,
                "DeltaDelta_T": h.slow_period_delta,
                "DeltaFreshnessThreshold": h.freshness_threshold_delta,
                "DeltaAggregationPolicy": h.aggregation_policy_delta,
                "DeltaCommitBoundary": h.commit_boundary_delta,
                "DeltaGammaEnvelope": h.gamma_envelope_delta or c.delta_gamma,
                # Backward-compatible aliases.
                "DeltaFastPeriod": h.fast_period_delta,
                "DeltaMesoPeriod": h.meso_period_delta,
                "DeltaSlowPeriod": h.slow_period_delta,
            }

        # Persistent adaptation is a hard FIELD-only invariant. Context conditioning
        # is required by v2 unless explicitly unavailable for a transient-only cycle.
        context_ok = context_present or not self.cfg.knowledge.require_context_for_persistent
        persistent_allowed = bool(trig.slow and not collect_only and field_ok and context_ok)
        if not persistent_allowed:
            if trig.slow and not field_ok: c.reasons.append("persistent_blocked_non_field")
            if trig.slow and field_ok and not context_ok: c.reasons.append("persistent_blocked_missing_CRP_SARG_context")
            return c
        c.persistent = True
        c.reasons.append(f"knowledge_conditioning_scale:{context_scale:.6f}")
        slow_scale = rates.slow_structural * context_scale
        meso_scale = min(rates.meso_relation, max(rates.weight, rates.coupling)) * context_scale

        # L2 typed correction deltas are kept distinct from TEP reliability.
        if route.layer2:
            for key, delta in h.bias_delta.items():
                cur = float(inp.network.metadata.get("bias", {}).get(key, 0.0))
                d = self._bounded_delta(cur, delta, h.bias_bounds.get(key), slow_scale)
                if d is not None: c.delta_b[key] = d
                else: c.reasons.append(f"bias_update_skipped_missing_bounds:{key}")
            base_maps = {
                "uncertainty": inp.network.metadata.get("uncertainty", {}),
                "quality": inp.network.twin_quality,
                "validity": inp.network.twin_validity,
                "reliability": inp.network.twin_reliability,
            }
            for name, source, bounds, target in [
                ("uncertainty", h.uncertainty_delta, h.uncertainty_bounds, c.delta_u),
                ("quality", h.quality_delta, h.quality_bounds, c.delta_q),
                ("validity", h.validity_delta, h.validity_bounds, c.delta_v),
                ("reliability", h.reliability_delta, h.reliability_bounds, c.delta_r),
            ]:
                base_map = base_maps[name]
                for key, delta in source.items():
                    cur = float(base_map.get(key, 0.0))
                    d = self._bounded_delta(cur, delta, bounds.get(key), slow_scale)
                    if d is not None: target[key] = d
                    else: c.reasons.append(f"{name}_update_skipped_missing_bounds:{key}")

        # Gaussian/Kalman parameter update with mandatory validity bounds and slow rate.
        if route.layer2 and not execution_dominant:
            for twin, mean_seq in h.theta_mean.items():
                if twin not in h.theta_bounds:
                    c.reasons.append(f"theta_update_skipped_missing_bounds:{twin}"); continue
                try:
                    m = np.asarray(mean_seq, dtype=float); P = np.asarray(h.theta_cov[twin], dtype=float)
                    H = np.asarray(h.theta_H[twin], dtype=float); R = np.asarray(h.theta_R[twin], dtype=float)
                    e = np.asarray(h.theta_residual[twin], dtype=float)
                    S = H @ P @ H.T + R; Kg = P @ H.T @ np.linalg.pinv(S)
                    posterior = m + Kg @ e
                    # Rate hierarchy applies to the candidate movement, then project.
                    mp = m + slow_scale * (posterior - m)
                    mp = project_vector(mp, h.theta_bounds[twin])
                    c.delta_theta[twin] = (mp - m).tolist()
                except (KeyError, ValueError, np.linalg.LinAlgError):
                    c.reasons.append(f"theta_update_skipped:{twin}")

        # W: FIELD posterior stays separate, while capped simulation prior may
        # influence candidate trust through effective_mean (L7-138/139).
        if route.layer2 or route.layer32:
            eta = {}
            for twin, w in inp.network.weights.items():
                valid = float(inp.network.twin_validity.get(twin, 0.0))
                if valid <= 0.0:
                    c.delta_w[twin] = -float(w)  # hard gate L7-63
                    eta[twin] = 0.0
                    c.reasons.append(f"invalid_twin_weight_zeroed:{twin}")
                    continue
                cell = h.tep_cell.get(twin, "default")
                rel_cell = self.reliability.cell(twin, cell)
                if twin in h.tep_success:
                    rel = self.reliability.preview_summary(twin, cell, h.tep_success[twin], inp.execution.evidence_type)['effective_mean']
                else:
                    rel = rel_cell.effective_mean()
                q = float(inp.network.twin_quality.get(twin, h.source_quality))
                f = float(inp.network.twin_freshness.get(twin, h.source_freshness))
                fault = attr.twin_posterior.get(twin, 0.0)
                eta[twin] = max(0.0, q * valid * f * rel * (1.0 - fault) /
                                (inp.network.uncertainty + 1e-6))
            s = sum(eta.values())
            if s > 0:
                alpha_w = min(rates.weight, rates.meso_relation) * context_scale
                for twin, val in eta.items():
                    if float(inp.network.twin_validity.get(twin, 0.0)) <= 0.0:
                        continue
                    target = val / s; current = float(inp.network.weights.get(twin, 0.0))
                    proposed = (1 - alpha_w) * current + alpha_w * target
                    lo, hi = h.weight_bounds.get(twin, (0.0, 1.0))
                    c.delta_w[twin] = clip(proposed, lo, hi) - current

        # K: scalar Gaussian update; missing explicit physical bound => fail-closed skip.
        if route.layer32 and not execution_dominant:
            alpha_k = min(rates.coupling, rates.meso_relation) * context_scale
            for edge, current in inp.network.couplings.items():
                if edge not in h.coupling_sensitivity: continue
                if edge not in h.coupling_bounds:
                    c.reasons.append(f"coupling_update_skipped_missing_bounds:{edge}"); continue
                phi = float(h.coupling_sensitivity[edge]); R = max(float(h.coupling_variance.get(edge, 1.0)), 1e-9)
                p = max(float(self.coupling_var.get(edge, h.coupling_prior_variance.get(edge, 1.0))), 1e-9)
                resid = float(h.coupling_error.get(edge, r.coupling))
                cyc_map = cycle_attribution(inp.network.topology, inp.network.hidden_loop_lci,
                                            budget=self.cfg.edge.max_cycle_candidates)
                cycle_score = abs(float(cyc_map.get(edge, 0.0)))
                cpl_loss = resid*resid + abs(float(inp.network.coc_cost)) + cycle_score
                c.knowledge_context.setdefault('coupling_loss', {})[edge] = {
                    'residual_sq':resid*resid,'J_C':abs(float(inp.network.coc_cost)),
                    'cycle':cycle_score,'L_cpl':cpl_loss}
                # Edge-friendly scalar Gaussian specialization: use a signed
                # effective innovation whose magnitude reflects formal L_cpl.
                signed = 1.0 if resid >= 0 else -1.0
                effective_resid = signed * math.sqrt(max(cpl_loss, 0.0))
                g = p * phi / (phi * phi * p + R)
                posterior = float(current) + g * (effective_resid - phi * float(current))
                self.coupling_var[edge] = max((1 - g * phi) * p, 1e-9)
                lo, hi = h.coupling_bounds[edge]
                raw_delta = alpha_k * (posterior - float(current))
                max_step = alpha_k * max(1.0, abs(float(current)))
                delta = clip(raw_delta, -max_step, max_step)
                c.delta_k[edge] = clip(float(current) + delta, lo, hi) - float(current)

        # G: bounded SCC/LCI evidence, hysteresis/dwell, global chatter lock.
        if route.layer32 and not execution_dominant:
            cyc = cycle_attribution(inp.network.topology, inp.network.hidden_loop_lci,
                                    budget=self.cfg.edge.max_cycle_candidates)
            scores = {}; keys = set(h.edge_dependency_scores) | set(h.edge_error_scores) | set(h.edge_risk_scores) | set(cyc)
            for edge in keys:
                scores[edge] = (float(h.edge_dependency_scores.get(edge, 0.0)) +
                                float(attr.edge_posterior.get(edge, 0.0)) +
                                float(cyc.get(edge, 0.0)) - float(h.edge_risk_scores.get(edge, 0.0)))
            cand = self.topology.candidate(inp.network.topology, scores)
            if set(cand) != set(inp.network.topology): c.graph_candidate = cand

        # epsilon slow update, mandatory physical/contract bounds.
        if route.layer32:
            q = self.cfg.knowledge.epsilon_quantile
            alpha_eps = min(rates.epsilon, rates.slow_structural) * context_scale
            for edge, hist in h.epsilon_residual_history.items():
                if edge not in h.epsilon_bounds:
                    c.reasons.append(f"epsilon_update_skipped_missing_bounds:{edge}"); continue
                vals = np.abs(np.asarray(list(hist), dtype=float))
                if vals.size == 0: continue
                empirical = float(np.quantile(vals, q)); current = float(inp.network.epsilon.get(edge, empirical))
                proposed = (1 - alpha_eps) * current + alpha_eps * empirical
                lo, hi = h.epsilon_bounds[edge]; c.delta_epsilon[edge] = clip(proposed, lo, hi) - current

            # Omega may only be produced on L3.2 route.
            alpha_omega = min(rates.omega, rates.slow_structural) * context_scale
            for key, delta in h.omega_delta.items():
                if key not in h.omega_bounds:
                    c.reasons.append(f"omega_update_skipped_missing_bounds:{key}"); continue
                current = float(inp.network.omega.get(key, 0.0)); lo, hi = h.omega_bounds[key]
                c.delta_omega[key] = clip(current + alpha_omega * float(delta), lo, hi) - current
            for key, delta in h.lambda_c_delta.items():
                if key not in h.lambda_c_bounds:
                    c.reasons.append(f"lambda_c_update_skipped_missing_bounds:{key}"); continue
                current = float(inp.network.metadata.get("lambda_c", {}).get(key, 0.0)); lo, hi = h.lambda_c_bounds[key]
                c.delta_lambda_c[key] = clip(current + slow_scale * float(delta), lo, hi) - current
            c.delta_cycle_policy = dict(h.cycle_policy_delta)

        # Policy/constraints are bounded supplied deltas only.
        if route.policy:
            alpha_p = min(rates.policy, rates.slow_structural) * context_scale
            alpha_b = min(rates.constraints, rates.slow_structural) * context_scale
            for key, delta in h.policy_delta.items():
                if key not in h.policy_bounds:
                    c.reasons.append(f"policy_update_skipped_missing_bounds:{key}"); continue
                current = float(inp.network.metadata.get("policy", {}).get(key, 0.0)); lo, hi = h.policy_bounds[key]
                c.delta_policy[key] = clip(current + alpha_p * float(delta), lo, hi) - current
            for key, delta in h.constraint_delta.items():
                if key not in h.constraint_bounds:
                    c.reasons.append(f"constraint_update_skipped_missing_bounds:{key}"); continue
                current = float(inp.network.constraints.get(key, 0.0)); lo, hi = h.constraint_bounds[key]
                c.delta_constraints[key] = clip(current + alpha_b * float(delta), lo, hi) - current

        # TEP candidate is FIELD-backed.  Report operational and tempered-prior
        # summaries separately so simulation cannot masquerade as field truth.
        if field_ok:
            for twin, success in h.tep_success.items():
                cell = h.tep_cell.get(twin, "default")
                summary = self.reliability.preview_summary(twin, cell, success, inp.execution.evidence_type)
                c.tep_candidate[twin] = {"cell": cell, "field_backed": True, **summary}
                if route.layer2:
                    current_r = float(inp.network.twin_reliability.get(twin, self.reliability.cell(twin, cell).mean))
                    c.delta_r.setdefault(twin, float(summary['operational_mean']) - current_r)

        if execution_dominant:
            c.delta_theta.clear(); c.delta_k.clear(); c.graph_candidate = []
            c.delta_omega.clear(); c.reasons.append("anti_compensation_freeze_model_K_G_Omega")

        c.structural = bool(c.delta_theta or c.delta_k or c.graph_candidate or c.delta_omega or
                            c.delta_lambda_c or c.delta_cycle_policy or c.delta_policy or c.delta_constraints)
        return c
