from __future__ import annotations
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

NumberMap = Dict[str, float]


class EvidenceType(str, Enum):
    SIMULATION = "simulation"
    REFERENCE = "reference"
    HIL = "HIL"
    FIELD = "field"


class AdaptationMode(str, Enum):
    ACTIVE = "ACTIVE"
    REJECT = "REJECT"
    COLLECT_ONLY = "COLLECT_ONLY"
    LOCKED = "LOCKED"
    DEGRADED = "DEGRADED"


@dataclass(slots=True)
class OfflineErrorPacket:
    delta_sarg: float = 0.0
    coc_cost: float = 0.0
    constraint_violations: int = 0
    simulation_residual: float = 0.0
    evidence_type: EvidenceType = EvidenceType.SIMULATION
    version: str = "0"
    context: Dict[str, Any] = field(default_factory=dict)
    provenance: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExecutionEvidence:
    y_real: NumberMap
    u_cmd: NumberMap = field(default_factory=dict)
    u_applied: NumberMap = field(default_factory=dict)
    x_obs: NumberMap = field(default_factory=dict)
    d_real: NumberMap = field(default_factory=dict)
    actuator_status: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0
    planned_timestamp: Optional[float] = None
    provenance: Dict[str, Any] = field(default_factory=dict)
    version: str = "0"
    evidence_type: EvidenceType = EvidenceType.FIELD
    # PT_LIB correlation chain.  The paired fields must match when present.
    observation_id: Optional[str] = None
    decision_id: Optional[str] = None
    command_id: Optional[str] = None
    input_observation_id: Optional[str] = None
    correlates_to_decision_id: Optional[str] = None
    correlates_to_actuator_command_id: Optional[str] = None
    actuator_feedback_id: Optional[str] = None
    # Canonical TDOC semantics: key -> {quantity/unit/frame/semantic/...}.
    output_semantics: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass(slots=True)
class PredictionEvidence:
    y_hat_td: NumberMap
    x_hat_td: NumberMap = field(default_factory=dict)
    xi_hat: NumberMap = field(default_factory=dict)
    sigma_pred: NumberMap = field(default_factory=dict)
    jc_pred: float = 0.0
    margin_pred: float = 0.0
    timestamp: float = 0.0
    provenance: Dict[str, Any] = field(default_factory=dict)
    version: str = "0"
    output_semantics: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass(slots=True)
