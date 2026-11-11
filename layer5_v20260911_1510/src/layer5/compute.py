from __future__ import annotations
from typing import Any
from .config import Layer5Config

class EdgeProfileManager:
    """Formalism §22–24 adaptive-complexity manager.

    It may reduce candidate/sensitivity/CVaR work, but never changes safety, Phoenix
    validation, integrity, admissibility hard limits, PSF or RTA bounds.
    """
    VALID=("EDGE-LIGHT","EDGE-ROBUST","EDGE-SAFETY")
    def __init__(self,config:Layer5Config): self.config=config
    def plan(self)->dict[str,Any]:
        p=str(self.config.get("edge.profile","EDGE-ROBUST")).upper()
        if p not in self.VALID: p="EDGE-ROBUST"
        base_solve=int(self.config.get("edge.solve_max",12)); base_sens=int(self.config.get("edge.sensitivity_top_k",3)); cvar_cfg=bool(self.config.get("admissibility.cvar_enabled",False))
        if p=="EDGE-LIGHT":
            solve=min(base_solve,int(self.config.get("edge.light.solve_max",6))); sens=0; cvar=False; covariance=False; chance=False
            features=["Tube","HardConstraints","Lexicographic","PSF","RTA"]
        elif p=="EDGE-SAFETY":
            solve=min(base_solve,int(self.config.get("edge.safety.solve_max",12))); sens=min(int(self.config.get("edge.safety.sensitivity_top_k",base_sens)),int(self.config.get("edge.candidates_max",12))); cvar=True; covariance=True; chance=True
            features=["Tube","HardConstraints","Lexicographic","PSF","RTA","Covariance","ChanceConstraints","TopKSensitivity","CVaR","ExtendedSensitivity"]
        else:
            solve=base_solve; sens=base_sens; cvar=cvar_cfg; covariance=True; chance=True
            features=["Tube","HardConstraints","Lexicographic","PSF","RTA","Covariance","ChanceConstraints","TopKSensitivity"]
        return {"profile":p,"solve_max":max(1,solve),"sensitivity_top_k":max(0,sens),"cvar_enabled":cvar,"covariance_enabled":covariance,"chance_enabled":chance,
                "features":features,"safety_bounds_relaxed":False,"physical_validation_gates_relaxed":False,"adaptive_complexity_only":True}
    def degrade(self,plan:dict[str,Any],elapsed_ms:float,deadline_ms:float)->dict[str,Any]:
        out=dict(plan); out["degraded"]=False; out["degradation_reason"]=None
        ratio=float(elapsed_ms)/max(float(deadline_ms),1e-9)
        if ratio>=float(self.config.get("edge.degrade_at_fraction",0.60)):
            out["degraded"]=True; out["degradation_reason"]="compute_budget_pressure"; out["sensitivity_top_k"]=min(int(out["sensitivity_top_k"]),1)
            out["solve_max"]=min(int(out["solve_max"]),max(1,int(self.config.get("edge.degraded_solve_max",3))))
        out["budget_fraction_at_plan"]=ratio
        # Invariant: complexity can decrease, safety cannot.
        out["safety_bounds_relaxed"]=False; out["physical_validation_gates_relaxed"]=False
        return out
