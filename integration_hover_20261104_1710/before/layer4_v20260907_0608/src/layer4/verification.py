from __future__ import annotations
import copy, hashlib, json, sys, time
from pathlib import Path
from typing import Any
import numpy as np
from .vtol import VTOLProfile
from .agl import resolve_operational_agl
from .native_controller_contract import readiness, model_ff_allowed, model_prediction_scope


class FullFidelityVerifier:
    """Selective full-fidelity adapter. verified=True requires actual Layer3FMURuntime + all verification gates."""
    def __init__(self,config,codec,*,native_reference_service=None):
        self.config=config; self.codec=codec; self.vtol=VTOLProfile(config)
        self.native_reference_service=native_reference_service

    def verify_native_reference(self,request):
        """Explicit R7.05 reference scope, with no operational FF promotion."""
        from .native_reference_verification import verify_reference
        return verify_reference(self,request,self.native_reference_service)

    _OUTCOME_SCHEMA="Layer4-FF-verification-outcome-v1"
    _OUTCOME_STATES={"NOT_ATTEMPTED","ATTEMPTED_AND_REJECTED","ATTEMPTED_AND_VERIFIED","EXCEPTION"}

    @staticmethod
    def _error_id(candidate_id:str, reason:str, error:str)->str:
        payload=f"{candidate_id}|{reason}|{error}".encode("utf-8",errors="replace")
        return "fferr-"+hashlib.sha256(payload).hexdigest()[:16]

    @classmethod
    def outcome(cls,status:str,reason:str,*,reason_codes=None,verified=False,attempted=False,executed=False,**extra):
        if status not in cls._OUTCOME_STATES:
            raise ValueError(f"invalid Full-Fidelity outcome status: {status}")
        codes=[]
        for item in list(reason_codes or [reason]):
            text=str(item)
            if text and text not in codes: codes.append(text)
        out={
            "schema":cls._OUTCOME_SCHEMA,
            "outcome_status":status,
            "verified":bool(verified),
            "candidate_verified":bool(verified),
            "attempted":bool(attempted),
            "executed":bool(executed),
            "reason":str(reason),
            "reason_codes":codes,
            # Legacy alias retained for existing consumers/reports.
            "verification_reasons":list(codes),
        }
        out.update(extra)
        return out

    def _available(self):
        l3=self.config.env_path("TDDT_LAYER3_ROOT"); twins=self.config.env_path("TDDT_TWINS_ROOT"); climate=self.config.env_path("TDDT_CLIMATE_ROOT"); tdoc=self.config.env_path("TDDT_TDOC_ROOT")
        ok=bool(l3 and twins and climate and tdoc and (l3/'src/layer3').exists() and (twins/'fmus/MESA_NAV.fmu').exists() and (climate/'python').exists() and tdoc.exists())
        return ok,{"layer3":str(l3) if l3 else None,"twins":str(twins) if twins else None,"climate":str(climate) if climate else None,"tdoc":str(tdoc) if tdoc else None}

    def _normalize(self,obj):
        out={}; eps=1e-12
        for k,v in obj.items():
            lo,hi=self.config.get(f"objectives.normalization.{k}",[0.0,1.0]); out[k]=(float(v)-float(lo))/(float(hi)-float(lo)+eps)
        return out

    def _full_objectives(self,rows,candidate,snapshot,scenario):
        H=max(len(rows),1); gamma=float(self.config.get("objectives.discount",0.98)); disc=np.asarray([gamma**i for i in range(H+1)])
        target_env=snapshot.context.get("target_projected_enu_m") or snapshot.context.get("scenario_destination_projected_enu_m") or snapshot.context.get("scenario_destination_enu_m") or snapshot.context.get("target_enu_m") or {}; tv=target_env.get("value") if isinstance(target_env,dict) else target_env
        pos=[]; risks=[]; uncs=[]; cons=[]; margins=[]
        target_mu=float(self.config.get("consistency.mu_target",0.25))
        for r in rows:
            s=r.get("snapshot",{}) or {}; reps=((s.get("std") or {}).get("representations") or {})
            p=((reps.get("MESA_NAV") or {}).get("canonical_state") or {}).get("position_ned_m")
            if isinstance(p,(list,tuple)) and len(p)>=3: pos.append(np.asarray(p[:3],dtype=float))
            risks.append(float(((s.get("guidance") or {}).get("risk_signal") or 0.0)))
            cov=np.asarray(((s.get("uncertainty_td") or {}).get("covariance") or []),dtype=float); uncs.append(float(np.trace(cov)) if cov.ndim==2 else 0.0)
            cons.append(float(s.get("mismatch") or 0.0)); mu=s.get("consistency_margin"); margins.append(max(0.0,target_mu-float(mu if mu is not None else -1.0))**2)
        if pos and isinstance(tv,(list,tuple)) and len(tv)>=3:
            target=np.asarray([tv[1],tv[0],-tv[2]],dtype=float); mission=float(np.linalg.norm(pos[-1]-target))
        elif len(pos)>=2: mission=float(np.linalg.norm(pos[-1]-pos[0]))
        else: mission=float(candidate.objective_vector.get("mission",0.0))
        deg=0.0
        if scenario is not None:
            for v in (scenario.degradation or {}).values(): deg+=float(v.get("severity",0.0)) if isinstance(v,dict) else float(v or 0.0)
        risk=(float(np.mean(risks)) if risks else 0.0)+float(self.config.get("objectives.degradation_risk_weight",1.0))*deg
        uncertainty=float(sum(disc[min(i,len(disc)-1)]*v for i,v in enumerate(uncs)))
        consistency=float(sum(disc[min(i,len(disc)-1)]*v for i,v in enumerate(cons)))
        margin=float(sum(disc[min(i,len(disc)-1)]*v for i,v in enumerate(margins)))
        return {"mission":mission,"time":float(len(candidate.actions)*self.config.dt),"energy":float(candidate.objective_vector.get("energy",0.0)),"risk":risk,"uncertainty":uncertainty,"consistency":consistency,"margin":margin}


    def _full_feasibility(self,rows,candidate,snapshot,scenario,*,progress_step_count=None):
        """Assess hard physical/mission/action gates from full Layer-3 replay output.

        The full-fidelity chance indicator is derived from this replay rather than
        reusing surrogate/qLPV chance risk (Formalism E4-81).
        """
        reasons=[]; details=[]
        base_ctx=snapshot.context or {}
        sc_constraints=(getattr(scenario,"constraints",{}) or {}) if scenario is not None else {}
        def limit(name,default):
            v=sc_constraints.get(name,default)
            try: return float(v)
            except Exception: return float(default)
        base_agl_res=resolve_operational_agl(snapshot,self.config)
        phase_entry_agl=base_agl_res.get("altitude_agl_m") if base_agl_res.get("ok") else None
        snap_std=getattr(snapshot,"std",{}) or {}
        base_reps=(snap_std.get("representations") or {}) if isinstance(snap_std,dict) else {}
        phase_entry_air=((base_reps.get("WingMPET") or {}).get("canonical_state") or {}).get("airspeed_m_s")
        try: phase_entry_air=max(float(phase_entry_air),0.0) if phase_entry_air is not None else 0.0
        except Exception: phase_entry_air=0.0
        # A commit-segment replay can contain only the committable first move while
        # still belonging to a longer forecast horizon.  Keep the phase-corridor
        # progress on that original horizon instead of treating the one commit row
        # as the terminal forecast row.  The default preserves the original API.
        corridor_step_count=max(int(progress_step_count or len(rows)),1)
        for row_index,r in enumerate(rows,start=1):
            sd=r.get("snapshot",{}) or {}; reps=((sd.get("std") or {}).get("representations") or {}); ctx=(sd.get("context") or base_ctx or {})
            mode=str(ctx.get("flight_mode") or ctx.get("stage") or ctx.get("stage_id") or "cruise").lower(); lim=self.vtol.limits(mode)
            rr=[]
            wing=((reps.get("WingMPET") or {}).get("canonical_state") or {}); climate=((reps.get("MicroMetTwin") or {}).get("canonical_state") or {})
            air=wing.get("airspeed_m_s")
            air_corridor=None
            if air is not None:
                if mode == "transition_forward":
                    terminal_default=float(lim.get("terminal_min_airspeed_m_s",self.config.get("vtol.min_airspeed_by_mode_m_s.transition",3.0)))
                    terminal_override=sc_constraints.get("min_airspeed_m_s") if "min_airspeed_m_s" in sc_constraints else terminal_default
                    air_corridor=self.vtol.airspeed_corridor(
                        mode,
                        phase_entry_air,
                        step_index=row_index,
                        step_count=corridor_step_count,
                        context={**base_ctx,**ctx},
                        terminal_min_override_m_s=terminal_override,
                    )
                    lo_air=float(air_corridor["lower_m_s"]); hi_air=min(limit("max_airspeed_m_s",lim["max_airspeed_m_s"]),float(air_corridor["upper_m_s"]))
                    if not bool(air_corridor.get("reachable",True)): rr.append("transition_forward_acceleration_envelope")
                else:
                    lo_air=limit("min_airspeed_m_s",lim["min_airspeed_m_s"]); hi_air=limit("max_airspeed_m_s",lim["max_airspeed_m_s"])
                    air_corridor=self.vtol.airspeed_corridor(mode,phase_entry_air,step_index=row_index,step_count=corridor_step_count,context={**base_ctx,**ctx})
                if not (float(lo_air) <= float(air) <= float(hi_air)): rr.append("airspeed")
            w=climate.get("wind_ned_m_s")
            if isinstance(w,(list,tuple)) and len(w)>=3 and float(np.linalg.norm(np.asarray(w[:3],float)))>limit("max_wind_m_s",lim["max_wind_m_s"]): rr.append("wind")
            row_agl_res=resolve_operational_agl(sd,self.config)
            agl=row_agl_res.get("altitude_agl_m") if row_agl_res.get("ok") else None
            if agl is None:
                rr.append("AGL_UNAVAILABLE")
            else:
                agl_f=float(agl); entry=phase_entry_agl if phase_entry_agl is not None else agl_f
                corridor=self.vtol.terrain_corridor(mode,entry,step_index=max(int(r.get("step") or 1)-1,0),step_count=corridor_step_count,context={**base_ctx,**ctx})
                lower=float(corridor["lower_m"]); upper=corridor.get("upper_m"); policy=corridor.get("policy")
                if agl_f < lower:
                    rr.append({"GROUND_REFERENCED_TAKEOFF_CORRIDOR":"takeoff_ground_corridor","HOVER_TARGET_AGL_CORRIDOR":"hover_agl_corridor","TERMINAL_GROUND_CORRIDOR":"landing_ground_penetration","ENROUTE_TERRAIN_FLOOR":"terrain_clearance"}.get(policy,"terrain_clearance"))
                if upper is not None and agl_f > float(upper):
                    rr.append({"GROUND_REFERENCED_TAKEOFF_CORRIDOR":"takeoff_ground_corridor","HOVER_TARGET_AGL_CORRIDOR":"hover_agl_corridor","TERMINAL_GROUND_CORRIDOR":"landing_corridor_upward_excursion"}.get(policy,"terrain_clearance"))
            building=float(ctx.get("building_height_m",0.0) or 0.0)
            if agl is not None and building>0 and float(agl)-building<limit("min_obstacle_clearance_m",self.config.get("feasibility.min_obstacle_clearance_m",10)): rr.append("obstacle_clearance")
            risk=float(((sd.get("guidance") or {}).get("risk_signal") or 0.0))
            risk_max=sc_constraints.get("risk_max",self.config.get("feasibility.risk_max",None))
            if risk_max is not None and risk>float(risk_max): rr.append("mission_risk_threshold")
            details.append({"step":r.get("step"),"ok":not rr,"reasons":rr,"mode":mode,"terrain_policy":(corridor if agl is not None else None),"airspeed_policy":air_corridor})
            reasons.extend(rr)
        A=np.asarray(candidate.actions,dtype=float); lo=np.asarray(self.config.get("qlpv.action_lower"),dtype=float); hi=np.asarray(self.config.get("qlpv.action_upper"),dtype=float)
        action_safe=bool(not A.size or (np.all(A>=lo-1e-9) and np.all(A<=hi+1e-9)))
        if not action_safe: reasons.append("action_bounds")
        safe=not reasons
        pviol=0.0 if safe else 1.0
        return {"safe":safe,"physical_mission_safe":safe,"action_safe":action_safe,"reasons":sorted(set(reasons)),"steps":details,"P_viol_full":pviol}

    def verify(self,candidate,snapshot,scenario=None)->dict[str,Any]:
        cid=str(getattr(candidate,"candidate_id","candidate"))
        native=readiness(snapshot,candidate,self.config)
        if native["native_pilot_execution_requested"] and not model_ff_allowed(snapshot,self.config):
            return self.outcome(
                "NOT_ATTEMPTED",native["reason"],reason_codes=[native["reason"]],
                verification_origin="UNVERIFIED",native_control_binding=native,
                native_controller_certified=False,flight_authorized=False,
                verification_scope="NATIVE_PILOT_CONTROLLER_REQUIRED_NOT_AVAILABLE",
            )
        if not bool(self.config.get("verification.enabled",True)):
            return self.outcome("NOT_ATTEMPTED","verification_disabled",reason_codes=["verification_disabled"])
        agl_res=resolve_operational_agl(snapshot,self.config)
        if not agl_res.get("ok"):
            return self.outcome(
                "NOT_ATTEMPTED","AGL_UNAVAILABLE",reason_codes=["AGL_UNAVAILABLE",str(agl_res.get("reason") or "agl_authority_unavailable")],
                agl_authority=agl_res.get("authority") or {},
                agl_unavailable_reason=agl_res.get("reason"),
                safety_invariant="Full-Fidelity verification requires authoritative operational AGL; no permissive fallback",
            )
        ok,paths=self._available()
        if not ok:
            return self.outcome("NOT_ATTEMPTED","external_full_fidelity_dependencies_unavailable",reason_codes=["external_full_fidelity_dependencies_unavailable"],paths=paths)
        l3=Path(paths["layer3"]); sys.path.insert(0,str(l3/'src')) if str(l3/'src') not in sys.path else None
        try:
            from layer3 import Layer3Config, Layer3FMURuntime, RuntimeRequest  # type: ignore
        except Exception as e:
            err=repr(e)
            return self.outcome("EXCEPTION","layer3_import_failed",reason_codes=["layer3_import_failed"],error=err,error_id=self._error_id(cid,"layer3_import_failed",err))
        template=Path(self.config.package_root/self.config.get("verification.request_template")); base=json.loads(template.read_text(encoding="utf-8"))
        cfg=Layer3Config.from_yaml(l3/'configs/configuration_boulder.yaml'); rows=[]; t0=time.perf_counter()
        try:
            with Layer3FMURuntime(cfg) as rt:
                for l,x in enumerate(candidate.trajectory[1:],start=1):
                    req=copy.deepcopy(base); req["reference_time_s"]=float(snapshot.reference_time_s+l*self.config.dt)
                    va=np.asarray([x[self.codec.index[f"Va_{i}"]] for i in range(3)],dtype=float); pned=np.asarray([x[self.codec.index[f"mesa_position_ned_m_{i}"]] for i in range(3)],dtype=float)
                    vals={"air_velocity_x":float(va[1]),"air_velocity_y":float(va[0]),"air_velocity_h":float(-va[2]),"state_x":float(pned[1]),"state_y":float(pned[0]),"state_h":float(-pned[2])}
                    for key,val in vals.items(): req.setdefault("wing_inputs",{}).setdefault(key,{})["value"]=val
                    req.setdefault("context",{})["layer4_candidate_id"]=candidate.candidate_id
                    req.setdefault("context",{})["layer4_scenario_id"]=candidate.scenario_id
                    req.setdefault("action",{})["layer4_hypothetical_action"]=candidate.actions[min(l-1,len(candidate.actions)-1)]
                    if scenario is not None:
                        req.setdefault("prediction",{})["layer4_scenario_parameters"]=scenario.parameters
                        req.setdefault("constraints",{}).update(scenario.constraints or {})
                    r=rt.step(RuntimeRequest.from_mapping(req)); sd=r.snapshot.to_dict() if hasattr(r.snapshot,"to_dict") else dict(r.snapshot)
                    reps=((sd.get("std") or {}).get("representations") or {}); minv=float(self.config.get("feasibility.min_twin_validity",0.2))
                    validity={t:float((rec or {}).get("validity",1.0)) for t,rec in reps.items()}
                    rows.append({"step":l,"mismatch":sd.get("mismatch"),"margin":sd.get("consistency_margin"),"health":sd.get("health"),"policy_version":sd.get("policy_version"),"validity":validity,"stability":sd.get("stability"),"snapshot":sd,"validity_ok":all(v>=minv for v in validity.values())})
        except Exception as e:
            err=repr(e)
            return self.outcome(
                "EXCEPTION","full_fidelity_runtime_failed",reason_codes=["full_fidelity_runtime_failed"],attempted=True,executed=bool(rows),
                error=err,error_id=self._error_id(cid,"full_fidelity_runtime_failed",err),
                steps=[{k:v for k,v in row.items() if k!="snapshot"} for row in rows],elapsed_s=time.perf_counter()-t0,
            )
        jmax=float((snapshot.slow_policy.get("consistency_bounds") or {}).get("maximum_mismatch",self.config.get("consistency.j_c_max",25))); mumin=float((snapshot.slow_policy.get("consistency_bounds") or {}).get("minimum_margin",self.config.get("consistency.mu_min",-1)))
        all_assessed=bool(rows) and all(r["mismatch"] is not None and r["margin"] is not None for r in rows)
        consistency_safe=all_assessed and all(float(r["mismatch"])<=jmax and float(r["margin"])>=mumin for r in rows)
        twin_safe=bool(rows) and all(r["validity_ok"] for r in rows)
        stability_safe=bool(rows) and all(((r.get("stability") or {}).get("stable") is not False) for r in rows)
        full_obj=self._full_objectives(rows,candidate,snapshot,scenario); full_norm=self._normalize(full_obj); sur_norm=candidate.normalized_objectives or self._normalize(candidate.objective_vector)
        keys=sorted(set(full_norm)&set(sur_norm)); delta=float(np.linalg.norm([full_norm[k]-sur_norm[k] for k in keys])) if keys else float("inf")
        eps=float(self.config.get("verification.objective_tolerance_normalized",2.0)); objective_safe=delta<=eps
        ff_feas=self._full_feasibility(rows,candidate,snapshot,scenario)
        chance_safe=float(ff_feas["P_viol_full"])<=float(self.config.get("feasibility.alpha_risk",0.1))
        F_full=bool(consistency_safe and twin_safe and stability_safe and ff_feas["safe"]); safe=bool(F_full and chance_safe and objective_safe)
        reasons=[]
        if not all_assessed: reasons.append("unassessed_layer3_consistency")
        elif not consistency_safe: reasons.append("layer3_consistency_gate_failed")
        if not twin_safe: reasons.append("twin_validity_gate_failed")
        if not stability_safe: reasons.append("stability_gate_failed")
        for reason in list(ff_feas.get("reasons") or []):
            if str(reason) not in reasons: reasons.append(str(reason))
        if not ff_feas["safe"] and "full_physical_feasibility_gate_failed" not in reasons: reasons.append("full_physical_feasibility_gate_failed")
        if not chance_safe: reasons.append("chance_constraint_gate_failed")
        if not objective_safe: reasons.append("objective_consistency_gate_failed")
        if safe: reasons=["verified"]
        status="ATTEMPTED_AND_VERIFIED" if safe else "ATTEMPTED_AND_REJECTED"
        result=self.outcome(
            status,"verified" if safe else (reasons[0] if reasons else "full_fidelity_verification_gate_failed"),reason_codes=reasons,
            verified=safe,attempted=True,executed=True,verification_origin="FF_CURRENT" if safe else "UNVERIFIED",
            F_full=F_full,chance_safe=chance_safe,objective_safe=objective_safe,objective_delta_normalized=delta,objective_tolerance_normalized=eps,
            full_objectives=full_obj,surrogate_objectives=candidate.objective_vector,consistency_safe=consistency_safe,
            twin_validity_safe=twin_safe,stability_safe=stability_safe,physical_mission_safe=ff_feas["safe"],
            P_viol_full=ff_feas["P_viol_full"],full_feasibility=ff_feas,surrogate_chance_risk=float(candidate.violation_probability),
            steps=[{k:v for k,v in row.items() if k!="snapshot"} for row in rows],
            elapsed_s=time.perf_counter()-t0,adapter="Layer3FMURuntime + MESA_NAV/WingMPET/MicroMetTwin + TDOC",
            verification_scope="MODEL_ONLY_LAYER3_FMU_REPLAY",native_controller_certified=False,flight_authorized=False,
        )
        result.update(model_prediction_scope(self.config))
        return result