class NetworkSnapshot:
    std: NumberMap = field(default_factory=dict)
    weights: NumberMap = field(default_factory=dict)
    couplings: NumberMap = field(default_factory=dict)  # key: src->dst[:type]
    topology: List[Tuple[str, str]] = field(default_factory=list)
    coc_cost: float = 0.0
    margin: float = 0.0
    uncertainty: float = 0.0
    guidance: Dict[str, Any] = field(default_factory=dict)
    constraints: Dict[str, Any] = field(default_factory=dict)
    epsilon: NumberMap = field(default_factory=dict)
    omega: NumberMap = field(default_factory=dict)
    hidden_loop_lci: NumberMap = field(default_factory=dict)
    # Layer-2 packet trust fields required by L7-63.
    twin_validity: NumberMap = field(default_factory=dict)
    twin_quality: NumberMap = field(default_factory=dict)
    twin_freshness: NumberMap = field(default_factory=dict)
    twin_reliability: NumberMap = field(default_factory=dict)
    timestamp: float = 0.0
    version: str = "0"
    content_hash: str = ""
    boundary_safe: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DecisionTrace:
    real_cost: Optional[float] = None
    feasible_reference_cost: Optional[float] = None
    constraint_violations: int = 0
    decision_confidence: Optional[float] = None
    timestamp: float = 0.0
    version: str = "0"
    provenance: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AdaptationHints:
    """Application-specific evidence supplied through TDOC.

    The side module never invents physical transforms, model Jacobians, or
    safety certificates.  Optional raw/reference pairs below let Layer 7
    compute the formal residuals locally when the upstream layer provides the
    underlying evidence instead of a precomputed residual.
    """
    observation_error: NumberMap = field(default_factory=dict)
    model_error: NumberMap = field(default_factory=dict)
    alignment_error: NumberMap = field(default_factory=dict)
    coupling_error: NumberMap = field(default_factory=dict)
    synchronization_error: NumberMap = field(default_factory=dict)
    emergent_error: float = 0.0
    observation_measurements: NumberMap = field(default_factory=dict)
    observation_reference: NumberMap = field(default_factory=dict)
    model_reference: NumberMap = field(default_factory=dict)
    model_prediction: NumberMap = field(default_factory=dict)
    alignment_expected: NumberMap = field(default_factory=dict)
    alignment_actual: NumberMap = field(default_factory=dict)
    coupling_observed: NumberMap = field(default_factory=dict)
    coupling_predicted: NumberMap = field(default_factory=dict)
    synchronization_source_times: NumberMap = field(default_factory=dict)
    synchronization_canonical_time: Optional[float] = None
    emergent_observed: NumberMap = field(default_factory=dict)
    emergent_baseline: NumberMap = field(default_factory=dict)

    twin_error_scores: NumberMap = field(default_factory=dict)
    edge_error_scores: NumberMap = field(default_factory=dict)
    edge_dependency_scores: NumberMap = field(default_factory=dict)
    edge_risk_scores: NumberMap = field(default_factory=dict)
    source_quality: float = 1.0
    source_validity: float = 1.0
    source_freshness: float = 1.0
    source_credibility: float = 1.0
    maturity: float = 0.0
    structural_error: float = 0.0
    authorized: bool = False
    identity_ok: bool = False
    policy_ok: bool = False
    trace_ok: bool = False
    approver: Optional[str] = None

    # Bayesian twin parameter update inputs, keyed by twin id.
    theta_mean: Dict[str, Sequence[float]] = field(default_factory=dict)
    theta_cov: Dict[str, Sequence[Sequence[float]]] = field(default_factory=dict)
    theta_H: Dict[str, Sequence[Sequence[float]]] = field(default_factory=dict)
    theta_R: Dict[str, Sequence[Sequence[float]]] = field(default_factory=dict)
    theta_residual: Dict[str, Sequence[float]] = field(default_factory=dict)
    theta_bounds: Dict[str, Sequence[Tuple[float, float]]] = field(default_factory=dict)
    state_gain: NumberMap = field(default_factory=dict)
    state_bounds: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    weight_bounds: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    coupling_bounds: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    coupling_sensitivity: NumberMap = field(default_factory=dict)
    coupling_variance: NumberMap = field(default_factory=dict)
    coupling_prior_variance: NumberMap = field(default_factory=dict)

    # L2 typed feedback deltas distinct from TEP posterior.
    bias_delta: NumberMap = field(default_factory=dict)
    bias_bounds: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    uncertainty_delta: NumberMap = field(default_factory=dict)
    uncertainty_bounds: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    quality_delta: NumberMap = field(default_factory=dict)
    quality_bounds: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    validity_delta: NumberMap = field(default_factory=dict)
    validity_bounds: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    reliability_delta: NumberMap = field(default_factory=dict)
    reliability_bounds: Dict[str, Tuple[float, float]] = field(default_factory=dict)

    policy_delta: NumberMap = field(default_factory=dict)
    policy_bounds: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    constraint_delta: NumberMap = field(default_factory=dict)
    constraint_bounds: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    omega_delta: NumberMap = field(default_factory=dict)
    omega_bounds: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    lambda_c_delta: NumberMap = field(default_factory=dict)
    lambda_c_bounds: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    cycle_policy_delta: Dict[str, Any] = field(default_factory=dict)
    epsilon_residual_history: Dict[str, Sequence[float]] = field(default_factory=dict)
    epsilon_bounds: Dict[str, Tuple[float, float]] = field(default_factory=dict)

    # TEP coverage: twin -> list of centroid vectors; current descriptor twin -> vector.
    tep_centroids: Dict[str, Sequence[Sequence[float]]] = field(default_factory=dict)
    tep_descriptor: Dict[str, Sequence[float]] = field(default_factory=dict)
    tep_max_distance: NumberMap = field(default_factory=dict)
    tep_cell: Dict[str, str] = field(default_factory=dict)
    tep_success: Dict[str, bool] = field(default_factory=dict)

    # Candidate-specific stability / shadow evidence fallback.  Preferred path
    # is the TDOC validation callback, invoked after the candidate hash exists.
    network_A: Optional[Sequence[Sequence[float]]] = None
    stability_certificate_ok: Optional[bool] = None
    stability_base_version: Optional[str] = None
    stability_candidate_hash: Optional[str] = None
    shadow_active_metrics: NumberMap = field(default_factory=dict)
    shadow_candidate_metrics: NumberMap = field(default_factory=dict)
    shadow_violation_count: int = 0
    shadow_coverage_ok: Optional[bool] = None
    shadow_margin: Optional[float] = None
    shadow_base_version: Optional[str] = None
    shadow_candidate_hash: Optional[str] = None
    shadow_sample_count: int = 0
    post_metrics: NumberMap = field(default_factory=dict)  # legacy diagnostic input only

    # Context/reference knowledge conditioning.
    crp_id: Optional[str] = None
    sarg_id: Optional[str] = None
    crp_match_quality: float = 1.0
    sarg_deviation: float = 0.0
    mrg_confidence: float = 1.0
    mrg: Dict[str, Any] = field(default_factory=dict)

    # Explicit Layer 3.1 feedback hints; Layer 7 never invents transforms.
    alignment_unit_delta: Dict[str, Any] = field(default_factory=dict)
    alignment_frame_delta: Dict[str, Any] = field(default_factory=dict)
    semantic_map_delta: Dict[str, Any] = field(default_factory=dict)
    shared_uncertainty_delta: NumberMap = field(default_factory=dict)
    freshness_policy_delta: Dict[str, Any] = field(default_factory=dict)

    # Explicit Layer 3.3/scheduler feedback hints. Scheduler changes remain TDOC-owned.
    fast_period_delta: float = 0.0
    meso_period_delta: float = 0.0
    slow_period_delta: float = 0.0
    freshness_threshold_delta: float = 0.0
    aggregation_policy_delta: Dict[str, Any] = field(default_factory=dict)
    commit_boundary_delta: Dict[str, Any] = field(default_factory=dict)
    gamma_envelope_delta: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TDOCInput:
    execution: ExecutionEvidence
    prediction: PredictionEvidence
    network: NetworkSnapshot
    decision: DecisionTrace = field(default_factory=DecisionTrace)
    hints: AdaptationHints = field(default_factory=AdaptationHints)
    transaction_id: str = ""


