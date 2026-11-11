from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Mapping
import time, uuid

@dataclass(frozen=True)
class PilotCommand:
    aileron: float
    elevator: float
    throttle: float
    rudder: float
    mode: str | None = None
    discrete: tuple[str, ...] = ()

    @classmethod
    def from_vector(cls, u_cmd, *, mode=None, discrete=()):
        if len(u_cmd) != 4:
            raise ValueError("u_cmd must be [aileron,elevator,throttle,rudder]")
        a,e,t,r=(float(x) for x in u_cmd)
        return cls(a,e,t,r,mode,tuple(discrete))

    def as_gui_state(self):
        return {"roll": self.aileron, "pitch": self.elevator,
                "throttle": self.throttle, "yaw": self.rudder}

@dataclass
class DecisionEnvelope:
    transaction_id: str
    command: PilotCommand
    tau_apply: float | None = None
    tau_deadline: float | None = None
    constraints: Mapping[str, Any] = field(default_factory=dict)
    safety: Mapping[str, Any] = field(default_factory=dict)
    confidence: float | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)
    version: str | None = None
    execution_segment: Mapping[str, Any] | None = None

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]):
        packet=d.get("layer6_package", d)
        u=packet.get("u_cmd") or packet.get("first_move")
        if u is None: raise ValueError("missing u_cmd")
        intent=packet.get("execution_intent", {}) or {}
        mode=intent.get("requested_pilot_mode") or packet.get("mode")
        discrete=intent.get("discrete", ()) or ()
        from .segments import contract
        return cls(
            transaction_id=str(packet.get("transaction_id") or d.get("transaction_id") or uuid.uuid4()),
            command=PilotCommand.from_vector(u,mode=mode,discrete=discrete),
            tau_apply=_f(packet.get("tau_apply")), tau_deadline=_f(packet.get("tau_deadline")),
            constraints=packet.get("C_exec",{}) or {}, safety=packet.get("S_safe",{}) or {},
            confidence=_f(packet.get("rho_decision")), provenance=packet.get("pi_decision",{}) or {},
            version=str(packet.get("nu")) if packet.get("nu") is not None else None,
            execution_segment=contract(d))

    def now(self): return time.time()

def _f(x):
    return None if x is None else float(x)
