from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any

@dataclass
class GateRecord:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    def to_dict(self): return asdict(self)

@dataclass
class Layer5Candidate:
    candidate_id: str
    raw: dict[str, Any]
    snapshot_version: str
    snapshot_hash: str
    actions: list[list[float]]
    objective_vector: dict[str, float]
    normalized_objectives: dict[str, float]
    network_trajectory: list[dict[str, Any]]
    uncertainty: list[Any]
    feasible: bool
    violation_probability: float
    verified: bool
    verification: dict[str, Any]
    provenance: dict[str, Any]
    ci: GateRecord | None = None
    adm: GateRecord | None = None
    vp: GateRecord | None = None
    logical_actions: list[list[float]] = field(default_factory=list)
    solver: dict[str, Any] = field(default_factory=dict)

@dataclass
class Recommendation:
    ranking: list[str]
    scores: dict[str, Any]
    tradeoff: dict[str, Any]
    evidence: dict[str, Any]
    def to_dict(self): return asdict(self)

@dataclass
class Decision:
    decision_id: str
    candidate_id: str | None
    mode: str
    confidence: float
    approval: dict[str, Any]
    def to_dict(self): return asdict(self)

@dataclass
class ControlAction:
    first_move: list[float]
    actuator_targets: list[str]
    constraints: dict[str, Any]
    apply_time: float
    deadline_time: float
    source: str
    def to_dict(self): return asdict(self)

@dataclass
class Layer6ActionPackage:
    command: list[float]
    actuator_targets: list[str]
    execution_constraints: dict[str, Any]
    safety_policy: dict[str, Any]
    apply_time: float
    deadline_time: float
    decision_confidence: float
    provenance: dict[str, Any]
    version: str
    def to_dict(self):
        d=asdict(self)
        # Canonical aliases from the formal Layer5->Layer6 contract (L5-86 / base Eq.71).
        d.update({"u_cmd":list(self.command),"A_target":list(self.actuator_targets),"C_exec":dict(self.execution_constraints),
                  "S_safe":dict(self.safety_policy),"tau_apply":self.apply_time,"tau_deadline":self.deadline_time,
                  "rho_decision":self.decision_confidence,"pi_decision":dict(self.provenance),"nu":self.version,"Source":"Layer5"})
        d["formal_contract"]={k:d[k] for k in ["u_cmd","A_target","C_exec","S_safe","tau_apply","tau_deadline","rho_decision","pi_decision","nu"]}
        return d

@dataclass
class Layer5Result:
    ok: bool
    learning_mode: str
    operational_mode: str
    snapshot_version: str | None
    snapshot_hash: str | None
    recommendation: dict[str, Any]
    decision: dict[str, Any]
    control_action: dict[str, Any] | None
    layer6_package: dict[str, Any] | None
    gates: dict[str, Any]
    fallback: dict[str, Any]
    knowledge: dict[str, Any]
    tdoc: dict[str, Any]
    phoenix: dict[str, Any]
    provenance: dict[str, Any]
    policy: dict[str, Any] = field(default_factory=dict)
    explanation: dict[str, Any] = field(default_factory=dict)
    layer7_evidence: dict[str, Any] = field(default_factory=dict)
    experience: dict[str, Any] = field(default_factory=dict)
    environment_feedback: dict[str, Any] = field(default_factory=dict)
    candidate_registry: dict[str, Any] = field(default_factory=dict)
    compliance: dict[str, Any] = field(default_factory=dict)
    network_commit: bool = False
    model_retraining: bool = False
    direct_pwm: bool = False
    def to_dict(self): return asdict(self)
