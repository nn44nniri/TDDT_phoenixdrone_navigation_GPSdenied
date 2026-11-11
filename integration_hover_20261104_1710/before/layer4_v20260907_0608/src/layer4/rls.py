from __future__ import annotations
import numpy as np

class FFRLSCouplingPredictor:
    """Scalar per-edge FF-RLS with scenario/guidance targets and policy bounds (E4-24..E4-28)."""
    def __init__(self,config): self.config=config

    def initial(self,snapshot):
        p0=float(self.config.get("coupling.initial_covariance",10.0))
        return {e:{"theta":np.array([float(k)]),"P":np.eye(1)*p0} for e,k in snapshot.couplings.items()}

    def _bounds(self,snapshot,edge):
        slow=(snapshot.slow_policy or {}).get("coupling_bounds") or {}
        b=slow.get(edge,self.config.get("coupling.default_bounds",[0,1]))
        return float(b[0]),float(b[1])

    def step(self,state:dict, snapshot, trust:dict[str,float], topology:dict, *, scenario=None, step_index:int=0, return_details:bool=False):
        lam=float(self.config.get("coupling.forgetting_factor",0.98)); active=set(topology.get("active_edges",[])); out={}; details={}
        transient=(snapshot.guidance or {}).get("transient_coupling_delta") or {}
        scenario_delta=((getattr(scenario,"parameters",{}) or {}).get("coupling_delta") or {}) if scenario is not None else {}
        decay=float(self.config.get("coupling.scenario_delta_decay",1.0))**max(step_index,0)
        for edge,rec in state.items():
            src=edge.split("->",1)[0]; phi=np.array([max(float(trust.get(src,0)),1e-6)])
            base=float(snapshot.couplings.get(edge,rec["theta"][0]))
            target=base+float(transient.get(edge,0.0))+decay*float(scenario_delta.get(edge,0.0))
            lo,hi=self._bounds(snapshot,edge); target=float(np.clip(target,lo,hi))
            y=target*phi[0]
            P=rec["P"]; den=lam+float(phi.T@P@phi); L=(P@phi)/max(den,1e-12)
            theta=rec["theta"]+L*(y-float(phi.T@rec["theta"])); P=(P-np.outer(L,phi)@P)/lam
            theta=np.clip(theta,lo,hi); rec["theta"],rec["P"]=theta,P
            out[edge]=float(theta[0]) if edge in active else 0.0
            details[edge]={"target":target,"estimate":out[edge],"bounds":[lo,hi],"active":edge in active}
        return (state,out,details) if return_details else (state,out)
