from __future__ import annotations

import math
from typing import Any, Mapping

from .config import Layer3Config
from .models import SharedTransDomainState
from .topology import TopologyManager
from .utils import clamp


class CouplingManager:
    """Computes directional scalar and six-class K_ij values.

    K_by_type is independent: an edge can assign a different scale/bound to each
    Data/Model/State/Error/Objective/Control component.
    """
    SIX=("Data","Model","State","Error","Objective","Control")
    def __init__(self, config: Layer3Config, topology: TopologyManager):
        self.config=config; self.topology=topology

    def compute(self, std: SharedTransDomainState, weights: Mapping[str,float], graph: Mapping[str,Any],
                slow_policy: Mapping[str,Any] | None=None, transient_delta: Mapping[str,float] | None=None
                ) -> tuple[dict[str,float],dict[str,Any]]:
        slow_policy=dict(slow_policy or {}); transient_delta=dict(transient_delta or {})
        bounds=dict(slow_policy.get("coupling_bounds") or self.config.policy.get("coupling_bounds") or {})
        typed_policy=dict(slow_policy.get("coupling_type_bounds") or self.config.policy.get("coupling_type_bounds") or {})
        couplings={}; details={}
        for key,edge in self.topology.edges.items():
            g=(graph.get("edges") or {}).get(key,{})
            active=bool(g.get("active_effective",0))
            if not active:
                couplings[key]=0.0
                details[key]={"proposed":0.0,"projected":0.0,"reason":"inactive_or_safety_cut",
                              "K_by_type":{k:0.0 for k in self.SIX},"K_vector_order":list(self.SIX),"K_vector":[0.0]*6}
                continue
            src=std.representations.get(edge.source); dst=std.representations.get(edge.target)
            if src is None or dst is None:
                couplings[key]=0.0
                details[key]={"reason":"missing_endpoint","K_by_type":{k:0.0 for k in self.SIX},
                              "K_vector_order":list(self.SIX),"K_vector":[0.0]*6}; continue
            trust=math.sqrt(max(0.,weights.get(edge.source,0.)*weights.get(edge.target,0.)))
            quality=math.sqrt(max(0.,src.quality*dst.quality))
            uf=max(0.,1.-.5*(src.normalized_uncertainty+dst.normalized_uncertainty))
            rel=float(g.get("relevance",0.0))
            proposed=edge.base_strength*(.35+.65*rel)*(.35+.65*trust)*quality*uf+float(transient_delta.get(key,0.0))
            lo,hi=bounds.get(key,[self.config.network.coupling_min,self.config.network.coupling_max])
            base=clamp(proposed,float(lo),float(hi))
            by_type={}
            for kind in self.SIX:
                if kind not in edge.coupling_types:
                    by_type[kind]=0.0; continue
                scale=float(edge.coupling_type_scales.get(kind,1.0))
                tlo,thi=edge.coupling_type_bounds.get(kind,typed_policy.get(key,{}).get(kind,[0.0,1.0]))
                by_type[kind]=clamp(base*scale,float(tlo),float(thi))
            aggregate=max(by_type.values(),default=0.0)
            couplings[key]=aggregate
            details[key]={"proposed":proposed,"projected":aggregate,"base_projected":base,
                          "trust_factor":trust,"quality_factor":quality,"uncertainty_factor":uf,"relevance":rel,
                          "coupling_types":list(edge.coupling_types),"K_by_type":by_type,
                          "K_vector_order":list(self.SIX),"K_vector":[by_type[k] for k in self.SIX],
                          "delay_ticks":int(edge.delay_ticks),"execution_threshold":float(edge.execution_threshold),
                          "phi_contract":{kind:edge.phi.get(kind,f"Phi_{kind.lower()}") for kind in edge.coupling_types},
                          "provenance":{"source":"Layer3-v3 coupling contract","state_owner_preserved":True}}
        return couplings,{"edges":details,"formula":"3.2-4g/3.2-9/3.3-44/3.3-45"}

    def matrix(self,couplings:Mapping[str,float],order:list[str])->list[list[float]]:
        idx={n:i for i,n in enumerate(order)}; m=[[0. for _ in order] for _ in order]
        for key,val in couplings.items():
            if "->" not in key: continue
            s,t=key.split("->",1)
            if s in idx and t in idx: m[idx[s]][idx[t]]=float(val)
        return m


