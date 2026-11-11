from __future__ import annotations
import numpy as np
from .utils import finite_float


class ObjectiveEngine:
    """Complete Layer-4 seven-objective vector (E4-65..E4-70).

    Unlike the previous post-hoc-only implementation, every returned component is
    suitable for use inside the online optimizer.  The optimizer scalarizes the
    normalized vector only as a bounded search device; the final Pareto front remains
    non-dominated in the original seven-objective space.
    """
    KEYS=("mission","time","energy","risk","uncertainty","consistency","margin")
    def __init__(self,config,codec): self.config=config; self.codec=codec

    def target_ned(self,snapshot):
        # MESA position_ned_m is expressed in the absolute projected map frame
        # (Northing, Easting, -MSL).  Prefer the explicitly absolute target when
        # TDOC supplies it.  Legacy target_enu_m remains local-ENU and is retained
        # only as a backward-compatible fallback for callers whose state is local.
        target_env=snapshot.context.get("target_projected_enu_m") or snapshot.context.get("scenario_destination_projected_enu_m")
        absolute=bool(target_env)
        if not target_env:
            target_env=snapshot.context.get("scenario_destination_enu_m") or snapshot.context.get("target_enu_m") or {}
        tv=target_env.get("value") if isinstance(target_env,dict) else target_env
        if isinstance(tv,(list,tuple)) and len(tv)>=3:
            return np.asarray([tv[1],tv[0],-tv[2]],dtype=float)
        return None

    def compute(self,trajectory,covariances,network,actions,snapshot,scenario):
        gamma=float(self.config.get("objectives.discount",0.98)); H=len(actions); disc=np.asarray([gamma**i for i in range(H+1)])
        idx=self.codec.index; X=np.asarray(trajectory,dtype=float)
        pos=X[:,[idx[f"mesa_position_ned_m_{i}"] for i in range(3)]]
        target=self.target_ned(snapshot)
        if target is not None:
            dist=np.linalg.norm(pos-target.reshape(1,3),axis=1)
            stage_gain=float(self.config.get("objectives.mission_stage_gain",0.15))
            terminal_gain=float(self.config.get("objectives.mission_terminal_gain",1.0))
            mission=float(stage_gain*np.sum(disc[:len(dist)]*dist)/max(np.sum(disc[:len(dist)]),1e-12)+terminal_gain*dist[-1])
            remaining=float(dist[-1])
        else:
            step=np.linalg.norm(np.diff(pos,axis=0),axis=1) if len(pos)>1 else np.zeros(0)
            mission=float(np.sum(step)); remaining=0.0

        # A fixed horizon alone makes J_time action-invariant.  E4-65 requires a
        # predictive stage/terminal time cost, so include a bounded time-to-go proxy.
        vg=X[:,[idx[f"Vg_{i}"] for i in range(3)]]
        speed=np.linalg.norm(vg[:,:2],axis=1) if len(vg) else np.zeros(1)
        min_speed=float(self.config.get("objectives.time_speed_floor_m_s",1.0))
        mean_speed=max(float(np.mean(np.maximum(speed,min_speed))),min_speed)
        time=float(H*self.config.dt + remaining/mean_speed)

        U=np.asarray(actions,dtype=float); energy=float(np.sum(U*U))*self.config.dt
        prior=scenario.parameters.get("knowledge_prior") or {}; risk=float(finite_float(prior.get("risk_signal"),0.0))
        deg=0.0
        for v in (scenario.degradation or {}).values():
            deg+=float(v.get("severity",0.0)) if isinstance(v,dict) else float(v or 0.0)
        disturbance_name=str(scenario.parameters.get("disturbance_name") or "")
        risk+=float(self.config.get("objectives.degradation_risk_weight",1.0))*deg
        if disturbance_name not in ("","nominal"): risk+=float(self.config.get("objectives.non_nominal_disturbance_risk",0.15))

        # Continuous proximity-to-boundary risk makes J_risk optimizable rather than
        # a scenario-only constant.  Hard safety remains in FeasibilityEngine.
        amin=float(self.config.get("feasibility.min_airspeed_m_s",0.0)); amax=float(self.config.get("feasibility.max_airspeed_m_s",1e9))
        wmax=max(float(self.config.get("feasibility.max_abs_wind_m_s",1e9)),1e-6)
        air=X[:,idx["airspeed_m_s"]]
        wind=np.stack([X[:,idx[f"W_{i}"]] for i in range(3)],axis=1)
        ar=max(amax-amin,1e-6); center=0.5*(amin+amax); half=max(0.5*ar,1e-6)
        air_prox=np.maximum(0.0,np.abs(air-center)/half-0.75)/0.25
        wind_prox=np.maximum(0.0,np.linalg.norm(wind,axis=1)/wmax-0.75)/0.25
        dynamic_risk=float(np.mean(air_prox*air_prox+wind_prox*wind_prox))
        risk+=float(self.config.get("objectives.dynamic_risk_weight",0.5))*dynamic_risk

        uncertainty=float(sum(disc[i]*np.trace(covariances[i]) for i in range(min(len(covariances),len(disc)))))
        consistency=float(sum(disc[i]*float(network[i].get("J_C",0.0)) for i in range(min(len(network),len(disc)))))
        target_mu=float(self.config.get("consistency.mu_target",0.25)); margin=float(sum(disc[i]*max(0.0,target_mu-float(network[i].get("mu",target_mu)))**2 for i in range(min(len(network),len(disc)))))
        return {"mission":mission,"time":time,"energy":energy,"risk":risk,"uncertainty":uncertainty,"consistency":consistency,"margin":margin}

    def normalize(self,obj):
        out={}; eps=1e-12
        for k in self.KEYS:
            lo,hi=self.config.get(f"objectives.normalization.{k}",[0.0,1.0]); out[k]=(float(obj.get(k,0.0))-float(lo))/(float(hi)-float(lo)+eps)
        return out

    def scalarize(self,obj,weights=None):
        norm=self.normalize(obj); weights=weights or self.config.get("objectives.scalarization_weights",{}) or {}
        if not weights: weights={k:1.0/len(self.KEYS) for k in self.KEYS}
        w=np.asarray([max(0.0,float(weights.get(k,0.0))) for k in self.KEYS],dtype=float)
        if w.sum()<=0: w=np.ones(len(self.KEYS))/len(self.KEYS)
        else: w=w/w.sum()
        return float(sum(wi*norm[k] for wi,k in zip(w,self.KEYS))),{k:float(wi) for wi,k in zip(w,self.KEYS)}
