from __future__ import annotations
import math
from .utils import clip01, finite_float

class BetaTrustPredictor:
    """Discounted fractional Beta evidence with horizon freshness/uncertainty prediction (E4-18..E4-23)."""
    def __init__(self,config): self.config=config

    def initial(self,snapshot):
        out={}
        a0=float(self.config.get("trust.alpha0",2)); b0=float(self.config.get("trust.beta0",1))
        for twin in snapshot.weights:
            w=clip01(snapshot.weights.get(twin,0.5)); strength=max(a0+b0,2.0)
            out[twin]=[max(w*strength,1e-3),max((1-w)*strength,1e-3)]
        return out

    def _freshness_lambda(self,twin,r):
        age=max(0.0,finite_float(r.get("age_s"),0.0)); fresh=clip01(finite_float(r.get("freshness"),1.0))
        if age>1e-6 and 0.0<fresh<1.0:
            return max(0.0,-math.log(max(fresh,1e-12))/age)
        defaults=self.config.get("trust.freshness_decay_per_s",{}) or {}
        return float(defaults.get(twin,defaults.get("default",0.0)))

    @staticmethod
    def _degradation_components(value):
        if isinstance(value,dict):
            return (float(value.get("quality",value.get("severity",0.0))),
                    float(value.get("validity",0.0)),float(value.get("reliability",value.get("severity",0.0))),
                    float(value.get("uncertainty",value.get("severity",0.0))))
        v=float(value or 0.0); return v,0.0,v,v

    def step(self,state:dict, snapshot, degradation:dict|None=None, *, step_index:int=0, dt_s:float=1.0, return_details:bool=False):
        lam=float(self.config.get("trust.forgetting_factor",0.96)); eps=float(self.config.get("trust.epsilon_u",1e-6)); ref=float(self.config.get("trust.eta_reference",1.0))
        reps=snapshot.std.get("representations") or {}; degradation=degradation or {}; raw={}; details={}
        growth=float(self.config.get("trust.uncertainty_growth_per_s",0.01))
        for twin,(alpha,beta) in state.items():
            r=reps.get(twin,{})
            dq,dv,dr,du=self._degradation_components(degradation.get(twin,0.0))
            q=clip01(finite_float(r.get("quality"),1.0)*(1-dq))
            v=clip01(finite_float(r.get("validity"),1.0)*(1-dv))
            rel=clip01(finite_float(r.get("reliability"),1.0)*(1-dr))
            lambda_f=self._freshness_lambda(twin,r)
            fresh0=clip01(finite_float(r.get("freshness"),1.0))
            fresh=clip01(fresh0*math.exp(-lambda_f*max(0,step_index+1)*dt_s))
            u0=max(0.0,finite_float(r.get("normalized_uncertainty"),0.0))
            u=max(0.0,u0*(1+du)+growth*max(0,step_index+1)*dt_s)
            eta=q*v*rel*fresh/(eps+u); e=clip01(eta/max(ref,eps))
            alpha=lam*alpha+e; beta=lam*beta+(1-e); state[twin]=[alpha,beta]; raw[twin]=alpha/(alpha+beta)
            details[twin]={"Q":q,"V":v,"R":rel,"F":fresh,"U":u,"eta":eta,"evidence":e}
        s=sum(raw.values()) or 1.0
        weights={k:v/s for k,v in raw.items()}
        bounds=(snapshot.slow_policy or {}).get("weight_bounds") or {}
        if bounds:
            lo={k:float(bounds.get(k,[0.0,1.0])[0]) for k in weights}
            hi={k:float(bounds.get(k,[0.0,1.0])[1]) for k in weights}
            weights={k:min(hi[k],max(lo[k],float(v))) for k,v in weights.items()}
            for _ in range(12):
                total=sum(weights.values()); delta=1.0-total
                if abs(delta)<=1e-12: break
                if delta>0:
                    cap={k:max(0.0,hi[k]-weights[k]) for k in weights}; csum=sum(cap.values())
                    if csum<=1e-15: break
                    for k in weights: weights[k]+=delta*cap[k]/csum
                else:
                    cap={k:max(0.0,weights[k]-lo[k]) for k in weights}; csum=sum(cap.values())
                    if csum<=1e-15: break
                    for k in weights: weights[k]+=delta*cap[k]/csum
                weights={k:min(hi[k],max(lo[k],v)) for k,v in weights.items()}
        return (state,weights,details) if return_details else (state,weights)
