from __future__ import annotations
import sys
import copy
import importlib.util
import warnings
import math
import hashlib
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from .base import LayerAdapter
from ..utils import unwrap, unwrap_mapping
from ..unit_adapter import UnitAdapterBinding
from ..climate_forcing import resolve_climate_forcing

class Layer2Adapter:
    layer_id=2
    def __init__(self,config,unit_adapter:UnitAdapterBinding|None=None):
        self.config=config; self.twins_root=config.path_value("TDDT_TWINS_ROOT"); self.climate_root=config.path_value("TDDT_CLIMATE_ROOT"); self.map_root=config.path_value("TDDT_MAP_ROOT")
        self.layer3_root=config.path_value("TDDT_LAYER3_ROOT"); self.unit_adapter=unit_adapter or UnitAdapterBinding(config)
        self.mesa=self.wing=None; self.climate_twin=None; self._estimate=None; self.contracts=None; self.layer3_cfg=None
        self.native_observation_source=None
        self.native_history_observation_source=None

    def _fresh_native_observation(self,current_source,context,payload):
        """Ask only the registered read-only source for missing measured history.

        Cursor advancement occurs after successful MESA propagation, never after
        merely downloading a page. Opaque local receipts are consumed here before
        the ingress becomes JSON context or persistent evidence.
        """
        cursor=getattr(self,'_online_mesa_history_cursor',None)
        episode=str(context.get('online_episode_id') or payload.get('online_episode_id') or 'online')
        if getattr(self,'_online_mesa_initialized_episode',None)!=episode:
            cursor=None
        history_source=getattr(self,'native_history_observation_source',None)
        if callable(history_source):
            fresh=(history_source(history_after_seq=cursor['sequence'],history_session_id=cursor['session_id'])
                   if cursor is not None else history_source())
            if not isinstance(fresh,Mapping) or fresh.get('ok') is not True:
                raise ValueError('Native retained IMU history source unavailable: '+str(
                    (fresh or {}).get('error') if isinstance(fresh,Mapping) else 'no response'))
            if cursor is not None and cursor['plant_time_s']!=getattr(self,'_online_mesa_last_time',None):
                raise ValueError('Native MESA history cursor differs from last integrated source time')
            from ..online_imu import attach_native_imu_history
            if cursor is None:
                anchor=fresh.get('native_imu_history_anchor') or {}
                gui=fresh.get('gui_state') or {}
                if (anchor.get('session_id')!=gui.get('session_id')
                    or anchor.get('monotonic_domain_id')!=gui.get('monotonic_domain_id')
                    or type(anchor.get('history_sequence')) is not int
                    or not isinstance(anchor.get('plant_time_s'),(int,float))
                    or isinstance(anchor.get('plant_time_s'),bool)
                    or not math.isfinite(anchor['plant_time_s']) or anchor['plant_time_s']<0):
                    raise ValueError('Native bootstrap history requires its actual retained source anchor')
                after_seq=anchor['history_sequence'];last_time=anchor['plant_time_s']
            else:
                after_seq=cursor['sequence'];last_time=cursor['plant_time_s']
            fresh=attach_native_imu_history(fresh,fresh.get('native_imu_history'),
                after_seq=after_seq,last_time=last_time,
                max_dt=float(self.config.get('tdoc.rtmo.realtime.max_sim_time_jump_s',.15)))
            if cursor is None:
                fresh['native_imu_history_provenance'].update(bootstrap_clock_history_only=True,
                    anchor=copy.deepcopy(anchor),MESA_initialization_unchanged=True,
                    historical_MESA_integration_before_initialization=False)
        else:
            fresh=current_source()
            if isinstance(fresh,Mapping) and fresh.get('native_imu_history') is not None:
                raise ValueError('Native history receipt supplied without registered measured cursor')
        return fresh

    def preflight(self):
        checks={}
        climate_forcing=resolve_climate_forcing(self.climate_root,require_exists=True)
        for name,p,req in [
            ("twins",self.twins_root,["python/twins_fmi","fmus/MESA_NAV.fmu","fmus/WingMPET.fmu","config/phoenix_drone_v3.yaml"]),
            ("climate",self.climate_root,["python/estimate_climate.py","python/tddt_layer2.py","fmu/MicroMetTwin.fmu"]),
        ]:
            missing=[] if p else req
            if p: missing=[r for r in req if not (p/r).exists()]
            checks[name]={"root":str(p) if p else None,"missing":missing,"ok":bool(p and p.is_dir() and not missing)}
        checks["climate"]["forcing_file"]=str(climate_forcing) if climate_forcing else None
        if not climate_forcing:
            checks["climate"]["missing"].append("active climate forcing (openmeteo_<hours>h.csv)")
            checks["climate"]["ok"]=False
        api={"DomainTwin":False,"ClimateDomainTwin":False,"strict_iso_boundary":False}
        if checks["twins"]["ok"]:
            try:
                if str(self.twins_root/"python") not in sys.path: sys.path.insert(0,str(self.twins_root/"python"))
                import twins_fmi
                api["DomainTwin"]=hasattr(twins_fmi,"DomainTwin")
            except Exception: pass
        if checks["climate"]["ok"]:
            try:
                if str(self.climate_root/"python") not in sys.path: sys.path.insert(0,str(self.climate_root/"python"))
                from tddt_layer2 import ClimateDomainTwin as _CDT  # noqa:F401
                api["ClimateDomainTwin"]=True
            except Exception: pass
        api["strict_iso_boundary"]=bool(self.layer3_root and (self.layer3_root/"src/layer3/contracts.py").exists() and self.unit_adapter.preflight().get("ok"))
        modules={name: bool(importlib.util.find_spec(name)) for name in ("numpy","pandas","rasterio","pyproj")}
        missing_modules=[name for name,ok in modules.items() if not ok]
        ok=all(x["ok"] for x in checks.values()) and not missing_modules and all(api.values())
        return {"ok":bool(ok),"checks":checks,"api":api,"python_modules":modules,"missing_python_modules":missing_modules,"ownership":"TDOC direct external Layer-2 API","normalization":"TDOC-owned strict SignalContractRegistry -> ISO adapter before Layer2 FMI ingress"}
    def _ensure(self):
        if self.mesa is not None:return
        if not self.preflight()["ok"]: raise RuntimeError("Layer2 dependency preflight failed")
        for p in (self.twins_root/"python",self.climate_root/"python"):
            if str(p) not in sys.path: sys.path.insert(0,str(p))
        from twins_fmi import DomainTwin
        from estimate_climate import estimate
        from tddt_layer2 import ClimateDomainTwin
        cfg=self.twins_root/"config"/"phoenix_drone_v3.yaml"
        self.mesa=DomainTwin(self.twins_root/"fmus"/"MESA_NAV.fmu",config=cfg,tep_root=self.twins_root/"resources"/"tddt_layer2"/"MESA_NAV")
        self.wing=DomainTwin(self.twins_root/"fmus"/"WingMPET.fmu",config=cfg,tep_root=self.twins_root/"resources"/"tddt_layer2"/"WingMPET")
        self._estimate=estimate; self.climate_twin=ClimateDomainTwin(self.climate_root/"resources"/"tddt_layer2")
        # Reuse Layer-3's strict signal-contract gateway, but invoke it from TDOC before Layer-2 FMI calls.
        if str(self.layer3_root/"src") not in sys.path: sys.path.insert(0,str(self.layer3_root/"src"))
        from layer3 import Layer3Config
        from layer3.tdoc_adapter import TDOCAdapter
        from layer3.contracts import SignalContractRegistry
        l3cfg=Layer3Config.from_yaml(self.layer3_root/"configs/configuration_boulder.yaml",package_root=self.layer3_root)
        self.config.bind_layer3_external_paths(l3cfg,required=True)
        self.unit_adapter.bind_layer3_config(l3cfg)
        rr=self.config.runtime_root/"layer2"; rr.mkdir(parents=True,exist_ok=True); l3cfg.paths.tdoc_health=str(rr/"tdoc_ingress_health.json")
        self.layer3_cfg=l3cfg; self.contracts=SignalContractRegistry(l3cfg,TDOCAdapter(l3cfg))
    @staticmethod
    def _packet(x): return x.to_dict() if hasattr(x,"to_dict") else dict(x)

    @staticmethod
    def _rebind_cached_online_mesa_packet(packet:Mapping[str,Any], *, context:Mapping[str,Any],
                                           context_id:str, stage_id:str)->dict[str,Any]:
        """Reuse one held plant sample without leaking transaction-local metadata.

        RTMO pre-release verification deliberately evaluates several phases while
        PT_LIB/JSBSim is held at one plant timestamp.  MESA's inertial state must not
        be integrated twice for that sample, but its packet context, context id, and
        stage are properties of the *current* transaction.  Reusing those fields from
        the preceding pre-release transaction can otherwise make MESA advertise
        ``hover`` while WingMPET and TDOC are executing ``vertical_takeoff``.
        """
        out=copy.deepcopy(dict(packet or {}))
        previous_stage=out.get("stage_id")
        out["context"]=copy.deepcopy(dict(context or {}))
        out["context_id"]=str(context_id)
        out["stage_id"]=str(stage_id)
        prov=dict(out.get("provenance") or {})
        prov["tdoc_same_sample_transaction_rebind"]={
            "schema":"TDOC-MESA-SameSampleTransactionRebind-v1",
            "state_reintegrated":False,
            "cached_state_reused":True,
            "previous_stage_id":previous_stage,
            "current_stage_id":str(stage_id),
            "reason":"plant sample unchanged; transaction-local phase/context rebound",
        }
        out["provenance"]=prov
        return out

    @staticmethod
    def _bind_mesa_simulation_packet_to_mission_time(packet:Mapping[str,Any], *, reference_time_s:Any,
                                                      alignment:Mapping[str,Any]|None, learning_mode:Any)->dict[str,Any]:
        """Bind simulation-backed MESA packet time to TDOC mission time.

        MESA's FMI clock advances by the local communication step and is not the
        mission clock.  When TDOC has explicitly aligned MESA to the simulated
        physical state for the current OFFLINE/ONLINE-JSBSim cycle, the resulting
        packet represents that cycle's snapshot and must therefore carry the same
        mission reference time for Layer-3 freshness.  The local FMI timestamp is
        retained in provenance and is never relabelled as field evidence.
        """
        out=dict(packet or {})
        mode=str(learning_mode or "").upper()
        if mode not in {"OFFLINE_SE","ONLINE_JSBSIM"} or not bool((alignment or {}).get("applied")):
            return out
        try:
            ref=float(reference_time_s)
        except (TypeError,ValueError):
            return out
        if not math.isfinite(ref):
            return out
        local_ts=out.get("timestamp")
        out["timestamp"]=ref
        prov=dict(out.get("provenance") or {})
        prov["tdoc_mesa_mission_time_alignment"]={
            "schema":"TDOC-MESA-MissionTimeAlignment-v1",
            "simulation_only":True,
            "not_field_evidence":True,
            "time_authority":"TDOC.reference_time_s",
            "local_fmi_timestamp_s":local_ts,
            "mission_reference_time_s":ref,
            "state_alignment_schema":str((alignment or {}).get("schema") or ""),
        }
        out["provenance"]=prov
        return out

    @staticmethod
    def _bind_wing_simulation_packet_to_mission_time(packet:Mapping[str,Any], *, reference_time_s:Any,
                                                      learning_mode:Any)->dict[str,Any]:
        """Bind freshly evaluated simulation WingMPET output to TDOC mission time.

        WingMPET's internal FMI clock measures local model evaluation progress and may
        advance faster than the mission transaction clock (especially when runtime
        coupling operations evaluate the same destination repeatedly).  The packet is
        nevertheless the WingMPET result for the current TDOC simulation snapshot, so
        Layer-3 freshness must use TDOC.reference_time_s.  The local FMI time is kept
        only as provenance and is never promoted to field evidence.
        """
        out=dict(packet or {})
        mode=str(learning_mode or "").upper()
        if mode not in {"OFFLINE_SE","ONLINE_JSBSIM"}:
            return out
        try:
            ref=float(reference_time_s)
        except (TypeError,ValueError):
            return out
        if not math.isfinite(ref):
            return out
        local_ts=out.get("timestamp")
        out["timestamp"]=ref
        prov=dict(out.get("provenance") or {})
        prov["tdoc_wing_mission_time_alignment"]={
            "schema":"TDOC-WingMPET-MissionTimeAlignment-v1",
            "simulation_only":True,
            "not_field_evidence":True,
            "time_authority":"TDOC.reference_time_s",
            "local_fmi_timestamp_s":local_ts,
            "mission_reference_time_s":ref,
        }
        out["provenance"]=prov
        return out

    @staticmethod
    def _estimate_climate_with_warning_filter(estimate,args):
        """Run the external estimator while filtering only its known masked-NoData cast warning."""
        filtered=[]
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result=estimate(args)
        for item in caught:
            message=str(item.message)
            is_known=(
                issubclass(item.category,UserWarning)
                and Path(str(item.filename)).name=="terrain_truth.py"
                and "converting a masked element to nan" in message.lower()
            )
            if is_known:
                filtered.append({"message":message,"source":str(item.filename),"line":int(item.lineno)})
            else:
                warnings.warn_explicit(message,item.category,str(item.filename),int(item.lineno))
        return result,filtered
    def _climate_args(self,request):
        raw=request.get("climate") or {}; ctx=request.get("context") or {}
        if self.contracts is None: raise RuntimeError("Layer2 ISO contract registry unavailable")
        c,records=self.contracts.normalize_climate_request(raw,context={**ctx,"reference_time_s":request.get("reference_time_s",0.0)},trusted_internal=False)
        if not c: raise ValueError("climate request is required")
        agl=c.get("elevation_ground_level",c.get("altitude_agl_m")); msl=c.get("elevation_sea_level",c.get("altitude_msl_m"))
        if agl is None and msl is None: raise ValueError("climate altitude AGL or MSL is required")
        time_text=str(c["time"])
        try:
            dt=datetime.fromisoformat(time_text.replace("Z","+00:00")); time_text=dt.replace(tzinfo=None).isoformat(timespec="seconds") if dt.tzinfo else time_text
        except Exception: pass
        forcing=resolve_climate_forcing(self.climate_root,explicit=c.get("input"),require_exists=False)
        return SimpleNamespace(
            fmu=str(c.get("fmu") or self.climate_root/"fmu"/"MicroMetTwin.fmu"), input=str(forcing) if forcing else "",
            time=time_text, latitude=float(c["latitude"]), longitude=float(c["longitude"]), elevation_sea_level=None if msl is None else float(msl), elevation_ground_level=None if agl is None else float(agl),
            dem=str(c.get("dem") or ((self.map_root/"terrain_obstacles.tif") if self.map_root else "")), dem_dir=c.get("dem_dir"), terrain_policy=str(c.get("terrain_policy","require-dem")),
            curvature_radius_m=float(c.get("curvature_radius_m",1000.0)), ground_elevation_m=c.get("ground_elevation_m"), slope_deg=float(c.get("slope_deg",0.0)), aspect_deg=float(c.get("aspect_deg",0.0)), curvature=float(c.get("curvature",0.0)), barnes_filter_km=float(c.get("barnes_filter_km",20.0)),
            wind_3dw_dx_m=float(c.get("wind_3dw_dx_m",100.0)),wind_3dw_dy_m=float(c.get("wind_3dw_dy_m",100.0)),wind_3dw_dz_m=float(c.get("wind_3dw_dz_m",10.0)),wind_profile_power=float(c.get("wind_profile_power",.143)),
            with_metadata=bool(c.get("with_metadata",(self.climate_root/"docs"/"fmi_semantic_schema.json").exists())), tddt_layer2=False, context_id=str(c.get("context_id",ctx.get("context_id","runtime"))), stage_id=str(request.get("stage","UNSPECIFIED")), operation_code=str(c.get("operation_code","climate_estimate")), evidence_log=None)
    @staticmethod
    def _state_scalar(mapping:Mapping[str,Any], name:str, default:float=0.0)->float:
        value=(mapping or {}).get(name,default)
        if isinstance(value,Mapping): value=value.get("value",default)
        try: return float(value)
        except Exception: return float(default)

    def _align_mesa_simulation_state(self, mesa, payload:dict[str,Any], climate_state:Mapping[str,Any], *, source:str)->dict[str,Any]:
        """Align MESA to TDOC-authoritative simulated plant state in OFFLINE or ONLINE SITL.

        ONLINE input is accepted only from TDOC's PT_LIB/JSBSim state ingress and is
        explicitly simulation-only/not-FIELD evidence.  This does not alter MESA-NAV's
        estimator ownership; it synchronizes the simulator state used by the cross-twin
        prediction/verification loop.
        """
        ctx=dict(payload.get("context") or {})
        mode=str(payload.get("learning_mode") or ctx.get("training_mode") or ctx.get("learning_mode") or "").upper()
        from tddt_tdoc.phoenix_frames import fallback_attitude, authoritative_or_fallback_attitude, normalize_phase
        rc=(ctx.get("rtmo_run_context") or ctx.get("tdoc_run_context") or {}) if isinstance(ctx,dict) else {}
        rc=rc if isinstance(rc,dict) else {}
        phase=normalize_phase(rc.get("phase_id") or payload.get("stage") or ctx.get("flight_phase"))
        complete=None; plant=None; state_source=None
        if mode=="OFFLINE_SE":
            complete=ctx.get("offline_physical_state") if isinstance(ctx.get("offline_physical_state"),dict) else None
            plant=ctx.get("offline_plant_state") if isinstance(ctx.get("offline_plant_state"),dict) else None
        elif mode=="ONLINE_JSBSIM":
            # RTMO authority separation: a mission-known initial condition may seed
            # MESA once, but PT_LIB/JSBSim truth must never re-anchor it every cycle.
            rtmo_auth=ctx.get("rtmo_data_authority") if isinstance(ctx.get("rtmo_data_authority"),dict) else {}
            if rtmo_auth.get("navigation")=="MESA_STD":
                init=ctx.get("rtmo_navigation_initial_state") if isinstance(ctx.get("rtmo_navigation_initial_state"),dict) else None
                if init and bool(ctx.get("rtmo_allow_initial_navigation_alignment")):
                    episode=str(ctx.get("online_episode_id") or payload.get("online_episode_id") or "online")
                    if getattr(self,"_online_mesa_initialized_episode",None)==episode:
                        return {"applied":False,"reason":"mission_initialization_already_consumed","navigation_authority":"MESA_STD"}
                    complete=init; state_source="rtmo_mission_initial_condition"
                else:
                    return {"applied":False,"reason":"rtmo_truth_injection_forbidden","simulation_only":False,"not_field_evidence":True,"navigation_authority":"MESA_STD"}
            else:
                complete=ctx.get("online_physical_state") if isinstance(ctx.get("online_physical_state"),dict) else None
                if not complete:
                    return {"applied":False,"reason":"online_simulation_state_missing","simulation_only":True,"not_field_evidence":True}
        else:
            return {"applied":False,"reason":"unsupported_learning_mode","simulation_only":True,"not_field_evidence":True}
        vals=None;initial_attitude=None
        try:
            if (mode=='ONLINE_JSBSIM' and state_source=='rtmo_mission_initial_condition'
                    and self.config.get('tdoc.rtmo.initial_navigation.attitude_source')=='native_ground_imu'):
                from ..online_imu import initial_ground_attitude
                ingress=(ctx.get('online_state_ingress') or {}).get('ptlib_ingress') or {}
                complete,initial_attitude=initial_ground_attitude(ingress,complete)
            if complete and all(k in complete for k in ("pn","pe","pd","vn","ve","vd","qw","qx","qy","qz")):
                vals={k:float(complete[k]) for k in ("pn","pe","pd","vn","ve","vd","qw","qx","qy","qz")}
                if mode=="OFFLINE_SE": state_source="offline_physical_state"
                elif state_source is None: state_source="online_physical_state/PT_LIB_JSBSim"
                # Before simulator release the public FMU façade masks Vg=0 even though
                # RunIC accepted an airborne true-airspeed seed.  TDOC carries that
                # air-relative initialization contract.  Combine it with *this cycle's*
                # authoritative MicroMet forcing so MESA Vg and Layer3 W are coherent.
                if mode=="ONLINE_JSBSIM" and complete.get("released") is False and isinstance(complete.get("air_velocity_ned_m_s"),(list,tuple)) and len(complete.get("air_velocity_ned_m_s"))==3:
                    av=[float(x) for x in complete["air_velocity_ned_m_s"]]
                    wu=self._state_scalar(climate_state,"wind_u_3dw",0.0); wv=self._state_scalar(climate_state,"wind_v_3dw",0.0); ww=self._state_scalar(climate_state,"diagnostic_vertical_wind",0.0)
                    vals["vn"]=av[0]+wv; vals["ve"]=av[1]+wu; vals["vd"]=av[2]-ww
                    state_source="online_RunIC_air_state+MicroMet_current_wind"
            elif mode=="OFFLINE_SE" and plant and all(k in plant for k in ("x","y","h","air_vx","air_vy","air_vh")):
                wu=self._state_scalar(climate_state,"wind_u_3dw",0.0); wv=self._state_scalar(climate_state,"wind_v_3dw",0.0); ww=self._state_scalar(climate_state,"diagnostic_vertical_wind",0.0)
                avx=float(plant["air_vx"]); avy=float(plant["air_vy"]); avh=float(plant["air_vh"])
                horiz=math.hypot(avx,avy); yaw=math.atan2(avx,avy) if horiz>1e-9 else 0.0; kinematic_pitch=math.atan2(avh,max(horiz,1e-9))
                att=authoritative_or_fallback_attitude(plant,phase,yaw_rad=yaw,kinematic_pitch_rad=kinematic_pitch)
                vals={"pn":float(plant["y"]),"pe":float(plant["x"]),"pd":-float(plant["h"]),"vn":avy+wv,"ve":avx+wu,"vd":-(avh+ww),"qw":att["qw"],"qx":att["qx"],"qy":att["qy"],"qz":att["qz"]}
                state_source="offline_plant_state+MicroMet+Phoenix_phase_attitude"
            else:
                return {"applied":False,"reason":"offline_simulation_state_missing","simulation_only":True,"not_field_evidence":True}
            fmu_values={"rN":vals["pn"],"rE":vals["pe"],"rD":vals["pd"],"vN":vals["vn"],"vE":vals["ve"],"vD":vals["vd"],"qw":vals["qw"],"qx":vals["qx"],"qy":vals["qy"],"qz":vals["qz"]}
            if not all(math.isfinite(float(v)) for v in fmu_values.values()): raise ValueError("non_finite_mesa_alignment")
            mesa.fmu.set(**fmu_values)
            if mode=="ONLINE_JSBSIM" and state_source=="rtmo_mission_initial_condition":
                self._online_mesa_initialized_episode=episode
                self._online_mesa_initialization=dict(complete)
                self._online_mesa_last_time=None
                self._online_mesa_last_packet=None
                self._online_mesa_history_cursor=None
            return {"applied":True,"schema":"TDOC-MESA-simulation-state-alignment-v2","source":source,"state_source":state_source,"learning_mode":mode,
                    "simulation_only":True,"not_field_evidence":True,"not_gnss_denied_estimator_evidence":True,
                    "position_ned_m":[fmu_values["rN"],fmu_values["rE"],fmu_values["rD"]],"ground_velocity_ned_m_s":[fmu_values["vN"],fmu_values["vE"],fmu_values["vD"]],
                    "initial_attitude":initial_attitude}
        except Exception as exc:
            return {"applied":False,"reason":"mesa_simulation_state_alignment_failed","error":repr(exc),"simulation_only":True,"not_field_evidence":True}

    @staticmethod
    def _wing27_integrate(wing,state:dict[str,Any],action:dict[str,Any],*,dt:float,wind_enu:tuple[float,float,float],air_density:float,source:str):
        """Integrate one physical Phoenix interval through WingMPET op27 using 10-ms substeps."""
        total=max(1e-6,float(dt)); n=max(1,int(math.ceil(total/0.01))); h=total/n
        cur={k:float(v) for k,v in state.items()}; feasible=True; last={}
        for _ in range(n):
            inp={**cur,"dt":h,
                 "left_motor_cmd":float(action["left_motor_cmd"]),"right_motor_cmd":float(action["right_motor_cmd"]),
                 "left_elevon_rad":float(action["left_elevon_rad"]),"right_elevon_rad":float(action["right_elevon_rad"]),
                 "wind_n":float(wind_enu[1]),"wind_e":float(wind_enu[0]),"wind_d":-float(wind_enu[2]),"air_density":float(air_density)}
            packet=wing.evaluate_packet(27,inputs=inp,require_all_inputs=True,communication_step=h,source=source)
            d=packet.to_dict() if hasattr(packet,"to_dict") else dict(packet); out=dict(d.get("state") or {}); last=d
            cur={"pn":float(out["next_pn"]),"pe":float(out["next_pe"]),"pd":float(out["next_pd"]),
                 "vn":float(out["next_vn"]),"ve":float(out["next_ve"]),"vd":float(out["next_vd"]),
                 "qw":float(out["next_qw"]),"qx":float(out["next_qx"]),"qy":float(out["next_qy"]),"qz":float(out["next_qz"]),
                 "p_rate":float(out["next_p_rate"]),"q_rate":float(out["next_q_rate"]),"r_rate":float(out["next_r_rate"]),
                 "omega_left":float(out["next_omega_left"]),"omega_right":float(out["next_omega_right"]),"soc":float(out["next_soc"])}
            if not all(math.isfinite(v) for v in cur.values()): raise RuntimeError("WingMPET op27 non-finite state")
            feasible=feasible and bool(float(out.get("vtol_feasible",0.0))>=0.5)
        return cur,{"ok":True,"operation_code":27,"substeps":n,"substep_s":h,"vtol_feasible":feasible,
                    "state":dict(last.get("state") or {}),"direct_pwm":False,"source":source}

    def derive_offline_phoenix_model_certificate(self, phoenix_profile_sha256:str|None=None, phase_seed_controls:dict[str,Any]|None=None, airframe_binding_sha256:str|None=None)->dict[str,Any]:
        """Qualify a local OFFLINE-only pilot->physical binding from WingMPET itself.

        The scan is deterministic, bounded, and never touches PT_LIB/ArduPilot.  It finds
        a forward-flight trim, then qualifies a conservative local elevon mapping around
        that trim.  The resulting evidence authorizes only the nominal zero-correction
        OFFLINE path; it cannot claim ONLINE readiness or measured servo/ESC dynamics.
        """
        self._ensure()
        import yaml
        from ..model_compatibility import integrated_native,capture_model_dependencies,qualify_new_certificate
        model_before=capture_model_dependencies(self.config) if integrated_native(self.config) else None
        cfg=yaml.safe_load((self.twins_root/"config"/"phoenix_drone_v3.yaml").read_text(encoding="utf-8")) or {}
        wing_fmu=self.twins_root/"fmus"/"WingMPET.fmu"
        wingmpet_fmu_sha256=hashlib.sha256(wing_fmu.read_bytes()).hexdigest() if wing_fmu.is_file() else None
        elev_lim=float((cfg.get("control_limits") or {}).get("elevon_joint_limit_rad",0.53))
        base={"pn":0.0,"pe":0.0,"pd":-100.0,"vn":13.0,"ve":0.0,"vd":0.0,
              "qw":1.0,"qx":0.0,"qy":0.0,"qz":0.0,"p_rate":0.0,"q_rate":0.0,"r_rate":0.0,
              "omega_left":0.0,"omega_right":0.0,"soc":1.0}
        scan=[]; best=None
        for motor in (0.20,0.25,0.30,0.35,0.40):
            for elev in (-0.12,-0.08,-0.04,0.0,0.04):
                elev=max(-elev_lim,min(elev_lim,float(elev)))
                action={"left_motor_cmd":motor,"right_motor_cmd":motor,"left_elevon_rad":elev,"right_elevon_rad":elev}
                st,rec=self._wing27_integrate(self.wing,base,action,dt=0.5,wind_enu=(0.0,0.0,0.0),air_density=1.225,source="TDOC/OFFLINE_MODEL_TRIM_SCAN")
                va=math.sqrt(st["vn"]**2+st["ve"]**2+st["vd"]**2); rate=math.sqrt(st["p_rate"]**2+st["q_rate"]**2+st["r_rate"]**2)
                score=abs(va-13.0)+2.0*abs(st["vd"])+0.25*rate+(0.0 if rec.get("vtol_feasible") else 1e3)
                row={"motor_cmd":motor,"symmetric_elevon_rad":elev,"airspeed_m_s":va,"vertical_velocity_ned_m_s":st["vd"],"body_rate_norm_rad_s":rate,"vtol_feasible":bool(rec.get("vtol_feasible")),"score":score}
                scan.append(row)
                if best is None or score<best[0]: best=(score,motor,elev,row)
        if best is None or not bool(best[3].get("vtol_feasible")):
            return {"ok":False,"reason":"offline_model_trim_not_found","scan":scan}
        _,motor_trim,elev_trim,best_row=best
        # Local pilot-domain gain is a bounded fraction of the source elevon limit.  It
        # preserves ArduPlane-like aileron/elevator mixing topology but is deliberately
        # much smaller than the online normalized-to-radian mixer.
        # A static OFFLINE model mixer has no ArduPlane inner rate loop.  Keep its
        # local authority deliberately conservative: 0.02 rad/norm is inside the
        # WingMPET-qualified neighborhood and well below the horizon-instability
        # boundary observed by bounded model sweep.  ONLINE commissioning/mixer is
        # unaffected and remains authoritative there.
        gain=min(0.02,0.04*elev_lim)
        M=[[gain,gain],[-gain,gain]]; probes=[]; qualified=True
        for a,e in ((0,0),(0.45,0),(-0.45,0),(0,0.65),(0,-0.65),(0.30,0.30),(-0.30,0.30)):
            le=max(-elev_lim,min(elev_lim,elev_trim+M[0][0]*a+M[0][1]*e)); re=max(-elev_lim,min(elev_lim,elev_trim+M[1][0]*a+M[1][1]*e))
            act={"left_motor_cmd":motor_trim,"right_motor_cmd":motor_trim,"left_elevon_rad":le,"right_elevon_rad":re}
            st,rec=self._wing27_integrate(self.wing,base,act,dt=0.20,wind_enu=(0.0,0.0,0.0),air_density=1.225,source="TDOC/OFFLINE_MODEL_LOCAL_QUALIFICATION")
            finite=all(math.isfinite(v) for v in st.values()); ok=bool(finite and rec.get("vtol_feasible")); qualified=qualified and ok
            probes.append({"aileron_norm":a,"elevator_norm":e,"left_elevon_rad":le,"right_elevon_rad":re,"ok":ok})
        # Qualify the local throttle delta used only by the WingMPET verification
        # bridge.  A candidate outside this measured-in-model neighborhood is rejected
        # rather than clamped or extrapolated.
        throttle_probes=[]; throttle_delta_min=-0.10; throttle_delta_max=0.10
        for delta in (throttle_delta_min,-0.05,0.0,0.05,throttle_delta_max):
            motor=max(0.0,min(1.0,motor_trim+delta))
            act={"left_motor_cmd":motor,"right_motor_cmd":motor,"left_elevon_rad":elev_trim,"right_elevon_rad":elev_trim}
            st,rec=self._wing27_integrate(self.wing,base,act,dt=0.20,wind_enu=(0.0,0.0,0.0),air_density=1.225,source="TDOC/OFFLINE_MODEL_THROTTLE_QUALIFICATION")
            finite=all(math.isfinite(v) for v in st.values()); ok=bool(finite and rec.get("vtol_feasible")); qualified=qualified and ok
            throttle_probes.append({"delta_from_trim_norm":delta,"motor_cmd":motor,"ok":ok})
        # Solution 2: vertical_takeoff/hover use Layer5-owned, source-bound vertical
        # seed controls.  This certificate only qualifies bounded command mapping;
        # downstream Full-Fidelity remains the physical authority and must still pass.
        phase_seeds=(phase_seed_controls or {}) if isinstance(phase_seed_controls,dict) else {}
        seed_phases=(phase_seeds.get("phases") or {}) if phase_seeds.get("ok") else {}
        seed_physics=(phase_seeds.get("physics_basis") or {}) if isinstance(phase_seeds.get("physics_basis"),dict) else {}
        vertical_paths={}; vertical_mapping_qualified=True
        for phase_name in ("vertical_takeoff","hover","vertical_landing"):
            rec0=seed_phases.get(phase_name) or {}
            if phase_name=="vertical_landing" and not rec0 and seed_phases.get("hover"):
                rec0=dict(seed_phases.get("hover") or {})
                rec0["operating_point_basis"]="phoenix_source_motor_law_landing_force_balance_reference"
            try: operating=float(rec0.get("throttle_request_norm"))
            except Exception: operating=float("nan")
            if not math.isfinite(operating) or operating<0.0 or operating>1.0:
                vertical_mapping_qualified=False
                vertical_paths[phase_name]={"model_qualified":False,"reason":"layer5_vertical_seed_missing_or_invalid"}
                continue
            if phase_name=="vertical_takeoff":
                physics_ok=bool(seed_physics.get("physics_derived") and seed_physics.get("source_motor_law") and seed_physics.get("force_balance_valid") and rec0.get("operating_point_basis")=="phoenix_source_motor_law_force_balance")
                if not physics_ok:
                    vertical_mapping_qualified=False
                    vertical_paths[phase_name]={"model_qualified":False,"reason":"vertical_takeoff_force_balance_provenance_missing"}
                    continue
            elif phase_name in {"hover","vertical_landing"}:
                pm=seed_physics.get("pilot_to_motor_mapping") or {}
                basis_ok=(rec0.get("operating_point_basis")=="phoenix_source_motor_law_hover_force_balance") if phase_name=="hover" else (rec0.get("operating_point_basis")=="phoenix_source_motor_law_landing_force_balance_reference")
                physics_ok=bool(seed_physics.get("physics_derived") and seed_physics.get("source_motor_law") and seed_physics.get("hover_force_balance_valid")
                    and seed_physics.get("air_density_context_valid") and seed_physics.get("battery_context_valid") and basis_ok
                    and pm.get("schema")=="Phoenix-pilot-to-motor-normalization-v1"
                    and float(pm.get("motor_norm_gain",-1.0))==1.0 and float(pm.get("motor_norm_offset",1.0))==0.0)
                if phase_name=="vertical_landing":
                    lb=seed_physics.get("vertical_landing_physics_basis") or {}
                    physics_ok=bool(physics_ok and lb.get("schema")=="Phoenix-vertical-landing-physics-basis-v1"
                        and lb.get("phase")=="vertical_landing" and lb.get("physics_derived") and lb.get("source_motor_law")
                        and lb.get("landing_force_reference_valid") and lb.get("air_density_context_valid") and lb.get("battery_context_valid")
                        and lb.get("terminal_agl_corridor_valid") and lb.get("terminal_agl_corridor_authority")=="Layer4 verified landing corridor"
                        and float(lb.get("descent_rate_target_m_s",-1.0))>=0.0
                        and float(lb.get("max_collective_reduction_norm",-1.0))>=0.0 and float(lb.get("max_braking_boost_norm",-1.0))>=0.0
                        and (lb.get("pilot_to_motor_mapping") or {}).get("schema")=="Phoenix-pilot-to-motor-normalization-v1")
                if not physics_ok:
                    vertical_mapping_qualified=False
                    vertical_paths[phase_name]={"model_qualified":False,"reason":f"{phase_name}_force_balance_provenance_missing"}
                    continue
            span_lo=0.10 if phase_name=="vertical_landing" else 0.04
            span_hi=0.08 if phase_name=="vertical_landing" else 0.04
            req_lo=max(0.0,operating-span_lo); req_hi=min(1.0,operating+span_hi)
            probes=[]; phase_ok=True
            # Solution2-v2: vertical_takeoff must not inherit the forward-flight
            # symmetric elevon trim.  Layer5 derives its trim from the source Phoenix
            # propwash pitch-moment equilibrium; TDOC only qualifies the bounded
            # mapping around that phase operating point. Hover/landing stay neutral.
            if phase_name=="vertical_takeoff":
                try:
                    phase_elev_trim=float(rec0.get("symmetric_elevon_trim_rad",(rec0.get("surface_trim_rad") or [0.0])[0]))
                except Exception:
                    phase_elev_trim=float("nan")
                if (not math.isfinite(phase_elev_trim)) or abs(phase_elev_trim)>elev_lim:
                    vertical_mapping_qualified=False
                    vertical_paths[phase_name]={"model_qualified":False,"reason":"vertical_takeoff_surface_trim_missing_or_invalid"}
                    continue
            else:
                phase_elev_trim=0.0 if phase_name in {"hover","vertical_landing"} else elev_trim
            for request_norm in (req_lo, operating-span_lo/2.0, operating, operating+span_hi/2.0, req_hi):
                request_norm=max(req_lo,min(req_hi,float(request_norm)))
                motor=request_norm
                act={"left_motor_cmd":motor,"right_motor_cmd":motor,"left_elevon_rad":phase_elev_trim,"right_elevon_rad":phase_elev_trim}
                st,rec=self._wing27_integrate(self.wing,base,act,dt=0.20,wind_enu=(0.0,0.0,0.0),air_density=1.225,source=f"TDOC/OFFLINE_MODEL_{phase_name.upper()}_COMMAND_QUALIFICATION")
                finite=all(math.isfinite(v) for v in st.values())
                ok=bool(finite and 0.0<=motor<=1.0)
                phase_ok=phase_ok and ok
                probes.append({"pilot_request_norm":request_norm,"motor_cmd":motor,"finite":bool(finite),"command_mapping_ok":ok,"wingmpet_vtol_feasible_diagnostic":bool(rec.get("vtol_feasible"))})
            vertical_mapping_qualified=vertical_mapping_qualified and phase_ok
            vertical_paths[phase_name]={"model_qualified":bool(phase_ok),
                "request_bounds_norm":[req_lo,req_hi],"operating_request_norm":operating,"operating_motor_norm":operating,
                "gain_motor_norm_per_pilot_delta":1.0,
                "surface_trim_rad":[phase_elev_trim,phase_elev_trim],
                "surface_mapping_scope":"phase-specific nominal surface trim; physical feasibility deferred to FF replay",
                "qualification_scope":"command_mapping_only; physical vertical-regime feasibility deferred to FF replay",
                "source":"Layer5-owned source-physics seed + WingMPET bounded actuator-command acceptance qualification",
                "operating_point_basis":rec0.get("operating_point_basis"),
                "physics_basis":seed_physics if phase_name in {"vertical_takeoff","hover","vertical_landing"} else None,
                "seed_provenance":{k:v for k,v in phase_seeds.items() if k!="phases"},"probes":probes}
            if phase_name=="vertical_takeoff":
                vertical_paths[phase_name].update({
                    "certificate_schema":"Layer5-Phoenix-takeoff-command-map-v2",
                    "bound_phase":"vertical_takeoff",
                    "surface_trim_basis":rec0.get("surface_trim_basis"),
                    "takeoff_symmetric_elevon_trim_rad":float(phase_elev_trim),
                    "takeoff_pitch_trim_derived":bool(seed_physics.get("takeoff_pitch_trim_derived")),
                    "takeoff_pitch_trim_valid":bool(seed_physics.get("takeoff_pitch_trim_valid")),
                    "qualification_semantics":"mapping-only around source-propwash-derived pitch equilibrium; WingMPET FF remains physical authority",
                })
            if phase_name=="vertical_landing":
                lb=seed_physics.get("vertical_landing_physics_basis") or {}
                vertical_paths[phase_name].update({
                    "certificate_schema":"Layer5-Phoenix-vertical-landing-command-map-v1",
                    "bound_phase":"vertical_landing",
                    "landing_physics_basis_schema":lb.get("schema"),
                    "descent_rate_target_m_s":lb.get("descent_rate_target_m_s"),
                    "terminal_agl_corridor_authority":lb.get("terminal_agl_corridor_authority"),
                    "qualification_semantics":"mapping-only around source-motor-law landing force reference with bounded descent/braking modulation; WingMPET FF remains physical authority",
                })
            if phase_name=="hover":
                # Solution 7: replace the legacy hover mapping neighborhood with a
                # versioned model/airframe/climate/state/action-bound certificate.
                hover_state=dict(seed_physics.get("hover_state_envelope") or {})
                hover_action=dict(seed_physics.get("hover_action_envelope") or {})
                vertical_paths[phase_name].update({
                    "certificate_schema":"Layer5-Phoenix-hover-command-map-v2",
                    "bound_phase":"hover",
                    "phoenix_profile_sha256":phoenix_profile_sha256,
                    "airframe_binding_sha256":airframe_binding_sha256,
                    "wingmpet_fmu_sha256":wingmpet_fmu_sha256,
                    "climate_fingerprint":seed_physics.get("environment_fingerprint"),
                    "state_envelope":hover_state,
                    "action_envelope":{
                        "pilot_throttle_norm":[req_lo,req_hi],
                        "motor_norm":[req_lo,req_hi],
                        "surface_trim_rad":[phase_elev_trim,phase_elev_trim],
                        **hover_action,
                    },
                    "legacy_hover_operating_point_invalidated":True,
                    "legacy_hover_operating_request_norm":0.05,
                    "reuse_semantics":"phase+model+airframe+climate+state+action envelope match required; FF remains mandatory",
                })
        qualified=qualified and vertical_mapping_qualified

        # Solution 6: transition phases use one bounded blend between the existing
        # forward tracker and the Layer5-owned vertical force-balance reference.
        # The certificate qualifies only the scalar command mapping over that union;
        # WingMPET Full-Fidelity remains the physical feasibility authority.
        hover_operating=float((seed_phases.get("hover") or {}).get("throttle_request_norm",motor_trim))
        transition_request_min=max(0.0,min(0.20,motor_trim-0.10))
        transition_request_max=min(1.0,max(0.42,hover_operating+0.04))
        transition_paths={}; transition_mapping_qualified=True
        for transition_phase in ("transition_forward","transition_back"):
            command_probes=[]; phase_ok=True
            for request_norm in (transition_request_min,motor_trim,(motor_trim+hover_operating)/2.0,hover_operating,transition_request_max):
                motor=max(0.0,min(1.0,float(request_norm)))
                act={"left_motor_cmd":motor,"right_motor_cmd":motor,"left_elevon_rad":elev_trim,"right_elevon_rad":elev_trim}
                st,rec=self._wing27_integrate(self.wing,base,act,dt=0.20,wind_enu=(0.0,0.0,0.0),air_density=1.225,source=f"TDOC/OFFLINE_MODEL_{transition_phase.upper()}_COMMAND_QUALIFICATION")
                finite=all(math.isfinite(v) for v in st.values()); ok=bool(finite and 0.0<=motor<=1.0)
                phase_ok=phase_ok and ok
                command_probes.append({"pilot_request_norm":float(request_norm),"motor_cmd":motor,"finite":bool(finite),"command_mapping_ok":ok,"wingmpet_vtol_feasible_diagnostic":bool(rec.get("vtol_feasible"))})
            transition_mapping_qualified=transition_mapping_qualified and phase_ok
            transition_paths[transition_phase]={"model_qualified":bool(phase_ok),
                "request_bounds_norm":[transition_request_min,transition_request_max],
                "operating_request_norm":motor_trim,"operating_motor_norm":motor_trim,
                "gain_motor_norm_per_pilot_delta":1.0,
                "qualification_scope":"command_mapping_only; blended transition physical feasibility deferred to FF replay",
                "source":"Solution6 regime-aware transition blend + WingMPET bounded actuator-command acceptance qualification",
                "controller_regime":"transition_blended_qLPV_reference","probes":command_probes}
        qualified=qualified and transition_mapping_qualified
        certificate={"schema":"Layer5-Phoenix-offline-model-certificate-v4","ok":bool(qualified),"learning_mode":"OFFLINE_SE",
                "simulation_only":True,"online_ready":False,"direct_pwm":False,"phoenix_profile_sha256":phoenix_profile_sha256,"airframe_binding_sha256":airframe_binding_sha256,"wingmpet_fmu_sha256":wingmpet_fmu_sha256,
                "nominal_tracker_authorized":bool(qualified),
                "mixer":{"model_qualified":bool(qualified),"elevon_matrix_rad_per_norm":M,"trim_rad":[elev_trim,elev_trim],
                         "gain_rad_per_norm":gain,"authority_fraction_of_joint_limit":gain/max(elev_lim,1e-9),
                         "source":"WingMPET/op27 deterministic local qualification + conservative horizon-bounded authority"},
                "trim":{"ready":bool(qualified),"pitch_request_norm":0.0,"throttle_request_norm":motor_trim,
                        "model_trim":{"motor_cmd":motor_trim,"symmetric_elevon_rad":elev_trim,"score":best_row["score"]}},
                "throttle_path":{"model_qualified":bool(qualified),"trim_norm":motor_trim,"gain_motor_norm_per_pilot_delta":1.0,
                                 "qualified_delta_from_operating_request_norm":[throttle_delta_min,throttle_delta_max],
                                 "source":"WingMPET/op27 bounded local throttle qualification"},
                "phase_throttle_paths":{**vertical_paths,**transition_paths},
                "servo_dynamics":{"identified":False,"measured":False},"esc_mapping":{"identified":False,"measured":False},
                "qualification":{"best":best_row,"local_probes":probes,"throttle_probes":throttle_probes,"scan_count":len(scan)},
                "safety_semantics":"OFFLINE model evidence only; nominal zero-correction path; no PT_LIB, no direct PWM, no ONLINE readiness"}
        if model_before is not None:
            certificate=qualify_new_certificate(certificate,model_before,capture_model_dependencies(self.config))
        return certificate

    def _apply_offline_training_control(self, training_feedback:dict[str,Any], payload:dict[str,Any], climate_state:dict[str,Any])->dict[str,Any]:
        phys=(training_feedback.get("physical_actuator_settings") or {}) if isinstance(training_feedback,dict) else {}
        vals=phys.get("values") if isinstance(phys,dict) else None; ctx=(payload.get("context") or {})
        if not (isinstance(vals,(list,tuple)) and len(vals)==4):
            return {"ok":False,"control_effect_applied":False,"reason":"offline_physical_actuator_settings_missing"}
        ml,mr,dl,dr=[float(x) for x in vals]; action={"left_motor_cmd":ml,"right_motor_cmd":mr,"left_elevon_rad":dl,"right_elevon_rad":dr}
        def cv(name,default=0.0):
            x=climate_state.get(name,default)
            if isinstance(x,dict): x=x.get("value",default)
            try:return float(x)
            except Exception:return float(default)
        wind=(cv("wind_u_3dw"),cv("wind_v_3dw"),cv("diagnostic_vertical_wind")); rho=cv("air_density",1.225)
        ops=(ctx.get("offline_physical_state") or {})
        required=("pn","pe","pd","vn","ve","vd","qw","qx","qy","qz","p_rate","q_rate","r_rate","omega_left","omega_right","soc")
        state=None
        if isinstance(ops,dict) and all(k in ops for k in required):
            try: state={k:float(ops[k]) for k in required}
            except Exception: state=None
        if state is None:
            ps=(ctx.get("offline_plant_state") or {})
            try:
                from tddt_tdoc.phoenix_frames import fallback_attitude, authoritative_or_fallback_attitude, normalize_phase
                rc=(ctx.get("rtmo_run_context") or ctx.get("tdoc_run_context") or {}) if isinstance(ctx,dict) else {}
                rc=rc if isinstance(rc,dict) else {}
                phase=normalize_phase(rc.get("phase_id") or payload.get("stage") or ctx.get("flight_phase"))
                avx=float(ps.get("air_vx",0.0)); avy=float(ps.get("air_vy",0.0)); avh=float(ps.get("air_vh",0.0)); horiz=math.hypot(avx,avy)
                yaw=math.atan2(avx,avy) if horiz>1e-9 else 0.0; kinematic_pitch=math.atan2(avh,max(horiz,1e-9))
                att=authoritative_or_fallback_attitude(ps,phase,yaw_rad=yaw,kinematic_pitch_rad=kinematic_pitch)
                state={"pn":float(ps["y"]),"pe":float(ps["x"]),"pd":-float(ps["h"]),
                       "vn":avy+wind[1],"ve":avx+wind[0],"vd":-(avh+wind[2]),
                       "qw":att["qw"],"qx":att["qx"],"qy":att["qy"],"qz":att["qz"],
                       "p_rate":float(ps.get("p_rate",0.0)),"q_rate":float(ps.get("q_rate",0.0)),"r_rate":float(ps.get("r_rate",0.0)),
                       "omega_left":float(ps.get("omega_left",0.0)),"omega_right":float(ps.get("omega_right",0.0)),"soc":float(ps.get("soc",1.0))}
            except Exception as exc:
                return {"ok":False,"control_effect_applied":False,"reason":"offline_physical_control_context_missing","error":repr(exc)}
        dt=float(ctx.get("offline_control_dt_s",1.0) or 1.0)
        try:
            nxt,rec=self._wing27_integrate(self.wing,state,action,dt=dt,wind_enu=wind,air_density=rho,source="TDOC/L5_OFFLINE_CONTROL_EFFECT")
            cf,cfrec=self._wing27_integrate(self.wing,state,action,dt=dt,wind_enu=(0.0,0.0,0.0),air_density=rho,source="TDOC/L5_OFFLINE_CONTROL_EFFECT_NO_WIND")
        except Exception as exc: return {"ok":False,"control_effect_applied":False,"reason":"offline_wingmpet_control_effect_exception","error":repr(exc)}
        applied=bool(rec.get("vtol_feasible") and all(math.isfinite(v) for v in nxt.values()))
        return {"ok":applied,"control_effect_applied":applied,"reason":"wingmpet_physical_control_effect" if applied else "wingmpet_physical_control_invalid",
                "physical_state":nxt,"counterfactual_physical_state":cf,"physical_action":action,"wingmpet":rec,"counterfactual_wingmpet":cfrec,
                "wind_enu_m_s":list(wind),"air_density_kg_m3":rho,"direct_pwm":False}

    @staticmethod
    def _layer7_feedback_ingress(payload:dict[str,Any])->dict[str,Any]:
        """Record the previous-cycle Layer7→Layer2 route as explicit bounded evidence.

        Raw Twin observations remain authoritative. Layer7 feedback is forwarded in
        the Layer2 context under TDOC/version control; it is never converted into PWM
        or direct JSBSim state/actuator mutation.
        """
        ctx=dict(payload.get("context") or {})
        routes=ctx.get("layer7_feedback") if isinstance(ctx.get("layer7_feedback"),dict) else {}
        route=(routes.get("Layer2") or {}) if isinstance(routes,dict) else {}
        packet=(route.get("packet") or {}) if isinstance(route,dict) else {}
        if not packet:
            return {"present":False,"consumed":False,"reason":"no_pending_layer7_layer2_feedback"}
        no_change=bool(packet.get("NoChangeAcknowledgement") is True)
        fields=sorted(str(k) for k,v in packet.items() if k != "Reason" and v not in ({},[],None))
        return {"present":True,"consumed":True,"schema":"TDOC-L7-to-L2-ingress-v1",
                "route_hash":route.get("route_hash"),"base_version":route.get("base_version"),
                "candidate_version":route.get("candidate_version") or packet.get("nu_new"),
                "fields":fields,"delta_x_present":bool(packet.get("DeltaX")),
                "corrected_prediction_present":bool(packet.get("CorrectedPrediction")),
                "corrected_uncertainty_present":bool(packet.get("CorrectedUncertainty")),
                "no_change_acknowledged":no_change,"adaptation_applied":bool(not no_change),
                "persistent":False if no_change else None,
                "application_semantics":"version-controlled adaptation evidence forwarded to Layer2/Twin context; no direct sensor/PWM/JSBSim mutation"}

    def invoke(self,payload:dict[str,Any])->dict[str,Any]:
        if payload.get("packets"):
            p={str(k):dict(v) for k,v in payload["packets"].items()}; return {"ok":True,"packets":p,"mode":"provided_packets","direct_layer_ownership":False}
        started=time.perf_counter(); timings={}
        self._ensure(); ctx=dict(payload.get("context") or {})
        layer7_ingress=self._layer7_feedback_ingress(payload)
        if layer7_ingress.get("present"):
            ctx["layer7_layer2_ingress"]={**layer7_ingress,"source":"Layer7/TDOC-VersionedRoute"}
            payload["context"]=ctx
        training_feedback=payload.get("training_feedback") or ctx.get("layer5_training_feedback")
        if training_feedback is not None:
            ctx["layer5_training_feedback"]=training_feedback
            ctx["training_mode"]="OFFLINE_SE"
        cid=str(ctx.get("context_id","runtime")); stage=str(payload.get("stage","UNSPECIFIED")); mop=int(payload.get("mesa_operation",5)); wop=int(payload.get("wing_operation",4))
        rctx={**ctx,"stage":stage,"reference_time_s":float(payload.get("reference_time_s",0.0))}
        mi=self.contracts.normalize_operation_inputs(self.mesa,mop,payload.get("mesa_inputs") or {},context=rctx,trusted_internal=False)
        wi=self.contracts.normalize_operation_inputs(self.wing,wop,payload.get("wing_inputs") or {},context=rctx,trusted_internal=False)
        wing_online_alignment={"applied":False,"reason":"not_online_or_no_state"}
        # ONLINE control starts from PT_LIB/JSBSim's explicit airborne RunIC contract.
        # Operation 4 computes WingMPET airspeed/gamma/heading from ENU air velocity;
        # feeding the stale example request here would make L4 optimize a different
        # heading than the simulator.  Align only the operation inputs, preserving
        # WingMPET as the producer of the realized canonical outputs.
        if str(payload.get("learning_mode") or ctx.get("training_mode") or "").upper()=="ONLINE_JSBSIM":
            ops=(ctx.get("online_execution_state") if isinstance(ctx.get("online_execution_state"),dict) else None) or (ctx.get("online_physical_state") if isinstance(ctx.get("online_physical_state"),dict) else None)
            av=(ops or {}).get("air_velocity_ned_m_s")
            if isinstance(av,(list,tuple)) and len(av)==3 and wop==4:
                try:
                    avn,ave,avd=[float(x) for x in av]
                    # NED -> ENU contract: x=E, y=N, h=Up=-D.
                    wi.values["air_velocity_x"]=ave; wi.values["air_velocity_y"]=avn; wi.values["air_velocity_h"]=-avd
                    if all(k in (ops or {}) for k in ("pn","pe","pd")):
                        wi.values["state_x"]=float(ops["pe"]); wi.values["state_y"]=float(ops["pn"]); wi.values["state_h"]=-float(ops["pd"])
                    wing_online_alignment={"applied":True,"schema":"TDOC-WingMPET-online-RunIC-input-alignment-v1",
                        "operation_code":4,"source":"TDOC online_execution_state / PT_LIB-JSBSim simulation-only plant evidence",
                        "air_velocity_ned_m_s":[avn,ave,avd],"air_velocity_enu_m_s":[ave,avn,-avd],
                        "simulation_only":True,"not_field_evidence":True,"ownership":"WingMPET remains canonical-output producer"}
                except Exception as exc:
                    raise RuntimeError(f"online WingMPET operation-4 state alignment failed: {exc!r}")
        # Climate is evaluated before MESA only so OFFLINE simulation truth can use the
        # same MicroMet wind to convert air-relative plant velocity to ground NED.
        # The three Layer-2 twins remain independent; no Layer-2 twin owns another.
        args=self._climate_args(payload)
        # Some valid terrain-edge samples in the external Climate package trigger a noisy
        # numpy masked-value cast warning before terrain_truth.py performs its own NoData
        # handling. Keep the external package immutable: TDOC suppresses only this exact
        # known warning at the adapter boundary, records it in provenance, and re-emits
        # every other warning unchanged.
        timings['setup_and_input_normalization']=time.perf_counter()-started
        mark=time.perf_counter()
        est,filtered_terrain_warnings=self._estimate_climate_with_warning_filter(self._estimate,args)
        climate=dict(self.climate_twin.packet_from_estimate(est,context_id=cid,stage_id=stage,operation_code=args.operation_code,data_age_s=float(payload.get("climate_data_age_s",0.0))))
        from ..native_time import stamp_climate_time
        climate=stamp_climate_time(climate,ctx)
        if filtered_terrain_warnings:
            provenance=dict(climate.get("provenance") or {})
            provenance["tdoc_external_warning_filter"]={
                "count":len(filtered_terrain_warnings),
                "reason":"external terrain_truth masked NoData cast warning; estimator completed and packet validity remains authoritative",
                "warnings":filtered_terrain_warnings,
            }
            climate["provenance"]=provenance
        # Preserve the full MicroMet-3DW formal outputs when the estimator produced them.
        cstate=dict(climate.get("state") or {})
        derivative_keys=("du_dx","du_dy","du_dz","dv_dx","dv_dy","dv_dz","dw_dx","dw_dy","dw_dz","du_dt","dv_dt","dw_dt","Du_Dt","Dv_Dt","Dw_Dt")
        for key in derivative_keys:
            if key in est and est.get(key) is not None: cstate[key]=est[key]
        climate["state"]=cstate; climate["signal_envelopes"]=self.contracts.climate_output_envelopes(climate)
        timings['climate']=time.perf_counter()-mark; mark=time.perf_counter()
        current_source=getattr(self,'native_observation_source',None)
        if (current_source is not None and
            str(payload.get('learning_mode') or ctx.get('training_mode') or '').upper()=='ONLINE_JSBSIM' and
            (ctx.get('rtmo_data_authority') or {}).get('navigation')=='MESA_STD'):
            # Climate/model validation is slower than IMU acquisition. Acquire
            # a NEW observation after that work, preserving the climate packet's
            # original dataset timestamp/projection. Layer3 measures their age
            # difference; no old observation is retimed or marked fresh.
            from ..online_imu import canonical_imu
            fresh=self._fresh_native_observation(current_source,ctx,payload)
            checked=canonical_imu(fresh,getattr(self,'_online_mesa_initialization',None))
            previous=float(payload.get('reference_time_s',0.))
            if checked['plant_time_s']<previous:
                raise ValueError('Native current observation refresh reversed source clock')
            ctx=dict(ctx);ctx['online_state_ingress']={'ptlib_ingress':fresh}
            ctx['native_current_observation_refresh']={
                'previous_source_time_s':previous,'current_source_time_s':checked['plant_time_s'],
                'climate_source_timestamp':climate['timestamp'],'clock_rebased':False,
                'archived_observation_retimed':False}
            payload['context']=ctx;payload['reference_time_s']=checked['plant_time_s']
        timings['native_observation_and_history']=time.perf_counter()-mark; mark=time.perf_counter()
        mesa_alignment=self._align_mesa_simulation_state(self.mesa,payload,cstate,source="TDOC/Layer2Adapter/simulation-state-alignment")
        if str(payload.get("learning_mode") or ctx.get("training_mode") or "").upper()=="OFFLINE_SE" and (ctx.get("offline_plant_state") or ctx.get("offline_physical_state")) and not mesa_alignment.get("applied"):
            raise RuntimeError(f"offline MESA simulation-state alignment required but failed: {mesa_alignment}")
        if str(payload.get("learning_mode") or ctx.get("training_mode") or "").upper()=="ONLINE_JSBSIM" and ctx.get("online_physical_state") and not mesa_alignment.get("applied"):
            raise RuntimeError(f"online MESA simulation-state alignment required but failed: {mesa_alignment}")
        learning_mode=str(payload.get("learning_mode") or ctx.get("training_mode") or "").upper()
        online_imu=None
        is_online_nav=(learning_mode=="ONLINE_JSBSIM" and (ctx.get("rtmo_data_authority") or {}).get("navigation")=="MESA_STD")
        if is_online_nav:
            if (ctx.get('rtmo_allow_initial_navigation_alignment') and not mesa_alignment.get('applied')
                    and mesa_alignment.get('reason')!='mission_initialization_already_consumed'):
                raise ValueError('Online initial MESA alignment rejected: '+str(mesa_alignment))
            from ..online_imu import canonical_imu, measured_intervals
            ingress=(ctx.get("online_state_ingress") or {}).get("ptlib_ingress") or {}
            online_imu=canonical_imu(ingress,getattr(self,"_online_mesa_initialization",None))
            if mop!=5:
                raise ValueError("Online MESA sensor bridge requires inertial propagation operation 5")
            last=getattr(self,"_online_mesa_last_time",None);now=online_imu["plant_time_s"]
            clock_sync=ctx.get("rtmo_plant_clock_synchronization")
            clock_rebased=False
            if clock_sync is not None:
                sync=dict(clock_sync) if isinstance(clock_sync,Mapping) else {}
                handoff=dict(sync.get("handoff_evidence") or {})
                try: sync_time=float(sync.get("plant_time_s"))
                except (TypeError,ValueError): sync_time=math.nan
                max_dt=float(self.config.get("tdoc.rtmo.realtime.max_sim_time_jump_s",0.15))
                valid_sync=bool(
                    sync.get("schema")=="TDOC-RTMO-PlantClockSynchronization-v1"
                    and sync.get("authorized") is True
                    and sync.get("reason")=="SUPERVISED_RELEASE_HANDOFF_COMPLETE"
                    and str(sync.get("source_transaction_id") or "")
                    and handoff.get("ready") is True
                    and handoff.get("authorized") is True
                    and handoff.get("transition_complete") is True
                    and handoff.get("handoff_state")=="complete"
                    and math.isfinite(sync_time)
                    and -1e-8 <= float(now)-sync_time <= max_dt+1e-8
                )
                if not valid_sync:
                    raise ValueError("Online MESA rejected invalid plant-clock synchronization evidence")
                # The supervised release is a lifecycle boundary, not a physical
                # integration interval.  Re-initialize from the current measured
                # sample and retain the normal grant for every subsequent sample.
                last=sync_time
                self._online_mesa_last_packet=None
                clock_rebased=True
            elapsed=(now-last) if last is not None else 0.0
            if elapsed < -1e-8:
                raise ValueError("Online MESA plant time regressed; refusing invalid IMU integration")
            max_dt=float(self.config.get("tdoc.rtmo.realtime.max_sim_time_jump_s",0.15))
            buffered_steps=measured_intervals(ingress,last,online_imu,max_dt)
            if online_imu["measured_simulated_imu"] and elapsed>max_dt+1e-8 and buffered_steps is None:
                raise ValueError("Online MESA IMU interval exceeds authorized plant-time grant")
            cached=getattr(self,"_online_mesa_last_packet",None)
            if elapsed<=1e-9 and cached is not None:
                mesa=self._rebind_cached_online_mesa_packet(
                    cached,context=ctx,context_id=cid,stage_id=stage)
                online_imu.update({"integrated_dt_s":0.0,"same_plant_sample_reused":True})
            else:
                # Only these six canonical SI channels are admitted; no position,
                # velocity, attitude or truth-wind reset from JSBSim is possible here.
                self.mesa.fmu.set(**online_imu["values"])
                # Operation-5's published TEP domain starts at 10 microseconds.
                # A held initialization evaluates a balanced, stationary sample;
                # it is not evidence of elapsed physical flight.
                if online_imu["measured_simulated_imu"] and 0 < elapsed < 1e-5:
                    raise ValueError("Online MESA sample interval below operation-5 domain")
                dt=(elapsed if online_imu["measured_simulated_imu"] and elapsed>0 else 1e-5)
                steps=buffered_steps or [(dt,online_imu["values"])]
                # Keep every measured integration step and every TEP check.  Do
                # not serialize the complete replay buffer into each substep's
                # evidence packet: it is retained once in the final context.
                from ..online_imu import compact_replay_context
                substep_context=compact_replay_context(ctx) if buffered_steps else ctx
                sample_time=float(last) if buffered_steps else float(now)-dt
                for index,(sample_dt, sample_values) in enumerate(steps):
                    self.mesa.fmu.set(**sample_values)
                    mi.values["dt"]=sample_dt
                    sample_time+=sample_dt
                    step_context=dict(ctx if index==len(steps)-1 else substep_context)
                    step_context["online_imu_substep"]={"index":index,"count":len(steps),
                        "plant_time_s":sample_time,"dt_s":sample_dt,"values":dict(sample_values),
                        "source":online_imu["source"],"frame":"BODY_FRD"}
                    # evaluate_packet executes the FMU and all TEP/validity checks
                    # on EVERY measured step. Intermediate packet objects are
                    # discarded; recursively copying their context is unnecessary.
                    mesa_packet=self.mesa.evaluate_packet(mop,context=step_context,context_id=cid,stage_id=stage,inputs=mi.values,communication_step=sample_dt,source=online_imu["source"])
                mesa=self._packet(mesa_packet)
                online_imu["integrated_sample_count"]=len(buffered_steps or [None])
                online_imu.update({"integrated_dt_s":dt,"same_plant_sample_reused":False})
                self._online_mesa_last_packet=copy.deepcopy(mesa)
            if clock_rebased:
                online_imu["plant_clock_synchronization"]={
                    "schema":clock_sync.get("schema"),
                    "authorized":True,
                    "reason":clock_sync.get("reason"),
                    "source_transaction_id":clock_sync.get("source_transaction_id"),
                    "plant_time_s":float(clock_sync.get("plant_time_s")),
                    "estimator_action":"REINITIALIZE_CURRENT_MEASURED_SAMPLE_WITHOUT_COMMISSIONING_INTERVAL_INTEGRATION",
                    "normal_interval_limit_s":float(self.config.get("tdoc.rtmo.realtime.max_sim_time_jump_s",0.15)),
                }
            self._online_mesa_last_time=now
            gui=ingress.get('gui_state') or {};sample=gui.get('imu') or {}
            sequence=sample.get('history_sequence')
            if type(sequence) is int and sequence>0 and gui.get('session_id'):
                self._online_mesa_history_cursor={'sequence':sequence,'session_id':gui['session_id'],
                    'plant_time_s':now,'monotonic_domain_id':gui.get('monotonic_domain_id')}
            if ingress.get('native_imu_history_provenance'):
                online_imu['history_replay']=copy.deepcopy(ingress['native_imu_history_provenance'])
            # Scheduler time is only the packet alignment coordinate. Plant time
            # remains separately available for freshness/execution qualification.
            mesa["timestamp"]=float(payload.get("reference_time_s",0.0))
            mesa.setdefault("provenance",{})["online_imu_ingress"]=online_imu
        else:
            mesa=self._packet(self.mesa.evaluate_packet(mop,context=ctx,context_id=cid,stage_id=stage,inputs=mi.values,communication_step=0.01 if mesa_alignment.get("applied") else 1e-3,source="TDOC/Layer2Adapter/ISO-normalized"))
        mesa=self._bind_mesa_simulation_packet_to_mission_time(
            mesa, reference_time_s=payload.get("reference_time_s",0.0), alignment=mesa_alignment, learning_mode=learning_mode)
        mesa["signal_envelopes"]=self.contracts.output_envelopes(self.mesa,mop,mesa,twin_id="MESA_NAV")
        if mesa_alignment.get("applied"):
            prov=dict(mesa.get("provenance") or {}); prov["tdoc_simulation_state_alignment"]=dict(mesa_alignment); mesa["provenance"]=prov
        timings['mesa_alignment_and_propagation']=time.perf_counter()-mark; mark=time.perf_counter()
        if is_online_nav and ingress.get('source')=='Layer6/GUI/MAVLink' and wop==4:
            from ..online_imu import wing_inputs_from_navigation
            wing_online_alignment=wing_inputs_from_navigation(wi.values,mesa['state'],cstate)
        wing=self._packet(self.wing.evaluate_packet(wop,context=ctx,context_id=cid,stage_id=stage,inputs=wi.values,source="TDOC/Layer2Adapter/ISO-normalized")); wing["signal_envelopes"]=self.contracts.output_envelopes(self.wing,wop,wing,twin_id="WingMPET")
        wing=self._bind_wing_simulation_packet_to_mission_time(wing,reference_time_s=payload.get("reference_time_s",0.0),learning_mode=learning_mode)
        if wing_online_alignment.get("applied"):
            prov=dict(wing.get("provenance") or {}); prov["tdoc_online_runic_input_alignment"]=dict(wing_online_alignment); wing["provenance"]=prov
        try:self.contracts.tdoc.flush_health()
        except Exception: pass
        meta={"input_normalization":{"MESA_NAV":{"records":mi.records,"envelopes":mi.envelopes},"WingMPET":{"records":wi.records,"envelopes":wi.envelopes}},"normalized_wing_inputs":wi.values,
              "mesa_simulation_state_alignment":mesa_alignment,"mesa_online_sensor_ingress":online_imu,"wing_online_input_alignment":wing_online_alignment,"layer7_feedback_ingress":layer7_ingress}
        control_effect={"ok":False,"control_effect_applied":False,"reason":"not_requested"}
        if training_feedback is not None:
            control_effect=self._apply_offline_training_control(training_feedback,payload,cstate)
        timings['wing_and_feedback']=time.perf_counter()-mark
        timings['total']=time.perf_counter()-started
        meta['wall_timings_s']=timings
        return {"ok":True,"packets":{"MESA_NAV":mesa,"WingMPET":wing,"MicroMetTwin":climate},"layer3_runtime_meta":meta,
                "mode":"tdoc_owned_direct_api_training_feedback" if training_feedback is not None else "tdoc_owned_direct_api",
                "training_feedback_applied":training_feedback is not None,"training_feedback_hash":(training_feedback or {}).get("feedback_hash") if isinstance(training_feedback,dict) else None,
                "control_effect_applied":bool(control_effect.get("control_effect_applied")),"control_effect":control_effect,
                "physical_state":control_effect.get("physical_state"),"direct_layer_ownership":False}

    def open_isolated_session(self):
        """Create isolated external Layer-2 instances for TDOC-owned full-fidelity verification."""
        self._ensure()
        from twins_fmi import DomainTwin
        from tddt_layer2 import ClimateDomainTwin
        cfg=self.twins_root/"config"/"phoenix_drone_v3.yaml"
        mesa=DomainTwin(self.twins_root/"fmus"/"MESA_NAV.fmu",config=cfg,tep_root=self.twins_root/"resources"/"tddt_layer2"/"MESA_NAV")
        wing=DomainTwin(self.twins_root/"fmus"/"WingMPET.fmu",config=cfg,tep_root=self.twins_root/"resources"/"tddt_layer2"/"WingMPET")
        climate=ClimateDomainTwin(self.climate_root/"resources"/"tddt_layer2")
        return {"mesa":mesa,"wing":wing,"climate_twin":climate,"estimate":self._estimate}

    @staticmethod
    def close_isolated_session(session):
        for key in ("mesa","wing"):
            try:
                obj=session.get(key)
                if obj is not None: obj.close()
            except Exception: pass

    def close(self):
        for x in (self.mesa,self.wing):
            try:
                if x is not None:x.close()
            except Exception: pass
        self.mesa=self.wing=None
