from __future__ import annotations
class HealthManager:
    def evaluate(self,registry:dict,preflight:dict|None=None)->dict:
        implemented=[v for k,v in registry.items() if int(k) in (2,3,4)]
        layer_ok=all(v.get("health") in ("AVAILABLE","EXTERNAL") for v in implemented)
        deps_ok=True if preflight is None else bool(preflight.get("required_ok",False))
        return {"ok":bool(layer_ok and deps_ok),"layer_ok":layer_ok,"dependencies_ok":deps_ok,"mode":"NORMAL" if layer_ok and deps_ok else "DEGRADED"}
