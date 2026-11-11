from __future__ import annotations
from pathlib import Path
from typing import Any
import numpy as np
from .config import Layer5Config
from .models import Layer5Candidate, GateRecord
from .phoenix import PhoenixSpecialization
from .utils import clip

class QLPVCorrectionKernel:
    """Verification-preserving risk-aware qLPV correction around a Layer-4 candidate.

    A non-zero correction is accepted only when (1) the Phoenix qLPV and actuator
    calibration gates pass, (2) the correction/state tube remains inside configured
    bounds, and (3) an explicit chance certificate remains within the Layer-5 risk
    budget.  Otherwise the verified Layer-4 nominal sequence is preserved exactly.
    """
    def __init__(self, config:Layer5Config, phoenix:PhoenixSpecialization):
        self.config=config; self.phoenix=phoenix; self.model=None; self.meta={}; self.error=None
        f=config.resolve(config.get("qlpv.model_file",""))
        if f: self._load(f)
    def _load(self,path:Path):
        try:
            z=np.load(path,allow_pickle=False); A=np.asarray(z["A"],float); B=np.asarray(z["B"],float); E=np.asarray(z["E"],float) if "E" in z else None
            calibrated=bool(int(np.asarray(z["calibrated"]).reshape(-1)[0])) if "calibrated" in z else False
            vertices=np.asarray(z["schedule_vertices"],float).reshape(-1) if "schedule_vertices" in z else None
            profile_hash=str(np.asarray(z["phoenix_profile_sha256"]).reshape(-1)[0]) if "phoenix_profile_sha256" in z else None
            airframe_hash=str(np.asarray(z["airframe_binding_sha256"]).reshape(-1)[0]) if "airframe_binding_sha256" in z else None
            if A.ndim==2: A=A[None,...]
            if B.ndim==2: B=B[None,...]
            if E is not None and E.ndim==2: E=E[None,...]
            if B.shape[-1]!=4: raise ValueError("Phoenix logical qLPV B must have 4 inputs")
            if A.shape[0]!=B.shape[0]: raise ValueError("qLPV vertex count mismatch")
            if vertices is not None and len(vertices)!=A.shape[0]: raise ValueError("schedule_vertices count mismatch")
            self.model={"A":A,"B":B,"E":E,"vertices":vertices,"calibrated":calibrated,"path":str(path),
                        "phoenix_profile_sha256":profile_hash,"airframe_binding_sha256":airframe_hash}
        except Exception as e: self.error=repr(e)
    def status(self)->dict[str,Any]:
        gates=self.phoenix.correction_validation_gates(); require_binding=bool(self.config.get("qlpv.require_airframe_binding_match",True))
        binding_match=bool(self.model and self.model.get("phoenix_profile_sha256")==self.phoenix.hash and self.model.get("airframe_binding_sha256")==self.phoenix.airframe_binding_hash())
        auth=bool(self.model and self.model["calibrated"] and gates["mixer"] and gates["servo_dynamics"] and gates["esc_mapping"] and (binding_match or not require_binding))
        return {"model_loaded":self.model is not None,"calibrated":bool(self.model and self.model["calibrated"]),"error":self.error,
                "vertices":int(self.model["A"].shape[0]) if self.model else 0,"airframe_binding_required":require_binding,"airframe_binding_match":binding_match,
                "model_source":self.model.get("source") if self.model else None,"model_scope":self.model.get("scope") if self.model else None,
                "simulation_only_model":bool(self.model and self.model.get("simulation_only")),
                "phoenix_validation_gates":gates,"nonzero_correction_authorized":auth}

    def bind_runtime_actuator_model(self, certificate:dict[str,Any]|None)->dict[str,Any]:
        """Build a calibrated local actuator qLPV realization from accepted SITL ID evidence.

        Scope is deliberately limited to the Layer-5 correction dynamics around an already
        Layer-4-verified trajectory.  It is not an aircraft-identification certificate and
        cannot authorize direct PWM or field/real-aircraft claims.
        """
        c=dict(certificate or {})
        if not bool(self.config.get("qlpv.allow_runtime_sitl_actuator_model",True)):
            return {"ok":False,"reason":"runtime_sitl_actuator_model_disabled"}
        if str(c.get("schema") or "")!="PTLIB-Phoenix-control-commission-v2" or not bool(c.get("ok")):
            return {"ok":False,"reason":"runtime_actuator_identification_certificate_v2_required"}
        sd=c.get("servo_dynamics") or {}; esc=c.get("esc_mapping") or {}
        if not (bool(sd.get("identified")) and bool(sd.get("measured")) and str(sd.get("scope") or "")=="effective_sitl_command_path"):
            return {"ok":False,"reason":"servo_runtime_identification_missing"}
        if not (bool(esc.get("identified")) and bool(esc.get("measured")) and str(esc.get("scope") or "")=="effective_sitl_command_path"):
            return {"ok":False,"reason":"esc_runtime_mapping_identification_missing"}
        tau=[float(x) for x in sd.get("tau_s",[]) or []]; rates=[float(x) for x in sd.get("rate_limit_rad_s",[]) or []]
        if len(tau)<2 or len(rates)<2 or not all(np.isfinite(x) and x>0 for x in tau[:2]+rates[:2]):
            return {"ok":False,"reason":"servo_runtime_identification_invalid"}
        M=np.asarray(self.phoenix.runtime_mixer_matrix_rad,dtype=float) if self.phoenix.runtime_mixer_matrix_rad is not None else None
        if M is None or M.shape!=(2,2) or not np.isfinite(M).all():
            return {"ok":False,"reason":"runtime_mixer_matrix_missing"}
        dt=float(self.config.get("qlpv.dt_s",0.1)); dt=max(1e-3,dt)
        # Four local correction states: two physical elevon errors [rad] and two
        # normalized twin-motor command errors.  Motor time constants remain source-
        # bound Phoenix parameters; ESC command gains come from the measured SITL map.
        aL=float(np.exp(-dt/max(tau[0],1e-6))); aR=float(np.exp(-dt/max(tau[1],1e-6)))
        mtau=max(1e-6,0.5*(float(self.phoenix.tau_up)+float(self.phoenix.tau_down)))
        am=float(np.exp(-dt/mtau))
        A=np.diag([aL,aR,am,am]).astype(float)
        B=np.zeros((4,4),dtype=float)
        B[0,0:2]=(1.0-aL)*M[0,:]
        B[1,0:2]=(1.0-aR)*M[1,:]
        gL=float(esc.get("left_gain_motor_norm_per_request_norm")); gR=float(esc.get("right_gain_motor_norm_per_request_norm"))
        B[2,2]=(1.0-am)*gL; B[3,2]=(1.0-am)*gR
        # Differential-thrust yaw remains zero unless the existing Phoenix gate already
        # validates it; never invent yaw-to-motor authority.
        if self.phoenix.dt_validated:
            g=float(self.config.get("phoenix.differential_thrust.gain",self.phoenix.rudd_dt_gain) or 0.0)
            B[2,3]=-(1.0-am)*g; B[3,3]=(1.0-am)*g
        self.model={"A":A[None,...],"B":B[None,...],"E":None,"vertices":None,"calibrated":True,
                    "path":None,"phoenix_profile_sha256":self.phoenix.hash,
                    "airframe_binding_sha256":self.phoenix.airframe_binding_hash(),
                    "source":"runtime_sitl_actuator_identification_v1",
                    "scope":"layer5_local_actuator_correction_only",
                    "state_order":["delta_L_error_rad","delta_R_error_rad","motor_L_error_norm","motor_R_error_norm"],
                    "simulation_only":True}
        self.error=None
        return {"ok":True,"source":self.model["source"],"scope":self.model["scope"],
                "state_order":list(self.model["state_order"]),"dt_s":dt,
                "simulation_only":True,"direct_pwm":False}

    def _matrices(self,schedule:dict[str,Any]):
        A=self.model["A"]; B=self.model["B"]; E=self.model["E"]; nv=A.shape[0]
        if nv==1: return A[0],B[0],E[0] if E is not None else None,{"vertices":[0],"weights":[1.0]}
        vs=self.model.get("vertices")
        if vs is None: raise ValueError("multi-vertex qLPV requires schedule_vertices")
        x=float(schedule.get("lambda_transition",1.0)); order=np.argsort(vs); vs=vs[order]; A=A[order]; B=B[order]; E=E[order] if E is not None else None
        if x<=vs[0]: return A[0],B[0],E[0] if E is not None else None,{"vertices":[int(order[0])],"weights":[1.0]}
        if x>=vs[-1]: return A[-1],B[-1],E[-1] if E is not None else None,{"vertices":[int(order[-1])],"weights":[1.0]}
        j=int(np.searchsorted(vs,x)); i=j-1; w=(x-vs[i])/max(vs[j]-vs[i],1e-12)
        Am=(1-w)*A[i]+w*A[j]; Bm=(1-w)*B[i]+w*B[j]; Em=((1-w)*E[i]+w*E[j]) if E is not None else None
        return Am,Bm,Em,{"vertices":[int(order[i]),int(order[j])],"weights":[float(1-w),float(w)]}
    def _chance_gate(self,c:Layer5Candidate,nonzero:bool)->dict[str,Any]:
        alpha=float(self.config.get("admissibility.alpha_risk",0.1)); cert=(c.provenance or {}).get("qlpv_chance_certificate") or (c.verification or {}).get("qlpv_chance_certificate")
        if not nonzero:
            return {"ok":c.violation_probability<=alpha,"source":"Layer4_verified_nominal_risk","epsilon_sum":c.violation_probability,"alpha_risk":alpha}
        if not isinstance(cert,dict): return {"ok":False,"source":"missing_nonzero_correction_certificate","epsilon_sum":None,"alpha_risk":alpha}
        eps=cert.get("epsilon") or cert.get("epsilons")
        if not isinstance(eps,(list,tuple)) or not eps: return {"ok":False,"source":"invalid_nonzero_correction_certificate","epsilon_sum":None,"alpha_risk":alpha}
        try: s=sum(float(x) for x in eps)
        except Exception: return {"ok":False,"source":"invalid_nonzero_correction_certificate","epsilon_sum":None,"alpha_risk":alpha}
        return {"ok":bool(cert.get("verified") is True and s<=alpha),"source":"explicit_qlpv_chance_certificate","epsilon_sum":s,"alpha_risk":alpha,"certificate_version":cert.get("version")}
    def solve(self,c:Layer5Candidate,context:dict[str,Any]|None=None,guidance:dict[str,Any]|None=None)->dict[str,Any]:
        if not c.logical_actions: return {"ok":False,"reason":"no_phoenix_logical_sequence","corrected_actions":[],"delta_u":[],"vp":GateRecord(False,["no_phoenix_logical_sequence"]).to_dict()}
        U=np.asarray(c.logical_actions,float); delta=np.zeros_like(U); errors=[]; schedules=[]; state_errors=[]
        authorized=bool(self.status().get("nonzero_correction_authorized"))
        physical_states=c.raw.get("predicted_physical_states") or []
        H=min(len(U),int(self.config.get("edge.horizon_max",12)))
        if authorized:
            n=self.model["A"].shape[-1]; e0=np.asarray((c.provenance or {}).get("qlpv_initial_error",np.zeros(n)),float).reshape(-1)
            if e0.size!=n: return {"ok":False,"reason":"qlpv_initial_error_dimension","corrected_actions":[],"delta_u":[]}
            bound=float(self.config.get("qlpv.correction_bound",0.10)); step=float(self.config.get("qlpv.step_size",0.08)); iters=int(self.config.get("qlpv.max_iterations",20)); du=np.zeros((H,4),float)
            # bounded projected-gradient using horizon-varying scheduled matrices; disturbance is explicit only.
            D=(c.provenance or {}).get("qlpv_disturbance") or []
            for _ in range(max(1,iters)):
                e=[e0]; mats=[]
                for k in range(H):
                    st=physical_states[k] if k<len(physical_states) and isinstance(physical_states[k],dict) else {}
                    sch=self.phoenix.schedule(st,context,guidance,c.network_trajectory[k] if k<len(c.network_trajectory) else None); Am,Bm,Em,blend=self._matrices(sch); mats.append((Am,Bm,Em,sch,blend))
                    dk=np.asarray(D[k],float).reshape(-1) if k<len(D) else None
                    term=(Em@dk if Em is not None and dk is not None and Em.shape[1]==dk.size else 0.0)
                    e.append(Am@e[-1]+Bm@du[k]+term)
                lam=np.zeros(n); grad=np.zeros_like(du)
                for k in range(H-1,-1,-1):
                    Am,Bm,_,_,_=mats[k]; lam=2*e[k+1]+Am.T@lam; grad[k]=0.2*du[k]+Bm.T@lam
                du=np.clip(du-step*grad,-bound,bound)
            delta[:H]=du; state_errors=[x.tolist() for x in e]
            schedules=[{"schedule":m[3],"blend":m[4]} for m in mats]
        else:
            schedules=[{"schedule":self.phoenix.schedule(physical_states[k] if k<len(physical_states) and isinstance(physical_states[k],dict) else {},context,guidance,c.network_trajectory[k] if k<len(c.network_trajectory) else None),"blend":{"vertices":[0],"weights":[1.0]}} for k in range(H)]
        corrected=U+delta; corrected[:,0]=np.clip(corrected[:,0],-1,1); corrected[:,1]=np.clip(corrected[:,1],-1,1); corrected[:,2]=np.clip(corrected[:,2],0,1); corrected[:,3]=np.clip(corrected[:,3],-1,1)
        nonzero=bool(np.linalg.norm(delta)>1e-12); tube_bound=float(self.config.get("qlpv.state_error_tube_norm_max",1e9)); max_en=max([float(np.linalg.norm(x)) for x in state_errors] or [0.0])
        if max_en>tube_bound: errors.append("state_error_tube_violation")
        if np.max(np.abs(delta))>float(self.config.get("qlpv.correction_bound",0.10))+1e-12: errors.append("control_correction_tube_violation")
        phx=self.phoenix.physical_trajectory_feasibility(corrected.tolist(),correction_requested=nonzero,states=physical_states if isinstance(physical_states,list) else None); errors.extend(phx["reasons"])
        chance=self._chance_gate(c,nonzero); 
        if not chance["ok"]: errors.append("chance_constraint_certificate_failed")
        vp=GateRecord(not errors,sorted(set(errors)),{"nonzero_correction":nonzero,"correction_authorized":authorized,"max_state_error_norm":max_en,"state_tube_bound":tube_bound,"chance":chance,"physical_tube":phx})
        return {"ok":vp.ok,"solver_mode":"calibrated_scheduled_projected_qLPV_correction" if authorized else "verification_preserving_zero_correction",
                "corrected_actions":corrected.tolist(),"delta_u":delta.tolist(),"state_error":state_errors,"scheduling":schedules,"vp":vp.to_dict(),"chance":chance,"physical_tube":phx,"model":self.status(),"pareto_generation":False}
