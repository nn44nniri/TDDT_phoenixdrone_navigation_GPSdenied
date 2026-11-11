from __future__ import annotations
from pathlib import Path
import json, numpy as np
from .vtol import VTOLProfile

class QLPVModel:
    """Small two-vertex polytopic qLPV predictor implementing E4-29..E4-34."""
    def __init__(self,config,codec):
        self.config=config; self.codec=codec
        p=config.package_root/config.get("qlpv.model_file")
        z=np.load(p)
        self.A=np.asarray(z["A"],dtype=float); self.Ba=np.asarray(z["Ba"],dtype=float); self.Bd=np.asarray(z["Bd"],dtype=float); self.b=np.asarray(z["b"],dtype=float)
        self.action_dim=self.Ba.shape[2]; self.disturbance_dim=self.Bd.shape[2]
        self.meta=json.loads((config.package_root/config.get("qlpv.metadata_file")).read_text(encoding="utf-8")); self.context={}
        self.vertical_collective_model=self._vertical_collective_linearization()
        # The runtime augmentation is part of the effective model identity used by
        # TDOC certificate hashing.  A certificate issued for the reference NPZ with
        # zero vertical authority must therefore never be reused for this model.
        self.meta["runtime_vertical_collective_model"]=dict(self.vertical_collective_model or {
            "available":False,"reason":"phoenix_source_dynamics_unavailable"})

    def _vertical_collective_linearization(self):
        """Build the vertical-flight input Jacobian from authoritative Phoenix source.

        The packaged reference qLPV has a zero ``delta_thrust_norm`` column for
        vertical position/velocity.  In takeoff/hover that makes the hard AGL
        horizon independent of every candidate action.  Add only the missing local
        Jacobian around physical hover force balance; do not edit the NPZ, invent an
        actuator command, or weaken Full-Fidelity verification.
        """
        profile=VTOLProfile(self.config)
        try:
            mass=float(profile._get("mass.empty_or_flight_mass_kg",None))
            gravity=float(profile._get("model.gravity_m_s2",None))
            motor_count=int(profile._get("propulsion.motor_count",None))
            k_t=float(profile._get("propulsion.source_motor_model.motor_constant_N_per_rad_s_squared",None))
            omega_max=float(profile._get("propulsion.source_motor_model.max_rot_velocity_rad_s",None))
            law_total=float(motor_count*k_t*omega_max*omega_max)
            configured_total=float(profile._get("propulsion.maximum_total_thrust_N",law_total))
            available_total=min(law_total,configured_total)
            if not all(np.isfinite(v) and v>0.0 for v in (mass,gravity,motor_count,k_t,omega_max,available_total)):
                raise ValueError("non_positive_source_parameter")
            equilibrium=float(np.sqrt(mass*gravity/available_total))
            if not np.isfinite(equilibrium) or not 0.0<equilibrium<=1.0:
                raise ValueError("hover_equilibrium_outside_normalized_motor_domain")
            # a_D = g - T/m and T=N*kT*(omega_max*u)^2, hence
            # d(a_D)/d(delta_u) at u_eq is negative.
            accel_sensitivity=float(-2.0*available_total*equilibrium/mass)
            order=list(self.meta.get("action_order") or [])
            action_index=order.index("delta_thrust_norm")
            return {
                "available":True,
                "schema":"Layer4-Phoenix-hover-collective-linearization-v1",
                "supported_phases":["vertical_takeoff","hover"],
                "source":profile.source,
                "mass_kg":mass,"gravity_m_s2":gravity,"motor_count":motor_count,
                "motor_k_t_N_per_rad_s2":k_t,"motor_omega_max_rad_s":omega_max,
                "available_total_thrust_N":available_total,
                "hover_equilibrium_motor_norm":equilibrium,
                "down_accel_sensitivity_m_s2_per_motor_norm":accel_sensitivity,
                "action_index":action_index,
                "action_semantics":"local motor-collective delta used only by qLPV prediction",
                "not_actuator_command":True,"full_fidelity_still_required":True,
            }
        except (TypeError,ValueError,KeyError):
            return None

    def set_context(self, context):
        self.context=dict(context or {})

    def schedule_vector(self,trust,couplings):
        health=float(np.mean(list(trust.values()))) if trust else 0.5
        k=float(np.mean(list(couplings.values()))) if couplings else 0.0
        wind=float(self.context.get("wind_speed_ms", self.context.get("boulder_reference_climate",{}).get("wind_speed_ms",0.0)) or 0.0)
        stage=str(self.context.get("stage", self.context.get("stage_id","cruise"))).lower()
        stage_code={"hover":0.0,"takeoff":0.15,"transition":0.45,"cruise":0.75,"landing":1.0}.get(stage,0.75)
        return np.asarray([health,k,np.clip(wind/30.0,0,1),stage_code],float)

    def weights(self,trust:dict[str,float],couplings:dict[str,float])->np.ndarray:
        rho=self.schedule_vector(trust,couplings); health,k,wind_n,stage_code=rho
        nominal=np.clip(0.58*health+0.27*(1-min(k,1.0))+0.10*(1-wind_n)+0.05*stage_code,0.0,1.0)
        return np.asarray([nominal,1-nominal],dtype=float)

    def matrices(self,trust:dict[str,float],couplings:dict[str,float],topology:dict):
        xi=self.weights(trust,couplings); A=np.tensordot(xi,self.A,axes=(0,0)); Ba=np.tensordot(xi,self.Ba,axes=(0,0)); Bd=np.tensordot(xi,self.Bd,axes=(0,0)); b=np.tensordot(xi,self.b,axes=(0,0))
        # Network-aware adjustment: active coupling strengths modulate velocity triangle relaxation.
        active=set(topology.get("active_edges",[])); idx=self.codec.index
        vg=[idx[f"Vg_{i}"] for i in range(3)]; va=[idx[f"Va_{i}"] for i in range(3)]; w=[idx[f"W_{i}"] for i in range(3)]
        kcw=float(couplings.get("MicroMetTwin->WingMPET",0.0)) if "MicroMetTwin->WingMPET" in active else 0.0
        kmw=float(couplings.get("MESA_NAV->WingMPET",0.0)) if "MESA_NAV->WingMPET" in active else 0.0
        for d in range(3):
            gain=np.clip(0.04+0.20*(kcw+kmw),0.02,0.28)
            A[vg[d],vg[d]]-=gain; A[vg[d],va[d]]+=0.55*gain; A[vg[d],w[d]]+=0.45*gain
        return A,Ba,Bd,b,xi

    def _apply_nominal_vertical_collective(self, raw, action):
        """Apply the source-derived vertical move to the nominal trajectory only.

        ``A/Ba`` in the packaged qLPV own the certified tube/error dynamics.  The
        missing Phoenix collective term is a deterministic nominal feed-forward
        model, not an identified error-feedback channel.  Keeping it here prevents
        the generic ``-k*Ba.T`` tube feedback from treating the large physical motor
        Jacobian as a calibrated feedback matrix while still making optimization and
        exact mean propagation responsive to the action.
        """
        y=np.asarray(raw,dtype=float).copy()
        phase=str(self.context.get("flight_mode") or self.context.get("stage") or self.context.get("stage_id") or "cruise").lower()
        vm=self.vertical_collective_model
        if phase not in ("vertical_takeoff","hover") or vm is None:
            return y,{"applied":False,"phase":phase}
        dt=float(self.config.dt); aidx=int(vm["action_index"]); delta=float(np.asarray(action,dtype=float)[aidx])
        sens=float(vm["down_accel_sensitivity_m_s2_per_motor_norm"])
        pd=self.codec.index["mesa_position_ned_m_2"]; vd=self.codec.index["Vg_2"]
        y[pd]+=0.5*dt*dt*sens*delta
        y[vd]+=dt*sens*delta
        return y,{"applied":True,"phase":phase,"delta_thrust_norm":delta,
            "down_accel_m_s2":sens*delta,
            "semantics":"deterministic nominal feed-forward; packaged qLPV retains tube/error dynamics",
            "not_actuator_command":True,"full_fidelity_still_required":True}

    def project_physical_state(self, x):
        """Project only intrinsic physical-domain invariants of the qLPV state.

        ``airspeed_m_s`` is a scalar speed, not a signed velocity component.  The
        affine reference model may otherwise cross zero under hypothetical negative
        delta-airspeed actions.  Projecting here (inside prediction dynamics) keeps
        optimization/objectives/UQ from evaluating a non-physical scalar speed; this
        is intentionally different from clipping a feasibility report afterwards.
        Signed vector components (Va_*, Vg_*, W_*) are left untouched.
        """
        y=np.asarray(x,dtype=float).copy()
        info={"active":False,"fields":{}}
        i=self.codec.index.get("airspeed_m_s")
        if i is not None and i < y.size and np.isfinite(y[i]):
            raw=float(y[i]); projected=max(raw,0.0)
            y[i]=projected
            info["fields"]["airspeed_m_s"]={"raw":raw,"projected":projected,"nonnegative":True}
            info["active"]=bool(raw < 0.0)
        return y,info

    def step(self,x,a,d,trust,couplings,topology):
        A,Ba,Bd,b,xi=self.matrices(trust,couplings,topology)
        raw=A@np.asarray(x)+Ba@np.asarray(a)+Bd@np.asarray(d)+b
        raw,vertical=self._apply_nominal_vertical_collective(raw,a)
        y,projection=self.project_physical_state(raw)
        return y, {"xi":xi.tolist(),"physical_state_projection":projection,
                   "nominal_vertical_collective":vertical}

    @staticmethod
    def _topology_at(topology,l):
        if isinstance(topology,(list,tuple)) and topology:
            return topology[min(l,len(topology)-1)]
        return topology

    def rollout(self,x0,actions,disturbances,trust_seq,coupling_seq,topology):
        x=np.asarray(x0,dtype=float); xs=[x.copy()]; meta=[]
        for l,(a,d) in enumerate(zip(actions,disturbances)):
            t=trust_seq[min(l,len(trust_seq)-1)]; k=coupling_seq[min(l,len(coupling_seq)-1)]; g=self._topology_at(topology,l)
            x,m=self.step(x,a,d,t,k,g); xs.append(x.copy()); meta.append(m)
        return np.asarray(xs),meta
