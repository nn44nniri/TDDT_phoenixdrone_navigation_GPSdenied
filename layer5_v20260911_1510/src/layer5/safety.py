from __future__ import annotations
from typing import Any
import math
from .config import Layer5Config
from .phoenix import PhoenixSpecialization

class PredictiveSafetyFilter:
    """Independent final decision-safety filter; does not replace Layer-6 execution safety."""
    def __init__(self,config:Layer5Config,phoenix:PhoenixSpecialization): self.config=config; self.phoenix=phoenix
    def filter(self,command:list[float],vp_ok:bool,active_constraints:dict[str,Any]|None=None)->dict[str,Any]:
        reasons=[]; active_constraints=active_constraints or {}
        if not vp_ok: reasons.append("verification_preserving_tube_failed")
        phx=self.phoenix.physical_feasibility(command,correction_requested=False); reasons.extend(phx["reasons"])
        maxnorm=float(self.config.get("safety.max_command_norm",2.0)); norm=math.sqrt(sum(float(x)*float(x) for x in command)) if len(command)==4 else float("inf")
        if norm>maxnorm: reasons.append("command_norm_limit")
        # Only generic declared constraints are interpreted here. Unknown constraints are retained for Layer 6, not silently invented.
        tmax=active_constraints.get("throttle_max")
        if tmax is not None and len(command)==4 and command[2]>float(tmax): reasons.append("active_throttle_max")
        rmax=active_constraints.get("abs_rudder_max")
        if rmax is not None and len(command)==4 and abs(command[3])>float(rmax): reasons.append("active_rudder_limit")
        gate=not reasons
        return {"gate":gate,"command":list(command) if gate else None,"reasons":sorted(set(reasons)),"model":"bounded Phoenix logical-input safety filter",
                "command_norm":norm,"max_command_norm":maxnorm,"active_constraints":active_constraints,"physical":phx,"layer6_execution_safety_required":True}

class BaselineSafeController:
    def __init__(self,config:Layer5Config): self.config=config
    def action(self,last_safe:list[float]|None=None)->dict[str,Any]:
        f=self.config.get("safety.fallback",{}) or {}; preferred=str(f.get("strategy","hold")); candidates=[]
        if last_safe is not None: candidates.append(("lastSafe",list(last_safe)))
        if isinstance(f.get("conservative_pilot_domain"),list): candidates.append(("conservative",[float(x) for x in f["conservative_pilot_domain"]]))
        if isinstance(f.get("pilot_domain"),list): candidates.append(("hold",[float(x) for x in f["pilot_domain"]]))
        if isinstance(f.get("emergency_pilot_domain"),list): candidates.append(("emergency",[float(x) for x in f["emergency_pilot_domain"]]))
        order=[preferred,"lastSafe","conservative","hold","emergency"]; seen=set(); ordered=[]
        for name in order:
            for n,c in candidates:
                if n==name and n not in seen: ordered.append((n,c)); seen.add(n)
        for n,c in candidates:
            if n not in seen: ordered.append((n,c)); seen.add(n)
        for name,cmd in ordered:
            if len(cmd)==4 and -1<=cmd[0]<=1 and -1<=cmd[1]<=1 and 0<=cmd[2]<=1 and -1<=cmd[3]<=1:
                return {"strategy":name,"command":cmd,"ladder":[n for n,_ in ordered],"advanced":False,"first_admissible":True}
        return {"strategy":"none","command":[0.0,0.0,0.0,0.0],"ladder":[n for n,_ in ordered],"advanced":False,"first_admissible":False}

class RuntimeAssurance:
    def choose(self,advanced:dict[str,Any],fallback:dict[str,Any],final_preconditions:bool)->dict[str,Any]:
        if final_preconditions and advanced.get("gate"):
            return {"gate":True,"source":"advanced","command":advanced["command"],"fallback_used":False,"simplex":"advanced-controller"}
        return {"gate":False,"source":"baseline","command":fallback["command"],"fallback_used":True,"reason":"advanced_path_not_assured","simplex":"baseline-safe-controller"}