@dataclass(slots=True)
class ResidualBundle:
    vector: NumberMap = field(default_factory=dict)
    output_residual: NumberMap = field(default_factory=dict)
    state_residual: NumberMap = field(default_factory=dict)
    execution_residual: NumberMap = field(default_factory=dict)
    observation_error_map: NumberMap = field(default_factory=dict)
    model_error_map: NumberMap = field(default_factory=dict)
    alignment_error_map: NumberMap = field(default_factory=dict)
    coupling_error_map: NumberMap = field(default_factory=dict)
    synchronization_error_map: NumberMap = field(default_factory=dict)
    residual_sources: Dict[str, str] = field(default_factory=dict)
    observation: float = 0.0
    model: float = 0.0
    state: float = 0.0
    alignment: float = 0.0
    coupling: float = 0.0
    synchronization: float = 0.0
    prediction: float = 0.0
    decision: float = 0.0
    execution: float = 0.0
    latency: float = 0.0
    domain: float = 0.0
    network: float = 0.0
    emergent: float = 0.0
    coc: float = 0.0
    lci: float = 0.0
    normalized: float = 0.0
    selected_cycles: List[str] = field(default_factory=list)


@dataclass(slots=True)
class GPResult:
    mean: float
    variance: float
    anomaly: float
    zscore: float
    added_to_dictionary: bool = False


@dataclass(slots=True)
class AttributionResult:
    posterior: NumberMap
    confidence: float
    dominant_cause: str
    twin_posterior: NumberMap = field(default_factory=dict)
    edge_posterior: NumberMap = field(default_factory=dict)


@dataclass(slots=True)
class DriftResult:
    changepoint_probability: float
    persistence: float
    drift_evidence: bool


@dataclass(slots=True)
class RouteDecision:
    layer2: bool = False
    layer31: bool = False
    layer32: bool = False
    layer33: bool = False
    policy: bool = False
    execution: bool = False
    confidence: NumberMap = field(default_factory=dict)
    low_confidence: List[str] = field(default_factory=list)

    @property
    def any_feedback(self) -> bool:
        return any([self.layer2, self.layer31, self.layer32, self.layer33, self.policy, self.execution])


@dataclass(slots=True)
class TriggerDecision:
    fast: bool = False
    meso: bool = False
    slow: bool = False
    meso_boundary: bool = False
    slow_boundary: bool = False
    ess: float = 0.0
    maturity: float = 0.0


