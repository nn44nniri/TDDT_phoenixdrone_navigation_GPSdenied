from __future__ import annotations
from dataclasses import asdict
import numpy as np
from .config import Layer7Config
from .models import CandidateUpdate, TDOCInput, AttributionResult, ResidualBundle
from .utils import dataclass_hash


class AntiCompensationGuard:
    def __init__(self, cfg: Layer7Config): self.cfg = cfg
    def execution_dominant(self, r: ResidualBundle, a: AttributionResult) -> bool:
        return bool(r.execution > self.cfg.thresholds.execution and
                    a.posterior.get('Execution', 0.0) >= self.cfg.thresholds.attribution_confidence)


class TEPCoverageGuard:
    @staticmethod
    def check(inp: TDOCInput) -> tuple[bool, dict[str, float]]:
        h = inp.hints; distances = {}; any_supplied = False
        for twin, desc in h.tep_descriptor.items():
            cents = h.tep_centroids.get(twin, [])
            if not cents: continue
            any_supplied = True; x = np.asarray(desc, dtype=float)
            ds = [float(np.linalg.norm(x - np.asarray(c, dtype=float))) for c in cents]
            d = min(ds); distances[twin] = d
            if d > float(h.tep_max_distance.get(twin, float('inf'))): return False, distances
        return (True if any_supplied else False), distances


class StabilityGuard:
    def __init__(self, cfg: Layer7Config): self.cfg = cfg

    def check(self, inp: TDOCInput, candidate: CandidateUpdate, external: dict | None = None) -> tuple[bool, str]:
        if not candidate.structural:
            return True, 'nonstructural'
        chash = dataclass_hash(candidate); external = external or {}
        # Preferred candidate-specific TDOC validation callback evidence.
        if external:
            if str(external.get('base_version', '')) != str(inp.network.version):
                return False, 'external_stability_base_version_mismatch'
            if str(external.get('candidate_hash', '')) != chash:
                return False, 'external_stability_candidate_hash_mismatch'
            if 'stability_ok' in external:
                return bool(external['stability_ok']), 'tdoc_candidate_specific_stability'
            if external.get('network_A') is not None:
                return self._spectral(external['network_A'])
        # Backward-compatible fallback is accepted only when explicitly bound to
        # this candidate and base version; unbound booleans are rejected.
        h = inp.hints
        if h.stability_certificate_ok is not None:
            if str(h.stability_base_version or '') != str(inp.network.version):
                return False, 'stability_certificate_base_version_mismatch'
            if str(h.stability_candidate_hash or '') != chash:
                return False, 'stability_certificate_candidate_hash_mismatch'
            return bool(h.stability_certificate_ok), 'bound_precomputed_certificate'
        if h.network_A is not None and h.stability_candidate_hash == chash and str(h.stability_base_version or '') == str(inp.network.version):
            return self._spectral(h.network_A)
        return False, 'missing_candidate_specific_stability_certificate'

    def _spectral(self, Araw) -> tuple[bool, str]:
        A = np.asarray(Araw, dtype=float)
        if A.ndim != 2 or A.shape[0] != A.shape[1]: return False, 'invalid_network_A'
        eig = np.linalg.eigvals(A); abscissa = float(np.max(np.real(eig))) if eig.size else -float('inf')
        return bool(abscissa < -self.cfg.thresholds.stability_margin), f'spectral_abscissa={abscissa}'


