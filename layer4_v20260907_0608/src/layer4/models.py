from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any
from .utils import probability01


@dataclass
class AtomicSnapshot:
    version: str
    reference_time_s: float
    hash: str
    std: dict[str,Any]
    center_of_consistency: dict[str,Any]
    mismatch: float | None
    weights: dict[str,float]
    couplings: dict[str,float]
    topology: dict[str,Any]
    consistency_margin: float | None
    uncertainty_td: dict[str,Any]
    guidance: dict[str,Any]
    constraints: dict[str,Any]
    context: dict[str,Any]
    stability: dict[str,Any]
    provenance: dict[str,Any]
    slow_policy: dict[str,Any] = field(default_factory=dict)
    source_snapshot: dict[str,Any] = field(default_factory=dict, repr=False)

    @property
    def snapshot_time(self) -> float:
        return self.reference_time_s

    def to_dict(self):
        d=asdict(self); d.pop("source_snapshot",None)
        d["snapshot_version"]=self.version
        d["snapshot_time"]=self.reference_time_s
        d["CoC"]=self.center_of_consistency
        d["consistency_cost"]=self.mismatch
        d["network_margin"]=self.consistency_margin
        d["twin_weights"]=self.weights
        d["coupling_matrix"]=self.couplings
        d["active_topology"]=self.topology
        d["uncertainty"]=self.uncertainty_td
        d["S_dyn"]=self.stability
        return d


@dataclass
class Scenario:
    scenario_id: str
    probability: float
    topology_mode: str
    topology: dict[str,Any]
    action_sequence: list[list[float]]
    disturbance_sequence: list[list[float]]
    topology_sequence: list[dict[str,Any]] = field(default_factory=list)
    parameters: dict[str,Any] = field(default_factory=dict)
    constraints: dict[str,Any] = field(default_factory=dict)
    objectives: dict[str,float] = field(default_factory=dict)
    degradation: dict[str,Any] = field(default_factory=dict)
    provenance: dict[str,Any] = field(default_factory=dict)
    def to_dict(self): return asdict(self)


@dataclass
class Candidate:
    candidate_id: str
    scenario_id: str
    snapshot_version: str
    snapshot_hash: str
    probability: float
    trajectory: list[list[float]]
    uncertainty: list[list[list[float]]]
    network_trajectory: list[dict[str,Any]]
    actions: list[list[float]]
    objective_vector: dict[str,float]
    normalized_objectives: dict[str,float] = field(default_factory=dict)
    feasible: bool = False
    violation_probability: float = 1.0
    pareto_rank: int | None = None
    rom_valid: bool | None = None
    rom_error: float | None = None
    verified: bool = False
    verification: dict[str,Any] = field(default_factory=dict)
    provenance: dict[str,Any] = field(default_factory=dict)
    signature: str | None = None
    scalar_cost: float | None = None
    robust_score: float | None = None
    robust_feasible: bool | None = None
    worst_case_scenario_id: str | None = None
    worst_case_disturbance: str | None = None

    def __post_init__(self):
        # P1-7: probability is a closed-domain contract.  Preserve fail-closed
        # semantics for non-finite values and eliminate floating-point excursions
        # such as 1.0000000000000002 at the producer/model boundary.
        self.violation_probability = probability01(self.violation_probability)

    def to_dict(self):
        # Candidate payloads contain several large horizon tensors.  ``asdict``
        # recursively copied every tensor and the aliases below caused the same
        # data to be copied again by Layer4Result.  These objects are immutable
        # after registration, so a field-wise view preserves the wire contract
        # without putting archival copying on the control critical path.
        d={
            "candidate_id":self.candidate_id,"scenario_id":self.scenario_id,
            "snapshot_version":self.snapshot_version,"snapshot_hash":self.snapshot_hash,
            "probability":self.probability,"trajectory":self.trajectory,
            "uncertainty":self.uncertainty,"network_trajectory":self.network_trajectory,
            "actions":self.actions,"objective_vector":self.objective_vector,
            "normalized_objectives":self.normalized_objectives,"feasible":self.feasible,
            "violation_probability":self.violation_probability,"pareto_rank":self.pareto_rank,
            "rom_valid":self.rom_valid,"rom_error":self.rom_error,
            "verified":self.verified,"verification":self.verification,
            "provenance":self.provenance,"signature":self.signature,
            "scalar_cost":self.scalar_cost,"robust_score":self.robust_score,
            "robust_feasible":self.robust_feasible,
            "worst_case_scenario_id":self.worst_case_scenario_id,
            "worst_case_disturbance":self.worst_case_disturbance,
        }
        # Canonical Layer-4 executable contract aliases from Layer4-details §51.2.
        d.update({
            "predicted_trajectory": d["trajectory"],
            "predicted_uncertainty": d["uncertainty"],
            "predicted_twin_weights": [n.get("W",{}) for n in d["network_trajectory"]],
            "predicted_couplings": [n.get("K",{}) for n in d["network_trajectory"]],
            "predicted_consistency": [n.get("J_C") for n in d["network_trajectory"]],
            "predicted_network_margin": [n.get("mu") for n in d["network_trajectory"]],
            "mission_cost": d["objective_vector"].get("mission"),
            "time_cost": d["objective_vector"].get("time"),
            "energy_cost": d["objective_vector"].get("energy"),
            "risk_cost": d["objective_vector"].get("risk"),
            "uncertainty_cost": d["objective_vector"].get("uncertainty"),
            "consistency_cost": d["objective_vector"].get("consistency"),
            "chance_constraint_risk": d["violation_probability"],
            "candidate_verified": d["verified"],
        })
        return d


@dataclass
class Layer4Result:
    snapshot_version: str
    snapshot_hash: str
    pareto_front: list[dict[str,Any]]
    verified_candidates: list[dict[str,Any]]
    candidates: list[dict[str,Any]]
    budget: dict[str,Any]
    conformance: dict[str,bool]
    provenance: dict[str,Any]
    robust_summary: dict[str,Any] = field(default_factory=dict)
    actuator_command: bool = False
    network_commit: bool = False

    def to_dict(self):
        # The engine has already materialized canonical candidate dictionaries.
        # Do not recursively duplicate them again at the TDOC boundary.
        d={
            "snapshot_version":self.snapshot_version,"snapshot_hash":self.snapshot_hash,
            "pareto_front":self.pareto_front,"verified_candidates":self.verified_candidates,
            "candidates":self.candidates,"budget":self.budget,
            "conformance":self.conformance,"provenance":self.provenance,
            "robust_summary":self.robust_summary,"actuator_command":self.actuator_command,
            "network_commit":self.network_commit,
        }
        # Canonical Y_4,k projections (Layer4-details eq.53 / Formalism E4-86..87).
        d["predicted_trajectories"]={c["candidate_id"]:c.get("predicted_trajectory",c.get("trajectory")) for c in d["candidates"]}
        d["risk_profiles"]={c["candidate_id"]:{"chance_constraint_risk":c.get("chance_constraint_risk",c.get("violation_probability")),"risk_cost":c.get("risk_cost",(c.get("objective_vector") or {}).get("risk"))} for c in d["candidates"]}
        d["uncertainty_profiles"]={c["candidate_id"]:c.get("predicted_uncertainty",c.get("uncertainty")) for c in d["candidates"]}
        d["network_predictions"]={c["candidate_id"]:c.get("network_trajectory",[]) for c in d["candidates"]}
        return d
