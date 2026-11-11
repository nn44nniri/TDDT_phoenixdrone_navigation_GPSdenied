from __future__ import annotations
import numpy as np
from .utils import finite_float
from .vtol import VTOLProfile
from .agl import resolve_operational_agl, resolve_operational_terrain
from .utils import probability01


class FeasibilityEngine:
    """Hard feasibility plus continuous violation measure for E4-58..E4-64.

    Hard indicators are used for final acceptance/chance probability.  The continuous
    normalized violation score is used *inside* the optimizer so constraints are not
    merely post-hoc filters.
    """
    def __init__(self,config,codec): self.config=config; self.codec=codec; self.vtol=VTOLProfile(config)

    @staticmethod
    def _merge_limits(snapshot,scenario,base):
        out=dict(base)
        gd=((snapshot.guidance or {}).get("constraint_delta") or {})
        sc=(getattr(scenario,"constraints",{}) or {}) if scenario is not None else {}
        for src in (gd,sc.get("layer3_guidance_delta") or {},sc):
            if not isinstance(src,dict): continue
            mapping={
                "reference_min_clearance_m":"min_terrain_clearance_m","min_terrain_clearance_m":"min_terrain_clearance_m",
                "min_obstacle_clearance_m":"min_obstacle_clearance_m","min_airspeed_m_s":"min_airspeed_m_s",
                "max_airspeed_m_s":"max_airspeed_m_s","max_wind_m_s":"max_wind_m_s","risk_max":"risk_max",
                "consistency_margin_guard":"consistency_margin_guard",
            }
            for k,v in src.items():
                kk=mapping.get(k)
                if kk and v is not None:
                    try: out[kk]=float(v)
                    except Exception: pass
        return out

    @staticmethod
    def _pos(x): return max(float(x),0.0)

    def evaluate_trajectory(self,trajectory,network,snapshot,degradation=None, *, actions=None, scenario=None, tube=None, terminal=None):
        idx=self.codec.index; degradation=degradation or {}; reasons=[]; per=[]; violation_terms=[]
        slow=snapshot.slow_policy or {}; cb=slow.get("consistency_bounds") or {}
        minv=float(self.config.get("feasibility.min_twin_validity",0.2)); jmax=float(cb.get("maximum_mismatch",self.config.get("consistency.j_c_max",25))); mumin=float(cb.get("minimum_margin",self.config.get("consistency.mu_min",-1)))
        ctx=snapshot.context or {}; mode=str(ctx.get("flight_mode") or ctx.get("stage") or ctx.get("stage_id") or "cruise").lower(); lim=self.vtol.limits(mode)
        base={"min_airspeed_m_s":float(lim["min_airspeed_m_s"]),"max_airspeed_m_s":float(lim["max_airspeed_m_s"]),"max_wind_m_s":float(lim["max_wind_m_s"]),
              "min_terrain_clearance_m":float(self.config.get("feasibility.min_terrain_clearance_m",30)),"min_obstacle_clearance_m":float(self.config.get("feasibility.min_obstacle_clearance_m",10)),
              "risk_max":None,"consistency_margin_guard":0.0,
              "constraint_mode":lim.get("constraint_mode"),"airspeed_policy":lim.get("airspeed_policy","STATIC_AIRSPEED_LIMIT")}
        if lim.get("terminal_min_airspeed_m_s") is not None:
            base["terminal_min_airspeed_m_s"]=float(lim["terminal_min_airspeed_m_s"])
        lim2=self._merge_limits(snapshot,scenario,base)
        reps=snapshot.std.get("representations") or {}; validity={}
        for t,r in reps.items():
            dg=degradation.get(t,0.0); dv=float(dg.get("validity",0.0)) if isinstance(dg,dict) else float(dg or 0.0)
            validity[t]=finite_float(r.get("validity"),1.0)*(1-dv)
        agl_res=resolve_operational_agl(snapshot,self.config)
        agl0=agl_res.get("altitude_agl_m")
        agl_authority=dict(agl_res.get("authority") or {})
        agl_available=bool(agl_res.get("ok") and agl0 is not None)
        legacy_flat_terrain=bool(agl_res.get("source")=="legacy_standalone_context")
        agl_unavailable_reason=agl_res.get("reason") or agl_authority.get("reason") or "authoritative_agl_missing"
        building=max(0.0,finite_float(ctx.get("building_height_m"),0.0))
        if not agl_available:
            reasons.append("AGL_UNAVAILABLE"); violation_terms.append(1.0)
        pidx=[idx[f"mesa_position_ned_m_{i}"] for i in range(3)]; d0=float(np.asarray(trajectory)[0,pidx[2]])
        weight_bounds=slow.get("weight_bounds") or {}; coupling_bounds=slow.get("coupling_bounds") or {}
        explicit_stable=(snapshot.stability or {}).get("stable")
        if explicit_stable is False: reasons.append("input_network_unstable"); violation_terms.append(1.0)
        tube_arr=np.asarray(tube,dtype=float) if tube is not None else None
        traj_arr=np.asarray(trajectory,dtype=float)
        entry_air=max(float(traj_arr[0,idx["airspeed_m_s"]]),0.0) if len(traj_arr) else 0.0
        terminal_override=None
        if mode == "transition_forward" and float(lim2.get("min_airspeed_m_s",0.0)) > 0.0:
            # A scenario/guidance minimum is interpreted as a terminal transition
            # requirement, never as a step-0 hover floor.
            terminal_override=float(lim2["min_airspeed_m_s"])
        ar=max(float(lim2["max_airspeed_m_s"])-0.0,1e-6)
        for l,x in enumerate(traj_arr):
            rr=[]; local=[]; air=float(x[idx["airspeed_m_s"]]); wind=np.asarray([x[idx[f"W_{i}"]] for i in range(3)])
            rad=(tube_arr[min(l,len(tube_arr)-1)] if tube_arr is not None and len(tube_arr) else np.zeros(len(x)))
            air_rad=abs(float(rad[idx["airspeed_m_s"]])) if len(rad)>idx["airspeed_m_s"] else 0.0
            wind_rad=np.asarray([abs(float(rad[idx[f"W_{i}"]])) for i in range(3)]) if len(rad)>max(idx[f"W_{i}"] for i in range(3)) else np.zeros(3)
            down_rad=abs(float(rad[pidx[2]])) if len(rad)>pidx[2] else 0.0
            effective_down_rad=down_rad; ground_contact_projection=False
            terrain_prediction=None
            if agl_available and not legacy_flat_terrain:
                terrain_prediction=resolve_operational_terrain(
                    float(x[pidx[0]]),float(x[pidx[1]]),self.config)
                if terrain_prediction.get("ok"):
                    agl=-float(x[pidx[2]])-float(terrain_prediction["terrain_elevation_m"])
                else:
                    agl=None
            else:
                agl=(agl0-(float(x[pidx[2]])-d0)) if agl_available else None
            obstacle_clear=(agl-building) if agl is not None else None
            if mode == "transition_forward":
                air_corridor=self.vtol.airspeed_corridor(
                    mode,
                    entry_air,
                    step_index=l,
                    step_count=max(len(traj_arr)-1,1),
                    context=ctx,
                    terminal_min_override_m_s=terminal_override,
                )
                min_air=max(float(air_corridor["lower_m_s"]),0.0)
                max_air=min(float(lim2["max_airspeed_m_s"]),float(air_corridor["upper_m_s"]))
                if not bool(air_corridor.get("reachable",True)):
                    rr.append("transition_forward_acceleration_envelope")
                    local.append(1.0)
            else:
                air_corridor=self.vtol.airspeed_corridor(
                    mode,
                    entry_air,
                    step_index=l,
                    step_count=max(len(traj_arr)-1,1),
                    context=ctx,
                )
                min_air=max(float(lim2["min_airspeed_m_s"]),0.0)
                max_air=float(lim2["max_airspeed_m_s"])
            # Zero-airspeed VTOL entry is legal, but scalar physical airspeed may
            # never become negative. Positive floors are robustly tube-tightened.
            if min_air > 0.0:
                v=self._pos((min_air-(air-air_rad))/ar)
            else:
                v=self._pos((0.0-air)/ar)
            local.append(v); rr += ["airspeed"] if v>0 else []
            v=self._pos(((air+air_rad)-max_air)/ar); local.append(v); rr += ["airspeed"] if v>0 else []
            wden=max(float(lim2["max_wind_m_s"]),1e-6); v=self._pos((float(np.linalg.norm(wind)+np.linalg.norm(wind_rad))-lim2["max_wind_m_s"])/wden); local.append(v); rr += ["wind"] if v>0 else []
            # Entry AGL authority does not make every hypothetical trajectory
            # point valid.  A point outside the canonical DEM must fail closed as
            # AGL_UNAVAILABLE, not fall through to arithmetic on ``None``.
            if agl_available and agl is not None:
                corridor=self.vtol.terrain_corridor(mode,agl0,step_index=l,step_count=max(len(trajectory)-1,1),context=ctx)
                lower=float(corridor["lower_m"]); upper=corridor.get("upper_m")
                # The ground is a unilateral contact boundary.  At vertical-takeoff
                # entry a symmetric free-flight tube may extend below that boundary,
                # even though those states are physically removed by ground contact.
                # Project only that impossible downward tail onto the ground corridor;
                # nominal penetration (agl < lower) remains a hard violation.
                ground_contact_projection=bool(corridor.get("policy")=="GROUND_REFERENCED_TAKEOFF_CORRIDOR")
                effective_down_rad=(min(down_rad,max(agl-lower,0.0)) if ground_contact_projection else down_rad)
                low_v=self._pos((lower-(agl-effective_down_rad))/max(abs(lower)+1.0,1.0))
                high_v=0.0 if upper is None else self._pos(((agl+down_rad)-float(upper))/max(abs(float(upper))+1.0,1.0))
                local.extend([low_v,high_v])
                policy=corridor.get("policy")
                if low_v>0:
                    rr += [{
                        "GROUND_REFERENCED_TAKEOFF_CORRIDOR":"takeoff_ground_corridor",
                        "HOVER_TARGET_AGL_CORRIDOR":"hover_agl_corridor",
                        "TERMINAL_GROUND_CORRIDOR":"landing_ground_penetration",
                        "ENROUTE_TERRAIN_FLOOR":"terrain_clearance",
                    }.get(policy,"terrain_clearance")]
                if high_v>0:
                    rr += [{
                        "GROUND_REFERENCED_TAKEOFF_CORRIDOR":"takeoff_ground_corridor",
                        "HOVER_TARGET_AGL_CORRIDOR":"hover_agl_corridor",
                        "TERMINAL_GROUND_CORRIDOR":"landing_corridor_upward_excursion",
                    }.get(policy,"terrain_clearance")]
                if building>0:
                    oden=max(float(lim2["min_obstacle_clearance_m"]),1.0); v=self._pos((lim2["min_obstacle_clearance_m"]-(obstacle_clear-down_rad))/oden); local.append(v); rr += ["obstacle_clearance"] if v>0 else []
            else:
                terrain_reason=((terrain_prediction or {}).get("reason") if terrain_prediction is not None
                                else agl_unavailable_reason)
                corridor={"policy":"AGL_UNAVAILABLE","reason":terrain_reason,"authority":
                          (terrain_prediction or {}).get("authority",agl_authority)}
                local.append(1.0); rr.append("AGL_UNAVAILABLE")
            for t,vv in validity.items():
                v=self._pos((minv-vv)/max(minv,1e-6)); local.append(v); rr += [f"validity:{t}"] if v>0 else []
            n=network[min(l,len(network)-1)]
            v=self._pos((float(n.get("J_C",0))-jmax)/max(abs(jmax),1.0)); local.append(v); rr += ["network_mismatch"] if v>0 else []
            mug=max(mumin,float(lim2.get("consistency_margin_guard",mumin))); v=self._pos((mug-float(n.get("mu",1)))/max(abs(mug)+1.0,1.0)); local.append(v); rr += ["network_margin"] if v>0 else []
            for t,w in (n.get("W") or {}).items():
                if t in weight_bounds:
                    lo,hi=map(float,weight_bounds[t]); den=max(hi-lo,1e-6); v=max(self._pos((lo-float(w))/den),self._pos((float(w)-hi)/den)); local.append(v); rr += [f"weight_bound:{t}"] if v>0 else []
            for e,k in (n.get("K") or {}).items():
                if e in coupling_bounds:
                    lo,hi=map(float,coupling_bounds[e]); den=max(hi-lo,1e-6); v=max(self._pos((lo-float(k))/den),self._pos((float(k)-hi)/den)); local.append(v); rr += [f"coupling_bound:{e}"] if v>0 else []
            violation_terms.extend(local)
            per.append({"step":l,"ok":not rr,"reasons":sorted(set(rr)),"airspeed_m_s":air,"wind_m_s":float(np.linalg.norm(wind)),"agl_m":agl,"obstacle_clearance_m":obstacle_clear,"agl_available":agl is not None,"agl_authority":agl_authority,"agl_unavailable_reason":None if agl is not None else ((terrain_prediction or {}).get("reason") or agl_unavailable_reason),
                        "terrain_prediction":terrain_prediction,
                        "tube_tightening":{"airspeed_radius_m_s":air_rad,"wind_radius_norm_m_s":float(np.linalg.norm(wind_rad)),"vertical_position_radius_m":down_rad,
                                           "effective_downward_position_radius_m":effective_down_rad,
                                           "unilateral_ground_contact_projection_applied":ground_contact_projection and effective_down_rad<down_rad,
                                           "lower_airspeed_tightening_applied":bool(min_air>0.0)},
                        "terrain_policy":corridor,
                        "airspeed_policy":air_corridor,
                        "constraint_violation_score":float(sum(v*v for v in local))})
            reasons.extend(rr)
        risk_signal=finite_float((snapshot.guidance or {}).get("risk_signal"),0.0)
        if lim2.get("risk_max") is not None:
            v=self._pos((risk_signal-float(lim2["risk_max"]))/max(abs(float(lim2["risk_max"])),1.0)); violation_terms.append(v)
            if v>0: reasons.append("mission_risk_threshold")
        if actions is not None:
            A=np.asarray(actions,dtype=float); lo=np.asarray(self.config.get("qlpv.action_lower"),dtype=float); hi=np.asarray(self.config.get("qlpv.action_upper"),dtype=float)
            if A.size:
                den=np.maximum(hi-lo,1e-6); av=np.maximum((lo-A)/den,(A-hi)/den); av=np.maximum(av,0.0); violation_terms.extend(av.reshape(-1).tolist())
                if np.any(av>0): reasons.append("action_bounds")
        terminal_required=bool(self.config.get("robust.require_terminal_certificate",False)); terminal_certified=None if terminal is None else bool(terminal.get("certified",False))
        if terminal_required and not terminal_certified: reasons.append("terminal_set_not_certified"); violation_terms.append(1.0)
        score=float(sum(v*v for v in violation_terms)); vmax=float(max(violation_terms) if violation_terms else 0.0)
        return {"feasible":not reasons,"per_step":per,"reasons":sorted(set(reasons)),"validity":validity,"vtol_mode":mode,"vtol_limits":lim2,
                "network_bounds":{"J_C_max":jmax,"mu_min":mumin,"weight_bounds":weight_bounds,"coupling_bounds":coupling_bounds},"risk_signal":risk_signal,
                "robust_state_tightening":tube is not None,"terminal_required":terminal_required,"terminal_certified":terminal_certified,
                "constraint_violation_score":score,"max_constraint_violation":vmax,"hard_violation_count":len(set(reasons))}

    def chance_probability(self,candidates):
        # Preserve optimizer-computed per-candidate chance probabilities when present.
        if candidates and all(bool((c.provenance or {}).get("optimizer_chance_probability")) or float(c.violation_probability)>=0 for c in candidates):
            explicit=[c for c in candidates if "optimizer_chance_probability" in (c.provenance or {})]
            if len(explicit)==len(candidates):
                total=sum(max(float(c.probability),0) for c in candidates) or 1.0
                return probability01(sum(max(float(c.probability),0)*probability01(c.violation_probability) for c in candidates)/total)
        groups={}
        for c in candidates:
            key=(c.provenance or {}).get("action_name") or c.candidate_id; groups.setdefault(key,[]).append(c)
        for members in groups.values():
            total=sum(max(float(c.probability),0) for c in members) or 1.0; viol=sum(max(float(c.probability),0) for c in members if not c.feasible); p=min(max(viol/total,0.0),1.0)
            for c in members: c.violation_probability=probability01(p)
        total=sum(max(float(c.probability),0) for c in candidates) or 1.0
        return probability01(sum(max(float(c.probability),0)*(0 if c.feasible else 1) for c in candidates)/total)