class CouplingOperatorRegistry:
    """Builds delayed, typed Phi effects before destination execution."""
    SIX=("Data","Model","State","Error","Objective","Control")
    def __init__(self): self._history:dict[str,list[dict[str,Any]]]={}

    def apply(self,std:SharedTransDomainState,graph:Mapping[str,Any],couplings:Mapping[str,float],*,
              coupling_details:Mapping[str,Any]|None=None,edge_residuals:Mapping[str,float]|None=None)->dict[str,Any]:
        edge_residuals=dict(edge_residuals or {}); cdet=(coupling_details or {}).get("edges",{})
        effects={}
        for key,rec in (graph.get("edges") or {}).items():
            if not rec.get("active_effective"): continue
            source,target=rec["source"],rec["target"]
            src=std.representations.get(source); dst=std.representations.get(target)
            if src is None or dst is None: continue
            detail=cdet.get(key,{})
            by_type=dict(detail.get("K_by_type") or {k:(float(couplings.get(key,0.)) if k in (rec.get("coupling_types") or []) else 0.) for k in self.SIX})
            delay=int(rec.get("delay_ticks",0) or 0)
            hist=self._history.setdefault(key,[])
            hist.append({"canonical_state":dict(src.canonical_state),"raw_state":dict(src.raw_state),"context":dict(src.context),
                         "reference_time_s":float(std.reference_time_s),"covariance":dict(src.covariance)})
            keep=max(16,delay+4)
            if len(hist)>keep: del hist[:-keep]
            delay_ready=len(hist)>delay
            delayed=hist[-1-delay] if delay_ready else None
            typed={}
            if delayed is not None:
                for kind in rec.get("coupling_types") or []:
                    K=float(by_type.get(kind,0.0))
                    if kind=="Data": typed[kind]={"payload":delayed["canonical_state"],"raw_payload":delayed["raw_state"],"strength":K,"state_overwrite":False}
                    elif kind=="Model": typed[kind]={"source_context":delayed["context"],"source_state":delayed["canonical_state"],"strength":K,"model_parameter_overwrite":False}
                    elif kind=="State": typed[kind]={"comparable_state":delayed["canonical_state"],"raw_state":delayed["raw_state"],"strength":K,"state_overwrite":False}
                    elif kind=="Error": typed[kind]={"cross_domain_residual":edge_residuals.get(key),"covariance":delayed["covariance"],"strength":K}
                    elif kind=="Objective": typed[kind]={"objective_context":std.objectives,"strength":K,"destination":"guidance_only"}
                    elif kind=="Control": typed[kind]={"control_status":std.control_status,"strength":K,"destination":"Layer5","actuator_command":False}
            effects[key]={"source":source,"target":target,"K":float(couplings.get(key,0.)),"K_by_type":by_type,
                          "K_vector_order":list(self.SIX),"K_vector":[float(by_type.get(k,0.)) for k in self.SIX],
                          "delay_ticks":delay,"delay_ready":delay_ready,
                          "delayed_source_time_s":None if delayed is None else delayed["reference_time_s"],
                          "types":typed,"phi_recorded":True,"execution_threshold":float(detail.get("execution_threshold",1e-9)),
                          "edge_record":{"active":True,"type":list(typed),"K":float(couplings.get(key,0.)),"delay":delay,
                                         "Phi":dict(detail.get("phi_contract") or {}),"provenance":"Layer3 CouplingOperatorRegistry"}}
        return {"edges":effects,"supported_classes":list(self.SIX),"state_injection":False,
                "formula":"3.2-3..3.2-4g"}
