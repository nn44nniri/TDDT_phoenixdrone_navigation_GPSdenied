from __future__ import annotations
from .config import Layer7Config
from .models import CandidateUpdate, TDOCInput, ValidationResult, RouteDecision, TriggerDecision
from .safety import BoundsGuard, StabilityGuard
from .utils import dataclass_hash


class ShadowValidator:
    WEIGHTS = {'error': 1.0, 'nlpd': 1.0, 'coc': 1.0, 'risk': 1.0, 'violations': 10.0}
    def __init__(self, cfg: Layer7Config): self.cfg = cfg
    def _loss(self, m):
        return sum(self.WEIGHTS.get(k, 1.0) * float(v) for k, v in m.items() if isinstance(v, (int, float)))

    def check(self, inp: TDOCInput, candidate: CandidateUpdate, external: dict | None = None) -> tuple[bool, float, float, float, str]:
        if not candidate.structural:
            return True, 0.0, 0.0, 0.0, 'nonstructural'
        chash = dataclass_hash(candidate); external = external or {}
        if external:
            if str(external.get('base_version', '')) != str(inp.network.version):
                return False, 0.0, 0.0, 0.0, 'external_shadow_base_version_mismatch'
            if str(external.get('candidate_hash', '')) != chash:
                return False, 0.0, 0.0, 0.0, 'external_shadow_candidate_hash_mismatch'
            a = dict(external.get('active_metrics') or {}); c = dict(external.get('candidate_metrics') or {})
            n = int(external.get('sample_count', 0)); vio = int(external.get('violation_count', 0))
            coverage = bool(external.get('coverage_ok', False))
            margin = float(external.get('margin', -float('inf')))
            source = 'tdoc_candidate_shadow'
        else:
            h = inp.hints
            if str(h.shadow_base_version or '') != str(inp.network.version) or str(h.shadow_candidate_hash or '') != chash:
                return False, 0.0, 0.0, 0.0, 'missing_candidate_bound_shadow_evidence'
            a = h.shadow_active_metrics; c = h.shadow_candidate_metrics
            n = int(h.shadow_sample_count); vio = int(h.shadow_violation_count)
            coverage = h.shadow_coverage_ok is True
            margin = float(h.shadow_margin) if h.shadow_margin is not None else -float('inf')
            source = 'bound_shadow_hints'
        if not a or not c:
            return False, 0.0, 0.0, 0.0, 'missing_shadow_metrics'
        if n <= 0 or n > self.cfg.edge.shadow_window:
            return False, 0.0, 0.0, 0.0, 'invalid_shadow_window_sample_count'
        la = self._loss(a); lc = self._loss(c); imp = la - lc
        ok = (imp >= self.cfg.thresholds.shadow_min_improvement and vio == 0 and coverage and
              margin >= self.cfg.thresholds.stability_margin)
        return ok, la, lc, imp, source


class GovernanceGate:
    @staticmethod
    def check(inp: TDOCInput, candidate: CandidateUpdate) -> tuple[bool, str]:
        if not candidate.structural and not candidate.persistent:
            return True, 'not_required_for_transient'
        h = inp.hints
        ok = bool(h.authorized and h.identity_ok and h.policy_ok and h.trace_ok)
        return ok, 'authorized' if ok else 'missing_identity_policy_authority_or_trace'


class ValidationEngine:
    def __init__(self, cfg: Layer7Config):
        self.cfg = cfg; self.bounds = BoundsGuard(cfg); self.stability = StabilityGuard(cfg)
        self.shadow = ShadowValidator(cfg); self.gov = GovernanceGate()

    def validate(self, inp: TDOCInput, candidate: CandidateUpdate, route: RouteDecision,
                 trig: TriggerDecision, evidence_ok: bool, coverage_ok: bool,
                 external_validation: dict | None = None) -> ValidationResult:
        chash = dataclass_hash(candidate)
        bounds_ok, bounds_reason = self.bounds.check(inp, candidate)
        stab_ok, stab_reason = self.stability.check(inp, candidate, external_validation)
        shadow_ok, la, lc, imp, shadow_reason = self.shadow.check(inp, candidate, external_validation)
        gov_ok, gov_reason = self.gov.check(inp, candidate)
        ess_ok = (not candidate.persistent) or (trig.ess >= self.cfg.thresholds.slow_ess_min)
        route_ok = (not candidate.persistent) or route.any_feedback
        boundary_ok = bool(inp.network.boundary_safe)
        if not candidate.persistent: coverage_ok = True
        reasons = []
        for ok, reason in [(bounds_ok, bounds_reason), (stab_ok, stab_reason), (shadow_ok, shadow_reason),
                           (gov_ok, gov_reason), (boundary_ok, 'boundary_unsafe')]:
            if not ok and reason: reasons.append(reason)
        if not ess_ok: reasons.append('insufficient_ESS')
        if not route_ok: reasons.append('missing_persistent_route')
        if not coverage_ok: reasons.append('out_of_TEP_coverage')
        return ValidationResult(evidence_ok=evidence_ok, ess_ok=ess_ok, route_ok=route_ok,
                                bounds_ok=bounds_ok, stability_ok=stab_ok, shadow_ok=shadow_ok,
                                governance_ok=gov_ok, boundary_ok=boundary_ok, coverage_ok=coverage_ok,
                                active_loss=la, shadow_loss=lc, improvement=imp, candidate_hash=chash,
                                stability_source=stab_reason, shadow_source=shadow_reason, reasons=reasons)
