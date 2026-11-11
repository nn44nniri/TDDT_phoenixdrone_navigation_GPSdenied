from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class AlignedTwinState:
    twin_id: str
    source_time_s: float
    reference_time_s: float
    age_s: float
    freshness: float
    gate: int
    quality: float
    validity: float
    reliability: float
    normalized_uncertainty: float
    raw_state: dict[str, Any]
    canonical_state: dict[str, Any]
    covariance: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    def to_dict(self) -> dict[str, Any]: return asdict(self)


@dataclass
class SharedTransDomainState:
    reference_time_s: float
    context: dict[str, Any]
    meta: dict[str, Any]
    representations: dict[str, AlignedTwinState]
    constraints: dict[str, Any] = field(default_factory=dict)
    risks: dict[str, Any] = field(default_factory=dict)
    objectives: dict[str, Any] = field(default_factory=dict)
    control_status: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    def to_dict(self) -> dict[str, Any]:
        return {"reference_time_s": self.reference_time_s, "context": self.context, "meta": self.meta,
                "representations": {k:v.to_dict() for k,v in self.representations.items()},
                "constraints": self.constraints, "risks": self.risks, "objectives": self.objectives,
                "control_status": self.control_status, "provenance": self.provenance}


@dataclass
class EdgeSpec:
    source: str
    target: str
    coupling_types: list[str]
    base_strength: float = 1.0
    tolerance: float = 5.0
    mandatory: bool = False
    active: bool = True
    delay_ticks: int = 0
    on_threshold: float = 0.45
    off_threshold: float = 0.25
    dwell_windows: int = 2
    risk_weight: float = 0.0
    criticality: float = 0.5
    coupling_type_scales: dict[str, float] = field(default_factory=dict)
    coupling_type_bounds: dict[str, list[float]] = field(default_factory=dict)
    phi: dict[str, str] = field(default_factory=dict)
    execution_threshold: float = 1.0e-9
    @property
    def key(self) -> str: return f"{self.source}->{self.target}"


@dataclass
class MesoGuidance:
    window_index: int
    crp_match: dict[str, Any]
    sarg_reference: dict[str, Any]
    sarg_deviation: dict[str, Any]
    decision_weight_delta: dict[str, float]
    constraint_delta: dict[str, Any]
    optimizer_delta: dict[str, Any]
    transient_coupling_delta: dict[str, float]
    candidate_topology: dict[str, Any]
    risk_signal: float
    load_signal: float
    provenance: dict[str, Any] = field(default_factory=dict)
    def to_dict(self) -> dict[str, Any]: return asdict(self)


@dataclass
class SlowKnowledge:
    period_index: int
    mean_weights: dict[str, float]
    mean_couplings: dict[str, float]
    topology: dict[str, Any]
    mean_mismatch: float
    worst_mismatch: float
    stable_fraction: float
    lci_summary: dict[str, Any]
    risk: float
    cost: float
    crp_histogram: dict[str, int]
    sarg_deviation: float
    provenance: dict[str, Any] = field(default_factory=dict)
    def to_dict(self) -> dict[str, Any]: return asdict(self)


@dataclass
class NetworkSnapshot:
    policy_version: str
    reference_time_s: float
    std: dict[str, Any]
    topology: dict[str, Any]
    weights: dict[str, float]
    couplings: dict[str, float]
    center_of_consistency: dict[str, Any]
    mismatch: float | None
    consistency_margin: float | None
    consistency_region: dict[str, Any]
    stability: dict[str, Any]
    emergence: dict[str, Any]
    hidden_loop: dict[str, Any]
    guidance: dict[str, Any]
    slow_policy: dict[str, Any]
    uncertainty_td: dict[str, Any]
    constraints: dict[str, Any]
    context: dict[str, Any]
    provenance: dict[str, Any]
    registry: dict[str, Any] = field(default_factory=dict)
    coupling_effects: dict[str, Any] = field(default_factory=dict)
    tdoc_health: dict[str, Any] = field(default_factory=dict)
    feedback_route: dict[str, Any] = field(default_factory=dict)
    decision_route: dict[str, Any] = field(default_factory=dict)
    dependency_status: dict[str, Any] = field(default_factory=dict)
    version_state: dict[str, Any] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    def to_dict(self) -> dict[str, Any]: return asdict(self)


@dataclass
class StepResult:
    snapshot: NetworkSnapshot
    fast_record: dict[str, Any]
    meso_event: dict[str, Any] | None = None
    slow_event: dict[str, Any] | None = None
    def to_dict(self) -> dict[str, Any]:
        return {"snapshot":self.snapshot.to_dict(),"fast_record":self.fast_record,
                "meso_event":self.meso_event,"slow_event":self.slow_event}