@dataclass(slots=True)
class CandidateUpdate:
    delta_x: Dict[str, Any] = field(default_factory=dict)
    delta_b: NumberMap = field(default_factory=dict)
    delta_theta: Dict[str, Any] = field(default_factory=dict)
    delta_u: NumberMap = field(default_factory=dict)
    delta_q: NumberMap = field(default_factory=dict)
    delta_v: NumberMap = field(default_factory=dict)
    delta_r: NumberMap = field(default_factory=dict)
    delta_w: NumberMap = field(default_factory=dict)
    delta_k: NumberMap = field(default_factory=dict)
    graph_candidate: List[Tuple[str, str]] = field(default_factory=list)
    delta_epsilon: NumberMap = field(default_factory=dict)
    delta_omega: NumberMap = field(default_factory=dict)
    delta_lambda_c: NumberMap = field(default_factory=dict)
    delta_cycle_policy: Dict[str, Any] = field(default_factory=dict)
    delta_constraints: NumberMap = field(default_factory=dict)
    delta_policy: NumberMap = field(default_factory=dict)
    tep_candidate: Dict[str, Any] = field(default_factory=dict)
    delta_gamma: Dict[str, Any] = field(default_factory=dict)
    alignment_update: Dict[str, Any] = field(default_factory=dict)
    temporal_update: Dict[str, Any] = field(default_factory=dict)
    corrected_prediction: NumberMap = field(default_factory=dict)
    corrected_uncertainty: NumberMap = field(default_factory=dict)
    knowledge_context: Dict[str, Any] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    structural: bool = False
    persistent: bool = False

    def is_empty(self) -> bool:
        return not any([
            self.delta_x, self.delta_b, self.delta_theta, self.delta_u,
            self.delta_q, self.delta_v, self.delta_r, self.delta_w, self.delta_k,
            self.delta_epsilon, self.delta_omega, self.delta_lambda_c,
            self.delta_cycle_policy, self.delta_constraints, self.delta_policy,
            self.tep_candidate, self.delta_gamma, self.alignment_update,
            self.temporal_update,
        ]) and not self.graph_candidate


@dataclass(slots=True)
class ValidationResult:
    evidence_ok: bool
    ess_ok: bool
    route_ok: bool
    bounds_ok: bool
    stability_ok: bool
    shadow_ok: bool
    governance_ok: bool
    boundary_ok: bool
    coverage_ok: bool
    active_loss: float = 0.0
    shadow_loss: float = 0.0
    improvement: float = 0.0
    candidate_hash: str = ""
    stability_source: str = ""
    shadow_source: str = ""
    reasons: List[str] = field(default_factory=list)

    @property
    def commit_gate(self) -> bool:
        return all([
            self.evidence_ok, self.ess_ok, self.route_ok, self.bounds_ok,
            self.stability_ok, self.shadow_ok, self.governance_ok,
            self.boundary_ok, self.coverage_ok,
        ])


@dataclass(slots=True)
class CommitResult:
    status: str
    current_version: str
    candidate_version: str
    committed_version: Optional[str] = None
    rollback_version: Optional[str] = None
    reason: str = ""
    candidate_hash: str = ""


@dataclass(slots=True)
class Layer7Output:
    transaction_id: str
    mode: AdaptationMode
    evidence_ok: bool
    residuals: ResidualBundle
    gp: Dict[str, GPResult]
    attribution: AttributionResult
    drift: DriftResult
    route: RouteDecision
    trigger: TriggerDecision
    candidate: CandidateUpdate
    validation: ValidationResult
    commit: CommitResult
    feedback_layer2: Dict[str, Any] = field(default_factory=dict)
    feedback_layer31: Dict[str, Any] = field(default_factory=dict)
    feedback_layer32: Dict[str, Any] = field(default_factory=dict)
    feedback_layer33: Dict[str, Any] = field(default_factory=dict)
    provenance: Dict[str, Any] = field(default_factory=dict)
    compliance: Dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        def conv(obj):
            if isinstance(obj, Enum):
                return obj.value
            if hasattr(obj, "__dataclass_fields__"):
                return {k: conv(v) for k, v in asdict(obj).items()}
            if isinstance(obj, dict):
                return {str(k): conv(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [conv(v) for v in obj]
            return obj
        return conv(self)
