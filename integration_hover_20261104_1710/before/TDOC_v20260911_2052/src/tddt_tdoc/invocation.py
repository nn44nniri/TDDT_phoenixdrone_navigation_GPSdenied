from __future__ import annotations
from typing import Any
from .models import Readiness

class InvocationGate:
    OWNER="TDOC/RuntimeScheduler"
    def evaluate(self,layer_id:int,*,valid=True,version_ok=True,boundary_ok=True,health_ok=True,policy_route=True,evidence:dict[str,Any]|None=None)->Readiness:
        flags={"valid":bool(valid),"version_ok":bool(version_ok),"boundary_ok":bool(boundary_ok),"health_ok":bool(health_ok),"policy_route":bool(policy_route)}
        reasons=[k for k,v in flags.items() if not v]
        return Readiness(layer_id=layer_id,ready=all(flags.values()),reasons=reasons,evidence=dict(evidence or {}),**flags)