class BoundsGuard:
    """Checks actual candidate targets against declared physical/contract bounds."""
    def __init__(self, cfg: Layer7Config): self.cfg = cfg

    @staticmethod
    def _within(x, bounds) -> bool:
        if bounds is None: return False
        lo, hi = bounds; return float(lo) <= float(x) <= float(hi)

    @staticmethod
    def _finite_obj(o):
        if isinstance(o, dict): return all(BoundsGuard._finite_obj(v) for v in o.values())
        if isinstance(o, (list, tuple)): return all(BoundsGuard._finite_obj(v) for v in o)
        if isinstance(o, (int, float)): return bool(np.isfinite(o))
        return True

    def check(self, inp: TDOCInput, candidate: CandidateUpdate) -> tuple[bool, str]:
        if not self._finite_obj(asdict(candidate)):
            return False, 'nonfinite_candidate'
        h = inp.hints; missing = []; bad = []
        for k, d in candidate.delta_x.items():
            b = h.state_bounds.get(k); target = float(inp.network.std.get(k, 0.0)) + float(d)
            if b is None: missing.append(f'state:{k}')
            elif not self._within(target, b): bad.append(f'state:{k}')
        for twin, ds in candidate.delta_theta.items():
            b = h.theta_bounds.get(twin); m = h.theta_mean.get(twin)
            if b is None or m is None: missing.append(f'theta:{twin}'); continue
            if len(b) != len(ds) or len(m) != len(ds): bad.append(f'theta:{twin}'); continue
            for i, d in enumerate(ds):
                if not self._within(float(m[i]) + float(d), b[i]): bad.append(f'theta:{twin}[{i}]')
        # Typed L2 correction bounds. GP-derived prediction uncertainty
        # keys are probabilistic outputs, not persistent physical state.
        l2_specs = [
            ('B', candidate.delta_b, inp.network.metadata.get('bias', {}), h.bias_bounds),
            ('U', {k:v for k,v in candidate.delta_u.items() if not str(k).startswith('prediction:')}, inp.network.metadata.get('uncertainty', {}), h.uncertainty_bounds),
            ('Q', candidate.delta_q, inp.network.twin_quality, h.quality_bounds),
            ('V', candidate.delta_v, inp.network.twin_validity, h.validity_bounds),
            ('R', {k:v for k,v in candidate.delta_r.items() if k in h.reliability_delta}, inp.network.twin_reliability, h.reliability_bounds),
        ]
        for prefix, mapping, base, bounds in l2_specs:
            for k, d in mapping.items():
                b = bounds.get(k); target = float(base.get(k, 0.0)) + float(d)
                if b is None: missing.append(f'{prefix}:{k}')
                elif not self._within(target, b): bad.append(f'{prefix}:{k}')
        # TEP-derived reliability deltas are intrinsically probability-bounded.
        for k, d in candidate.delta_r.items():
            if k not in h.reliability_delta:
                target = float(inp.network.twin_reliability.get(k, 0.5)) + float(d)
                if not (0.0 <= target <= 1.0): bad.append(f'R_TEP:{k}')

        for twin, d in candidate.delta_w.items():
            target = float(inp.network.weights.get(twin, 0.0)) + float(d)
            b = h.weight_bounds.get(twin, (0.0, 1.0))
            if not self._within(target, b): bad.append(f'W:{twin}')
            if float(inp.network.twin_validity.get(twin, 0.0)) <= 0 and abs(target) > 1e-12: bad.append(f'W_invalid:{twin}')
        for edge, d in candidate.delta_k.items():
            b = h.coupling_bounds.get(edge); target = float(inp.network.couplings.get(edge, 0.0)) + float(d)
            if b is None: missing.append(f'K:{edge}')
            elif not self._within(target, b): bad.append(f'K:{edge}')
        for edge, d in candidate.delta_epsilon.items():
            b = h.epsilon_bounds.get(edge); target = float(inp.network.epsilon.get(edge, 0.0)) + float(d)
            if b is None: missing.append(f'epsilon:{edge}')
            elif not self._within(target, b): bad.append(f'epsilon:{edge}')
        for key, d in candidate.delta_omega.items():
            b = h.omega_bounds.get(key); target = float(inp.network.omega.get(key, 0.0)) + float(d)
            if b is None: missing.append(f'omega:{key}')
            elif not self._within(target, b): bad.append(f'omega:{key}')
        for key, d in candidate.delta_lambda_c.items():
            b = h.lambda_c_bounds.get(key); target = float(inp.network.metadata.get('lambda_c', {}).get(key, 0.0)) + float(d)
            if b is None: missing.append(f'lambda_c:{key}')
            elif not self._within(target, b): bad.append(f'lambda_c:{key}')
        for key, d in candidate.delta_policy.items():
            b = h.policy_bounds.get(key); target = float(inp.network.metadata.get('policy', {}).get(key, 0.0)) + float(d)
            if b is None: missing.append(f'policy:{key}')
            elif not self._within(target, b): bad.append(f'policy:{key}')
        for key, d in candidate.delta_constraints.items():
            b = h.constraint_bounds.get(key); target = float(inp.network.constraints.get(key, 0.0)) + float(d)
            if b is None: missing.append(f'constraint:{key}')
            elif not self._within(target, b): bad.append(f'constraint:{key}')
        if candidate.graph_candidate and len(candidate.graph_candidate) > self.cfg.edge.max_edges:
            bad.append('topology_edge_budget')
        if self.cfg.fail_closed_structural and candidate.structural and missing:
            return False, 'missing_bounds:' + ','.join(sorted(missing))
        if bad:
            return False, 'out_of_bounds:' + ','.join(sorted(bad))
        return True, 'physical_contract_bounds_ok'


class CatastrophicGuard:
    def __init__(self, cfg: Layer7Config): self.cfg = cfg
    def score(self, pre_loss: float, post_loss: float, violations: int, margin: float,
              pre_uncertainty: float, post_uncertainty: float, margin_critical: float = 0.0) -> float:
        eps = 1e-9
        return (post_loss/(pre_loss+eps) + float(violations) + max(0.0, margin_critical-margin) +
                post_uncertainty/(pre_uncertainty+eps))
