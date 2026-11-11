from __future__ import annotations
import math
from typing import Any
from .config import Layer5Config
from .utils import clip, finite_float, sha256_obj

class PolicyAdapter:
    """Bounded Layer-3.3 guidance and Layer-7 policy adapter (Formalism §6).

    It changes only decision weights/Layer-5 policy parameters.  It never commits
    Layer-3 network W/K/G and never performs model retraining.
    """
    def __init__(self, config: Layer5Config): self.config=config

    def adapt(self, layer4: dict[str,Any], external_policy: dict[str,Any] | None=None, episode_mrg:dict[str,Any]|None=None, setd_prior:dict[str,Any]|None=None) -> dict[str,Any]:
        base_w={k:float(v) for k,v in (self.config.get("objectives.guidance_weights",{}) or {}).items()}
        prior_used=False
        # SETD may always carry valid safety/HOLD knowledge, but only a package with
        # explicit control-effect evidence may seed online *control* decision weights.
        setd_safety_valid=False; setd_control_valid=False
        if isinstance(setd_prior,dict) and setd_prior.get("ok"):
            stage=setd_prior.get("knowledge") or {}; kp=stage.get("knowledge") if isinstance(stage,dict) else {}
            q=(kp or {}).get("knowledge_quality") if isinstance(kp,dict) else {}
            setd_safety_valid=bool((q or {}).get("safety_knowledge_valid"))
            setd_control_valid=bool((q or {}).get("control_knowledge_valid"))
            pw=(kp or {}).get("decision_weights") if isinstance(kp,dict) and setd_control_valid else None
            if isinstance(pw,dict):
                for k,v in pw.items():
                    if k in base_w:
                        try: base_w[k]=max(0.0,float(v)); prior_used=True
                        except Exception: pass
        lo=float(self.config.get("policy.decision_weight_min",0.0)); hi=float(self.config.get("policy.decision_weight_max",10.0))
        knowledge=(layer4.get("provenance") or {}).get("knowledge") or {}
        gamma=self._gamma(layer4,knowledge)
        if isinstance(episode_mrg,dict) and episode_mrg:
            gamma=dict(gamma); gamma["meso"]={**(gamma.get("meso") or {}),**episode_mrg}; gamma["source_episode_mrg"]=True
        meso=(gamma.get("meso") or {}) if isinstance(gamma,dict) else {}
        slow=(gamma.get("slow") or {}) if isinstance(gamma,dict) else {}
        direct=(knowledge.get("objective_weights") or layer4.get("objective_weights") or {})
        bm=float(self.config.get("policy.meso_gain",1.0)); bs=float(self.config.get("policy.slow_gain",1.0))
        w=dict(base_w)
        applied=[]
        for source,gain,label in ((direct,1.0,"direct"),(meso.get("objective_weights",{}),bm,"meso"),(slow.get("objective_weights",{}),bs,"slow")):
            if not isinstance(source,dict): continue
            for k,v in source.items():
                if k not in w: continue
                try:
                    w[k]=max(lo,min(hi,w[k]+gain*(float(v)-base_w[k]))) if label!="direct" else max(lo,min(hi,float(v)))
                    applied.append({"source":label,"objective":k,"value":w[k]})
                except Exception: pass
        # Formal decision weights beta^dec are projected to the simplex: beta>=0, sum(beta)=1.
        total=sum(max(0.0,float(v)) for v in w.values())
        if total<=1e-15:
            n=max(len(w),1); w={k:1.0/n for k in w}
        else:
            w={k:max(0.0,float(v))/total for k,v in w.items()}
        theta={"version":str(self.config.get("governance.policy_version","L5-policy-v1")),
               "constraints":dict(self.config.get("policy.default_constraints",{}) or {}),
               "parameters":dict(self.config.get("policy.parameters",{}) or {}),
               "source":"configuration"}
        # Current-cycle upstream guidance is bounded and may tune only declared Layer-5 policy keys.
        gp=(gamma.get("policy") or {}) if isinstance(gamma,dict) else {}
        if isinstance(gp,dict):
            for k,v in gp.items():
                if k in set(self.config.get("policy.allowed_runtime_keys",[]) or []):
                    theta["parameters"][k]=v
            if gp: theta["source"]="Layer3.3-guidance"
        update=self._layer7_update(external_policy or (layer4.get("provenance") or {}).get("layer7_policy_update"))
        if update["accepted"]:
            theta["version"]=update["version"]
            theta["parameters"].update(update["projected_delta"])
            theta["source"]="Layer7-authorized-bounded-update"
        active_constraints={}
        for src in (knowledge.get("active_constraints"), gamma.get("constraints") if isinstance(gamma,dict) else None, theta.get("constraints")):
            if isinstance(src,dict): active_constraints.update(src)
        w_epi=self._epistemic_weights(layer4)
        return {"decision_weights":w,"decision_weight_sum":sum(w.values()),"epistemic_weights_read_only":w_epi,"W_epi_not_beta_dec":True,"theta":theta,"gamma":gamma,"active_constraints":active_constraints,
                "setd_prior_used":prior_used,"setd_safety_knowledge_valid":setd_safety_valid,"setd_control_knowledge_valid":setd_control_valid,"episode_mrg_used":bool(episode_mrg),
                "layer7_update":update,"applied_weight_updates":applied,"network_commit":False,
                "model_retraining":False,"digest":sha256_obj({"w":w,"theta":theta,"gamma":gamma})}

    def _epistemic_weights(self, layer4:dict[str,Any])->dict[str,float]:
        sources=[]
        n=layer4.get("network_predictions")
        if isinstance(n,list) and n and isinstance(n[0],dict): sources.append(n[0].get("W"))
        c=layer4.get("candidates")
        if isinstance(c,list) and c and isinstance(c[0],dict):
            nt=c[0].get("network_trajectory") or []
            if nt and isinstance(nt[0],dict): sources.append(nt[0].get("W"))
        for src in sources:
            if isinstance(src,dict):
                out={}
                for k,v in src.items():
                    try: out[str(k)]=float(v)
                    except Exception: pass
                if out: return out
        return {}

    def _gamma(self, layer4:dict[str,Any], knowledge:dict[str,Any])->dict[str,Any]:
        for src in (layer4.get("guidance"), knowledge.get("guidance"), knowledge.get("Gamma"), (layer4.get("provenance") or {}).get("guidance")):
            if isinstance(src,dict): return dict(src)
        return {}

    def _layer7_update(self, update:Any)->dict[str,Any]:
        out={"accepted":False,"reason":"absent","version":None,"projected_delta":{},"norm":0.0}
        if not isinstance(update,dict): return out
        if update.get("authorized_by_layer7") is not True:
            out["reason"]="not_layer7_authorized"; return out
        if not update.get("version"):
            out["reason"]="missing_version"; return out
        delta=update.get("delta") or {}
        if not isinstance(delta,dict): out["reason"]="delta_not_mapping"; return out
        allowed=set(self.config.get("policy.allowed_online_update_keys",[]) or [])
        vals={}
        for k,v in delta.items():
            if k not in allowed: continue
            try: vals[k]=float(v)
            except Exception: continue
        norm=math.sqrt(sum(v*v for v in vals.values()))
        maxnorm=max(float(self.config.get("policy.online_delta_max",0.1)),0.0)
        if norm>maxnorm+1e-12:
            out["reason"]="delta_norm_exceeds_bound"; out["norm"]=norm; return out
        bounds=self.config.get("policy.safe_parameter_bounds",{}) or {}
        projected={}
        for k,v in vals.items():
            b=bounds.get(k)
            if isinstance(b,(list,tuple)) and len(b)==2:
                v=max(float(b[0]),min(float(b[1]),v))
            projected[k]=v
        out.update({"accepted":True,"reason":"accepted","version":str(update["version"]),"projected_delta":projected,"norm":norm})
        return out
