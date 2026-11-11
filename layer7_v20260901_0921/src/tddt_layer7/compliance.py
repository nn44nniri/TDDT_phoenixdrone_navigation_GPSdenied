from __future__ import annotations
import math
from .models import CandidateUpdate, Layer7Output, ResidualBundle, RouteDecision, TriggerDecision, ValidationResult, CommitResult


class ComplianceEvaluator:
    """Runtime evaluator for Formalism_layer7_v2 L7-171/172.

    This replaces the former hard-coded ``Compliance_7=True``.  The result is
    transaction-specific: rejected/incomplete evidence can legitimately yield
    zero even though the package implements the capability.
    """
    REQUIRED_VECTOR = {'obs','model','state','align','cpl','sync','pred','dec','exec'}
    L2_KEYS = {'DeltaX','DeltaB','DeltaTheta','DeltaU','DeltaQ','DeltaV','DeltaR','TEP_candidate','Reason','rho_attr','nu_new'}
    L31_KEYS = {'DeltaT_unit','DeltaT_frame','Delta_h','DeltaSigma_STD','DeltaSemanticMap','DeltaFreshnessPolicy','Reason','nu_new'}
    L32_KEYS = {'DeltaW','DeltaK','G_candidate','DeltaEpsilon','DeltaOmega','DeltaLambda_C','DeltaCyclePolicy','Reason','nu_new'}
    L33_KEYS = {'DeltaDelta_t','DeltaDelta_t_m','DeltaDelta_T','DeltaFreshnessThreshold','DeltaAggregationPolicy','DeltaCommitBoundary','DeltaGammaEnvelope','Reason','nu_new'}

    @staticmethod
    def _typed(ok_flag: bool, feedback: dict, required: set[str]) -> bool:
        if not ok_flag: return feedback == {} or isinstance(feedback, dict)
        return isinstance(feedback, dict) and required.issubset(feedback)

    def evaluate(self, *, gate, residuals: ResidualBundle, route: RouteDecision,
                 trigger: TriggerDecision, candidate: CandidateUpdate,
                 validation: ValidationResult, commit: CommitResult,
                 feedbacks: tuple[dict,dict,dict,dict], provenance: dict,
                 max_edges: int, rollback_capable: bool) -> dict[str, bool]:
        real_offline = bool(gate.ok and gate.version_ok and gate.correlation_ok and gate.freshness_ok and gate.semantic_ok)
        decomp = set(residuals.vector) == self.REQUIRED_VECTOR and all(math.isfinite(float(v)) for v in residuals.vector.values())
        deterministic_route = isinstance(route, RouteDecision) and isinstance(route.low_confidence, list)
        evidence = math.isfinite(float(trigger.ess)) and trigger.ess >= 0.0 and ((not candidate.persistent) or validation.ess_ok)
        bounded_wkg = bool(validation.bounds_ok and len(candidate.graph_candidate) <= max_edges)
        validate = bool((not candidate.structural) or (validation.shadow_ok and validation.stability_ok and validation.coverage_ok))
        safe_commit = bool((not candidate.persistent) or commit.status in {
            'COMMITTED','STAGED_NOT_COMMITTED','REJECTED','TDOC_COMMIT_REJECTED'
        })
        rollback = bool(rollback_capable)
        version = bool(provenance.get('old_version') is not None and provenance.get('candidate_version') is not None and
                       provenance.get('transaction_id') is not None)
        f2,f31,f32,f33 = feedbacks
        feedback = (self._typed(route.layer2,f2,self.L2_KEYS) and self._typed(route.layer31,f31,self.L31_KEYS) and
                    self._typed(route.layer32,f32,self.L32_KEYS) and self._typed(route.layer33,f33,self.L33_KEYS))
        out = {
            'real_offline_separation': real_offline,
            'error_decomposition': decomp,
            'deterministic_route': deterministic_route,
            'evidence_sufficiency': evidence,
            'bounded_WKG': bounded_wkg,
            'shadow_validation': validate,
            'safe_commit': safe_commit,
            'rollback': rollback,
            'version_provenance': version,
            'typed_feedback': feedback,
        }
        out['Compliance_7'] = all(out.values())
        return out
