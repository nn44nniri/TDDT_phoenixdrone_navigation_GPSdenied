from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any

@dataclass
class LayerRegistration:
    layer_id: int
    name: str
    role: str
    implementation: str
    enabled: bool
    implemented: bool
    root: str|None=None
    version: str|None=None
    health: str="UNKNOWN"
    provenance: dict[str,Any]=field(default_factory=dict)
    def to_dict(self): return asdict(self)

@dataclass
class Readiness:
    layer_id: int
    ready: bool
    valid: bool=True
    version_ok: bool=True
    boundary_ok: bool=True
    health_ok: bool=True
    policy_route: bool=True
    reasons: list[str]=field(default_factory=list)
    evidence: dict[str,Any]=field(default_factory=dict)
    def to_dict(self): return asdict(self)

@dataclass
class InvocationEvent:
    invocation_id: str
    transaction_id: str
    trigger_owner: str
    source: str
    destination: str
    event_type: str
    input_version: str|None
    time_boundary: str|None
    reason: str
    status: str
    event_time: str
    previous_hash: str|None=None
    event_hash: str|None=None
    details: dict[str,Any]=field(default_factory=dict)
    def to_dict(self): return asdict(self)

@dataclass
class CycleResult:
    transaction_id: str
    orchestration_version: str
    reference_time_s: float
    status: str
    layer_status: dict[str,Any]
    layer2_packets: dict[str,Any]=field(default_factory=dict)
    layer3_result: dict[str,Any]|None=None
    layer4_result: dict[str,Any]|None=None
    future_layers: dict[str,Any]=field(default_factory=dict)
    conformance: dict[str,bool]=field(default_factory=dict)
    provenance: dict[str,Any]=field(default_factory=dict)
    def to_dict(self):
        d=asdict(self)
        # Backward compatibility: ``future_layers`` existed before Layers 5/6 became active.
        # ``downstream_layers`` is the semantically current alias.
        d["downstream_layers"]=d["future_layers"]
        return d
