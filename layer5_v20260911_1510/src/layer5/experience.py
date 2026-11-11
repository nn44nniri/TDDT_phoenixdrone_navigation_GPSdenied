from __future__ import annotations
from collections import deque
from statistics import mean
from typing import Any
from .config import Layer5Config
from .utils import finite_float, sha256_obj

class EpisodeAggregator:
    """Fast->Meso/Slow single-episode aggregation and MRG wrapper (§24A)."""
    def __init__(self, config:Layer5Config):
        self.config=config
        self.fast=deque(maxlen=int(config.get("edge.knowledge_ram_records",128)))
        self.meso=deque(maxlen=int(config.get("experience.meso_history_max",64)))
        self.slow=deque(maxlen=int(config.get("experience.slow_history_max",32)))
        self.mrg_history=deque(maxlen=int(config.get("experience.mrg_history_max",64)))
        self.fast_count=0

    def append_fast(self, record:dict[str,Any], refs:dict[str,Any]|None=None)->dict[str,Any]:
        self.fast_count+=1; self.fast.append(record)
        m=int(self.config.get("experience.fast_per_meso",3)); l=int(self.config.get("experience.meso_per_slow",2))
        events={"fast_count":self.fast_count,"meso":None,"slow":None,"mrg":None}
        if m>0 and self.fast_count % m==0:
            window=list(self.fast)[-m:]; agg=self._aggregate(window,"MESO",len(self.meso)+1)
            self.meso.append(agg); mrg=self._mrg(agg,refs or {}); self.mrg_history.append(mrg)
            events["meso"]=agg; events["mrg"]=mrg
            if l>0 and len(self.meso)%l==0:
                sw=list(self.meso)[-l:]; sagg=self._aggregate(sw,"SLOW",len(self.slow)+1); self.slow.append(sagg); events["slow"]=sagg
        return events

    def _aggregate(self, rows:list[dict[str,Any]], level:str, index:int)->dict[str,Any]:
        def vals(path):
            out=[]
            for r in rows:
                cur=r
                for p in path:
                    cur=cur.get(p) if isinstance(cur,dict) else None
                if cur is not None:
                    try: out.append(float(cur))
                    except Exception: pass
            return out
        risks=vals(["risk"]); conf=vals(["decision_confidence"]); jc=vals(["network","J_C"]); mu=vals(["network","mu"])
        obj_keys=sorted({k for r in rows for k in ((r.get("objectives") or {}).keys())})
        obj={k:mean([finite_float(r.get("objectives",{}).get(k)) for r in rows]) for k in obj_keys} if rows else {}
        rec={"kind":f"{level}_AGGREGATE","index":index,"count":len(rows),"risk_mean":mean(risks) if risks else None,"risk_max":max(risks) if risks else None,
             "confidence_mean":mean(conf) if conf else None,"J_C_mean":mean(jc) if jc else None,"J_C_max":max(jc) if jc else None,
             "mu_min":min(mu) if mu else None,"objective_mean":obj,"selected_candidates":[r.get("candidate_id") for r in rows if isinstance(r,dict)]}
        rec["sha256"]=sha256_obj(rec); return rec

    def _mrg(self, aggregate:dict[str,Any], refs:dict[str,Any])->dict[str,Any]:
        rr=refs.get("references",{}) if isinstance(refs,dict) else {}
        # MRG consumes resolved reference guidance only.  It does not scan raw datasets or invent domain semantics.
        crp=(rr.get("CRP") or {}).get("guidance") or {}
        sarg=(rr.get("SARG") or {}).get("guidance") or {}
        weights={}
        for src in (crp.get("objective_weights") if isinstance(crp,dict) else None, sarg.get("objective_weights") if isinstance(sarg,dict) else None):
            if isinstance(src,dict):
                for k,v in src.items():
                    try: weights[k]=float(v)
                    except Exception: pass
        g={"kind":"MRG","aggregate_index":aggregate.get("index"),"CRP_ref":(rr.get("CRP") or {}).get("id"),"SARG_ref":(rr.get("SARG") or {}).get("id"),
           "objective_weights":weights,"risk_context":aggregate.get("risk_mean"),"confidence_context":aggregate.get("confidence_mean"),"read_only":True}
        g["sha256"]=sha256_obj(g); return g

    def summary(self)->dict[str,Any]:
        return {"fast_count":self.fast_count,"meso_count":len(self.meso),"slow_count":len(self.slow),
                "meso":list(self.meso),"slow":list(self.slow),"mrg":list(self.mrg_history),
                "bounded_ram":True,"full_episode_in_ram":False}
