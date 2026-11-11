from __future__ import annotations
from collections import deque
from dataclasses import asdict
from .config import Layer7Config
from .models import (
    AdaptationMode, AttributionResult, CommitResult, Layer7Output,
    OfflineErrorPacket, TDOCInput, ValidationResult, CandidateUpdate,
    ResidualBundle, DriftResult,
)
from .evidence import EvidenceGateway
from .residuals import ResidualEngine
from .gp import SparseGPBank
from .attribution import ObjectOrientedBayesAttributor
from .drift import TruncatedBayesianChangePoint
from .routing import RouteEngine
from .triggers import TriggerEngine
from .reliability import BayesianReliabilityStore
from .adaptation import AdaptationEngine
from .safety import AntiCompensationGuard, TEPCoverageGuard, CatastrophicGuard
from .validation import ValidationEngine
from .commit import CommitManager
from .governance import ProvenanceManager
from .kstore import BoundedKStore, SETDKStore
from .compliance import ComplianceEvaluator
from .utils import dataclass_hash, atomic_write_json
import json


class Layer7Pipeline:
    """End-to-end L7-117 pipeline, invoked by TDOC per transaction."""
    def __init__(self, config: Layer7Config | None = None, tdoc_commit_callback=None,
                 tdoc_validation_callback=None, tdoc_rollback_callback=None, state_root=None,
                 native_diagnostic_validator=None):
        self.cfg = config or Layer7Config()
        self.evidence = EvidenceGateway(self.cfg, native_diagnostic_validator)
        self.residuals = ResidualEngine(self.cfg.edge.max_cycle_candidates)
        self.gp = SparseGPBank(self.cfg.gp)
        self.attr = ObjectOrientedBayesAttributor()
        self.drift = TruncatedBayesianChangePoint(self.cfg.drift)
        self.route = RouteEngine(self.cfg); self.triggers = TriggerEngine(self.cfg)
        self.reliability = BayesianReliabilityStore(self.cfg.knowledge)
        self.adapt = AdaptationEngine(self.cfg, self.reliability)
        self.anti = AntiCompensationGuard(self.cfg)
        self.validation = ValidationEngine(self.cfg)
        self.validation_callback = tdoc_validation_callback
        self.commit = CommitManager(state_root, tdoc_commit_callback, tdoc_rollback_callback)
        self.prov = ProvenanceManager()
        self._load_reliability_state()
        self.ss = BoundedKStore.from_environment(self.cfg.edge.kstore_max_records,
                                                 self.cfg.edge.kstore_segment_records,
                                                 self.cfg.edge.kstore_max_segments)
        self.setd = SETDKStore.from_environment(self.cfg.edge.setd_max_packages)
        self.cat = CatastrophicGuard(self.cfg); self.compliance_eval = ComplianceEvaluator()
        self.osc_history: dict[str, deque] = {}
        self.locked = False; self.degraded = False


    def _tep_state_path(self, version: str):
        p = self.commit.root/'tep_versions'/f'{version}.json'
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _save_reliability_state(self, version: str) -> None:
        atomic_write_json(self._tep_state_path(str(version)), self.reliability.export_state())

    def _load_reliability_state(self) -> None:
        ap = self.commit.root/'active_pointer.json'
        if not ap.exists(): return
        try:
            version = str(json.loads(ap.read_text()).get('version'))
            p = self._tep_state_path(version)
            if p.exists(): self.reliability.import_state(json.loads(p.read_text()))
        except Exception:
            return

    def _restore_reliability_state(self, version: str | None) -> None:
        if not version: return
        p = self._tep_state_path(str(version))
        if p.exists():
            try: self.reliability.import_state(json.loads(p.read_text()))
            except Exception: pass

    @staticmethod
    def _feature(r, inp):
        return [r.prediction, r.domain, r.coupling, r.synchronization, r.execution, r.coc, r.lci,
                float(inp.network.margin), float(inp.network.uncertainty)]

    def _feedback(self, inp, residuals, candidate, route, attr, commit):
        vnew = commit.committed_version or commit.candidate_version
        f2 = {}; f31 = {}; f32 = {}; f33 = {}
        if route.layer2:
            # ΔR and TEP are intentionally distinct. Corrected prediction/UQ are
            # exposed as supplemental probabilistic evidence, not actuator commands.
            f2 = {
                'DeltaX': candidate.delta_x, 'DeltaB': candidate.delta_b,
                'DeltaTheta': candidate.delta_theta, 'DeltaU': candidate.delta_u,
                'DeltaQ': candidate.delta_q, 'DeltaV': candidate.delta_v,
                'DeltaR': candidate.delta_r, 'TEP_candidate': candidate.tep_candidate,
                'CorrectedPrediction': candidate.corrected_prediction,
                'CorrectedUncertainty': candidate.corrected_uncertainty,
                # Transaction-matched evidence only.  TDOC/Layer3 may use this
                # to inflate the next prediction tube; it is not a state write,
                # a command, or an operational adaptation by itself.
                'StateResidualEvidence': dict(residuals.state_residual),
                'Reason': candidate.reasons, 'rho_attr': attr.confidence, 'nu_new': vnew,
            }
        if route.layer31:
            f31 = {**candidate.alignment_update, 'Reason': candidate.reasons, 'nu_new': vnew}
        if route.layer32:
            f32 = {
                'DeltaW': candidate.delta_w, 'DeltaK': candidate.delta_k,
                'G_candidate': candidate.graph_candidate, 'DeltaEpsilon': candidate.delta_epsilon,
                'DeltaOmega': candidate.delta_omega, 'DeltaLambda_C': candidate.delta_lambda_c,
                'DeltaCyclePolicy': candidate.delta_cycle_policy,
                'Reason': candidate.reasons, 'nu_new': vnew,
            }
        if route.layer33:
            f33 = {**candidate.temporal_update,
                   'DeltaGammaEnvelope': candidate.temporal_update.get('DeltaGammaEnvelope', candidate.delta_gamma),
                   'Reason': candidate.reasons, 'nu_new': vnew}
        return f2, f31, f32, f33

    def process_offline(self, packet: OfflineErrorPacket) -> dict:
        """Store simulation/reference error and update only the tempered non-field prior."""
        if packet.evidence_type.value not in ('simulation', 'reference'):
            raise ValueError('offline packet must be simulation or reference evidence')
        tep_updates = packet.context.get('tep_updates', []) if isinstance(packet.context, dict) else []
        prior_updates = []
        for u in tep_updates:
            try:
                twin = str(u['twin']); cell = str(u.get('cell', 'default')); success = bool(u['success'])
                self.reliability.observe(twin, cell, success, packet.evidence_type)
                prior_updates.append({'twin': twin, 'cell': cell, **self.reliability.summary(twin, cell)})
            except Exception:
                continue
        rec = {'version': packet.version, 'evidence_type': packet.evidence_type.value,
               'residuals': {'delta_sarg': packet.delta_sarg, 'coc': packet.coc_cost,
                             'constraint_violations': packet.constraint_violations,
                             'simulation_residual': packet.simulation_residual},
               'context': packet.context, 'provenance': packet.provenance,
               'prior_updates': prior_updates, 'operational_commit_allowed': False}
        stored = self.ss.append(rec)
        return {'stored': True, 'record_hash': stored.get('record_hash'), 'operational_commit_allowed': False,
                'evidence_type': packet.evidence_type.value, 'tempered_prior_updates': prior_updates}

    def _apply_oscillation_guard(self, candidate):
        gamma = self.cfg.rates.oscillation_decay; threshold = self.cfg.thresholds.oscillation
        def visit(prefix, mapping):
            for k, v in list(mapping.items()):
                if not isinstance(v, (int, float)): continue
                key = f'{prefix}:{k}'; hist = self.osc_history.setdefault(key, deque(maxlen=self.cfg.edge.oscillation_window))
                hist.append(float(v))
                if len(hist) >= 3:
                    flips = sum(1 for a, b in zip(hist, list(hist)[1:]) if a*b < 0)
                    score = flips/max(1, len(hist)-1)
                    if score > threshold:
                        mapping[k] = float(v)*gamma; candidate.reasons.append(f'anti_oscillation:{key}:{score:.3f}')
        visit('W', candidate.delta_w); visit('K', candidate.delta_k)
        visit('EPS', candidate.delta_epsilon); visit('OMG', candidate.delta_omega)
        for twin, vals in candidate.delta_theta.items():
            if isinstance(vals, list):
                for idx, val in enumerate(list(vals)):
                    key = f'THETA:{twin}:{idx}'; hist = self.osc_history.setdefault(key, deque(maxlen=self.cfg.edge.oscillation_window))
                    hist.append(float(val))
                    if len(hist) >= 3:
                        flips = sum(1 for a, b in zip(hist, list(hist)[1:]) if a*b < 0); score = flips/max(1, len(hist)-1)
                        if score > threshold:
                            vals[idx] = float(val)*gamma; candidate.reasons.append(f'anti_oscillation:{key}:{score:.3f}')

    def _apply_topology_chatter_guard(self, inp, candidate):
        if not candidate.graph_candidate: return
        rate = self.adapt.topology.chattering_rate(inp.network.topology, candidate.graph_candidate)
        if rate > self.cfg.thresholds.topology_chatter:
            candidate.graph_candidate = []
            self.adapt.topology.lock_for_dwell()
            candidate.reasons.append(f'topology_chattering_blocked_for_dwell:{rate:.3f}')
            candidate.structural = bool(candidate.delta_theta or candidate.delta_k or candidate.delta_omega or
                                        candidate.delta_lambda_c or candidate.delta_policy or candidate.delta_constraints)

    def _candidate_validation_evidence(self, inp, candidate) -> dict | None:
        if not candidate.structural or self.validation_callback is None:
            return None
        req = {'operation': 'validate_candidate', 'transaction_id': inp.transaction_id,
               'base_version': inp.network.version, 'candidate_hash': dataclass_hash(candidate),
               'candidate': asdict(candidate), 'shadow_window': self.cfg.edge.shadow_window}
        try:
            out = self.validation_callback(req)
            return dict(out or {})
        except Exception as exc:
            return {'base_version': inp.network.version, 'candidate_hash': dataclass_hash(candidate),
                    'stability_ok': False, 'coverage_ok': False, 'validation_error': str(exc)}

    def _monitor_post_commit(self, inp, r) -> CommitResult | None:
        watch = self.commit.post_commit_watch()
        if not watch or str(watch.get('version')) != str(inp.network.version):
            return None
        baseline_loss = max(float(watch.get('baseline_loss', 0.0)), 1e-9)
        current_loss = float(r.normalized + r.coc + 10.0*inp.decision.constraint_violations)
        pre_u = max(float(watch.get('baseline_uncertainty', inp.network.uncertainty)), 1e-9)
        current_u = float(inp.network.uncertainty); margin = float(inp.network.margin)
        score = self.cat.score(baseline_loss, current_loss, inp.decision.constraint_violations,
                               margin, pre_u, current_u, self.cfg.thresholds.stability_margin)
        stab_post = inp.network.metadata.get('stability_ok', True) is not False
        degraded = (current_loss > baseline_loss*(1.0+self.cfg.thresholds.rollback_degradation) or
                    inp.decision.constraint_violations > 0 or current_u > self.cfg.thresholds.max_uncertainty or
                    margin < self.cfg.thresholds.stability_margin or not stab_post or
                    score > self.cfg.thresholds.catastrophic)
        post_metrics = {
            'loss': current_loss, 'baseline_loss': baseline_loss,
            'uncertainty': current_u, 'baseline_uncertainty': pre_u,
            'margin': margin, 'constraint_violations': int(inp.decision.constraint_violations),
            'stability_ok': bool(stab_post), 'catastrophic_score': float(score),
            'degraded': bool(degraded), 'remaining_ticks_before': int(watch.get('remaining_ticks', 1)),
        }
        if degraded:
            self.locked = True; self.degraded = True
            result = self.commit.rollback(inp.network.version, f'post_commit_degradation:score={score:.6f}')
            post_metrics['rollback_status'] = result.status
            post_metrics['rollback_version'] = result.rollback_version
            self.commit.record_post_commit_metrics(watch, post_metrics, status=result.status)
            if result.status == 'ROLLED_BACK': self._restore_reliability_state(result.rollback_version)
            return result
        remaining = int(watch.get('remaining_ticks', 1)) - 1
        post_metrics['remaining_ticks_after'] = remaining
        if remaining <= 0:
            self.commit.record_post_commit_metrics(watch, post_metrics, status='MONITOR_ACCEPTED')
            self.commit.clear_post_commit_watch()
        else:
            self.commit.record_post_commit_metrics(watch, post_metrics, status='MONITORING')
            watch['remaining_ticks'] = remaining; self.commit.update_post_commit_watch(watch)
        return None

    def _corrected_prediction(self, inp, r, gp):
        corr = {}; unc = {}
        for key, base in inp.prediction.y_hat_td.items():
            g = gp.get(f'output::{key}')
            if g is None: continue
            corr[key] = float(base) + float(g.mean)
            unc[key] = float(inp.prediction.sigma_pred.get(key, 0.0)) + float(g.variance)
        return corr, unc

    def _compliance(self, gate=None, residuals=None, route=None, trigger=None, candidate=None,
                    validation=None, commit=None, feedbacks=None, provenance=None):
        """Compatibility method; no hard-coded success is returned."""
        if any(x is None for x in (gate,residuals,route,trigger,candidate,validation,commit,feedbacks,provenance)):
            return {'real_offline_separation': False, 'error_decomposition': False, 'deterministic_route': False,
                    'evidence_sufficiency': False, 'bounded_WKG': False, 'shadow_validation': False,
                    'safe_commit': False, 'rollback': self.commit.rollback_capable,
                    'version_provenance': False, 'typed_feedback': False, 'Compliance_7': False}
        return self.compliance_eval.evaluate(gate=gate, residuals=residuals, route=route, trigger=trigger,
                                             candidate=candidate, validation=validation, commit=commit,
                                             feedbacks=feedbacks, provenance=provenance,
                                             max_edges=self.cfg.edge.max_edges,
                                             rollback_capable=self.commit.rollback_capable)

    def process(self, inp: TDOCInput) -> Layer7Output:
        gate = self.evidence.check(inp)
        if not gate.ok:
            # Fail closed without calculating an unbound physical residual,
            # updating drift/GP/TEP, KStore, or contacting TDOC commit.
            r = ResidualBundle(residual_sources={'pred':'REJECTED_UNBOUND', 'state':'REJECTED_UNBOUND'})
            empty_attr = AttributionResult({'Unknown': 1.0}, 1.0, 'Unknown')
            d = DriftResult(0.0, 0.0, False); route = self.route.route(r, empty_attr)
            trig = self.triggers.decide(0.0, d, empty_attr, False, 0.0, 0.0, False, 0, 0, 0, 0)
            candidate = CandidateUpdate(); val = ValidationResult(False, False, False, True, True, True, True,
                                                                   inp.network.boundary_safe, True, reasons=gate.reasons)
            commit = CommitResult('REJECTED', inp.network.version, inp.network.version, reason='evidence gate failed')
            prov = {'transaction_id': inp.transaction_id, 'old_version': inp.network.version,
                    'candidate_version': inp.network.version, 'reasons': gate.reasons, 'trigger_owner': 'TDOC',
                    'native_pair': gate.native_pair, 'dynamic_validation_certified': False,
                    'flight_authorized': False, 'evidence_scope': 'REJECTED_UNBOUND'}
            feedbacks = ({},{},{},{})
            comp = self._compliance(gate,r,route,trig,candidate,val,commit,feedbacks,prov)
            return Layer7Output(inp.transaction_id, AdaptationMode.REJECT, False, r, {}, empty_attr, d, route, trig,
                                candidate, val, commit, *feedbacks, provenance=prov, compliance=comp)

        if (gate.native_pair or {}).get('scope') == 'MODEL_PREDICTION_NATIVE_EXECUTION_DIAGNOSTIC':
            # Current simulation feedback closes the loop without training GP,
            # changing TEP, writing KStore or invoking operational safe commit.
            r = self.residuals.compute(inp)
            a = self.attr.infer(r, {}, inp)
            route = self.route.route(r, a, inp.decision.constraint_violations)
            d = DriftResult(0.0, 0.0, False)
            trig = self.triggers.decide(0., d, a, True, 0., 0., False, 0., 0., 0., 0.)
            candidate = CandidateUpdate()
            val = ValidationResult(False, False, False, True, True, True, True,
                                   inp.network.boundary_safe, True, reasons=['simulation_diagnostic_only'])
            commit = CommitResult('TRANSIENT_ONLY', inp.network.version, inp.network.version,
                                  reason='matched simulation diagnostic; no operational update')
            feedback = {'StateResidualEvidence': dict(r.state_residual),
                        'Reason': ['matched_model_native_diagnostic'], 'rho_attr': float(a.confidence),
                        'nu_new': inp.network.version, 'learning_update': False}
            prov = {'transaction_id': inp.transaction_id, 'trigger_owner': 'TDOC',
                    'native_pair': gate.native_pair, 'flight_authorized': False,
                    'dynamic_validation_certified': False,
                    'evidence_scope': 'MODEL_PREDICTION_NATIVE_EXECUTION_DIAGNOSTIC',
                    'persistent_update': False, 'kstore_write': False}
            return Layer7Output(inp.transaction_id, AdaptationMode.COLLECT_ONLY, True, r, {}, a, d,
                                route, trig, candidate, val, commit, feedback_layer2=feedback,
                                provenance=prov)

        r = self.residuals.compute(inp); feature = self._feature(r, inp)
        targets = {'prediction': r.prediction, 'domain': r.domain, 'coupling': r.coupling,
                   'sync': r.synchronization, 'execution': r.execution}
        targets.update({f'output::{k}': v for k, v in r.output_residual.items()})
        gp = self.gp.evaluate(feature, targets, update=True)
        a = self.attr.infer(r, gp, inp)
        aggregate_anomaly = max([x.anomaly for x in gp.values()] or [0.0])
        aggregate_mean = max([abs(x.mean) for x in gp.values()] or [0.0])
        d = self.drift.update(r.normalized, aggregate_anomaly, aggregate_mean)

        # v2 ordering: Attribution -> deterministic Route -> Trigger.
        route = self.route.route(r, a, inp.decision.constraint_violations)
        preliminary_gov = bool(inp.hints.authorized and inp.hints.identity_ok and inp.hints.policy_ok and inp.hints.trace_ok)
        trig = self.triggers.decide(aggregate_anomaly, d, a, True, inp.hints.structural_error, inp.hints.maturity,
                                    preliminary_gov, inp.hints.source_quality, inp.hints.source_validity,
                                    inp.hints.source_freshness, inp.hints.source_credibility)
        coverage_ok, distances = TEPCoverageGuard.check(inp)
        collect_only = bool(not coverage_ok and trig.slow)
        post_rollback = self._monitor_post_commit(inp, r)
        if post_rollback is not None: collect_only = True
        exec_dom = self.anti.execution_dominant(r, a)
        candidate = self.adapt.build(inp, r, route, trig, a, execution_dominant=exec_dom,
                                     collect_only=collect_only or self.locked)
        candidate.corrected_prediction, candidate.corrected_uncertainty = self._corrected_prediction(inp, r, gp)
        if route.layer2:
            for key, u_corr in candidate.corrected_uncertainty.items():
                candidate.delta_u.setdefault(f'prediction:{key}', float(u_corr) - float(inp.prediction.sigma_pred.get(key, 0.0)))
        self._apply_oscillation_guard(candidate); self._apply_topology_chatter_guard(inp, candidate)
        if collect_only: candidate.reasons.append('out_of_TEP_or_post_commit_collect_only')
        if self.locked: candidate.reasons.append('adaptation_locked')

        external_validation = self._candidate_validation_evidence(inp, candidate)
        val = self.validation.validate(inp, candidate, route, trig, True, coverage_ok, external_validation)
        # TEP posterior is a candidate until safe commit. Preserve the base version
        # before any FIELD update so rollback can restore it exactly.
        if candidate.persistent and candidate.tep_candidate:
            self._save_reliability_state(inp.network.version)
        commit = post_rollback or self.commit.prepare(inp, candidate, val)
        if commit.status == 'COMMITTED':
            for twin, success in inp.hints.tep_success.items():
                self.reliability.observe(twin, inp.hints.tep_cell.get(twin, 'default'), success, inp.execution.evidence_type)
            self._save_reliability_state(commit.committed_version or commit.candidate_version)
            baseline = val.shadow_loss if val.shadow_loss > 0 else max(r.normalized + r.coc, 1e-9)
            self.commit.begin_post_commit_watch(commit.committed_version or commit.candidate_version,
                                                commit.candidate_hash or val.candidate_hash, baseline,
                                                inp.network.uncertainty, inp.network.margin,
                                                self.cfg.edge.post_commit_monitor_ticks)

        rec = {'transaction_id': inp.transaction_id, 'version': inp.network.version,
               'evidence_type': inp.execution.evidence_type.value, 'residuals': r.vector,
               'residual_sources': r.residual_sources,
               'route': {'L2': route.layer2, 'L31': route.layer31, 'L32': route.layer32, 'L33': route.layer33},
               'attribution': a.posterior, 'twin_attribution': a.twin_posterior, 'edge_attribution': a.edge_posterior,
               'selected_cycles': r.selected_cycles, 'crp_id': inp.hints.crp_id, 'sarg_id': inp.hints.sarg_id,
               'mrg': inp.hints.mrg}
        self.ss.append(rec)
        if trig.slow_boundary:
            self.setd.aggregate(self.ss.snapshot(), inp.network.version,
                                {'crp_id': inp.hints.crp_id, 'sarg_id': inp.hints.sarg_id,
                                 'mrg': inp.hints.mrg, 'tep_cells': inp.hints.tep_cell})

        mode = AdaptationMode.ACTIVE
        if self.degraded: mode = AdaptationMode.DEGRADED
        elif self.locked: mode = AdaptationMode.LOCKED
        elif collect_only: mode = AdaptationMode.COLLECT_ONLY
        feedbacks = self._feedback(inp, r, candidate, route, a, commit)
        prov = self.prov.build(inp, candidate, a, route, val, commit)
        prov['tep_distances'] = distances; prov['evidence_gate_reasons'] = gate.reasons
        prov['selected_cycles'] = r.selected_cycles; prov['hidden_loop_active'] = bool(r.lci > 0.0); prov['candidate_hash'] = val.candidate_hash
        prov['external_validation_used'] = bool(external_validation)
        prov['native_pair'] = gate.native_pair
        prov['dynamic_validation_certified'] = False
        prov['flight_authorized'] = False
        prov['evidence_scope'] = 'LEGACY_MODEL_OR_DIAGNOSTIC_ONLY' 
        comp = self._compliance(gate,r,route,trig,candidate,val,commit,feedbacks,prov)
        return Layer7Output(inp.transaction_id, mode, True, r, gp, a, d, route, trig, candidate, val, commit,
                            *feedbacks, provenance=prov, compliance=comp)
