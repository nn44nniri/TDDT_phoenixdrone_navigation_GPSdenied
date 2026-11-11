from __future__ import annotations
import json
from pathlib import Path
import numpy as np


class ExplicitParetoPolicy:
    """Optional validated multiparametric/PWA policy lookup for E4-72; empty reference package falls back to bounded online Pareto."""
    def __init__(self,config):
        self.config=config; p=config.package_root/config.get("pareto.explicit_regions_file","resources/pareto/explicit_regions.json")
        self.path=p; self.data=json.loads(Path(p).read_text(encoding="utf-8")) if Path(p).exists() else {"regions":[],"validated":False}

    @staticmethod
    def _stage_code(stage):
        return {"hover":0.0,"takeoff":0.15,"transition":0.45,"cruise":0.75,"landing":1.0}.get(str(stage).lower(),0.75)

    def theta(self,snapshot):
        ctx=snapshot.context or {}; wind=float(ctx.get("wind_speed_ms",(ctx.get("boulder_reference_climate") or {}).get("wind_speed_ms",0.0)) or 0.0)
        risk=float((snapshot.guidance or {}).get("risk_signal",0.0) or 0.0); mt=float(np.mean(list(snapshot.weights.values()))) if snapshot.weights else 0.0; mk=float(np.mean(list(snapshot.couplings.values()))) if snapshot.couplings else 0.0
        stage=ctx.get("flight_mode") or ctx.get("stage") or ctx.get("stage_id") or "cruise"
        return np.asarray([risk,wind,mt,mk,self._stage_code(stage)],dtype=float)

    def proposals(self,snapshot):
        if not bool(self.data.get("validated",False)): return []
        th=self.theta(snapshot); out=[]
        for r in self.data.get("regions",[]):
            lo=np.asarray(r.get("lower",[-np.inf]*len(th)),float); hi=np.asarray(r.get("upper",[np.inf]*len(th)),float)
            if len(lo)!=len(th) or len(hi)!=len(th) or not np.all((th>=lo)&(th<=hi)): continue
            F=np.asarray(r.get("F"),float); g=np.asarray(r.get("g"),float); a=(F@th+g).reshape(-1)
            out.append({"name":r.get("id","explicit"),"vector":a.tolist(),"probability":float(r.get("probability",1.0)),"explicit_region":r.get("id"),"validated":True})
        return out

    def status(self):
        return {"path":str(self.path),"validated":bool(self.data.get("validated",False)),"region_count":len(self.data.get("regions",[])),"fallback":"bounded_online_pareto"}
