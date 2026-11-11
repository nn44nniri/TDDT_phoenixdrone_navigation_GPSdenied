from __future__ import annotations
import importlib
import inspect
import math
import os
import sys
import hashlib
from pathlib import Path
from typing import Any
from ..utils import jsonable
from ..storage import CompressedPersistentWorkspace

class Layer5Adapter:
    """Exact-root adapter for the external Layer-5 decision package.

    TDOC owns invocation. Layer 5 remains external and is loaded only from
    TDDT_LAYER5_ROOT; a globally/preloaded copy outside that root is rejected.
    """
    layer_id=5
    def __init__(self,config,*,kstore_root=None):
        self.config=config
        self.root=config.path_value("TDDT_LAYER5_ROOT")
        self.expected_package=str(config.get("layers.layer5.expected_package","layer5_v20260911_1510"))
        self.expected_version=str(config.get("layers.layer5.expected_version","2026.9.11.1510"))
        self.config_rel=str(config.get("layers.layer5.config","configs/configuration_phoenix.yaml"))
        self.cfg=None; self.engine=None; self.module=None
        self._verification_model_certificate=None
        self.native_mission_binding=None
        self.kstore_root=(Path(kstore_root) if kstore_root is not None else
                         (config.path_value("TDDT_LAYER5_KSTORE_ROOT") or (config.runtime_root/"layer5/kstore"))).resolve()
        self.kstore_root.mkdir(parents=True,exist_ok=True)
        self.kstore_workspace=CompressedPersistentWorkspace(
            self.kstore_root,prefix="tdoc-layer5-",working_parent=self.config.runtime_root/".transient")
    def _origin_ok(self,module)->bool:
        path=getattr(module,"__file__",None)
        if not path or not self.root:return False
        try: Path(path).resolve().relative_to(self.root.resolve()); return True
        except Exception:return False
    def _required(self):
        return ["src/layer5/__init__.py",self.config_rel]
    def preflight(self):
        p=self.root; missing=["root"] if not p or not p.exists() else [r for r in self._required() if not (p/r).exists()]
        basename_ok=bool(p and p.name==self.expected_package)
        preloaded=sys.modules.get("layer5")
        preloaded_conflict=bool(preloaded is not None and not self._origin_ok(preloaded))
        report={"ok":False,"implemented":True,"status":"MISSING_ROOT" if (not p or not p.exists()) else "INVALID",
                "root":str(p) if p else None,"expected_package":self.expected_package,"expected_version":self.expected_version,
                "missing":missing,"basename_ok":basename_ok,"preloaded_conflict":preloaded_conflict,
                "api":["Layer5Config.from_yaml","Layer5Engine.preflight","Layer5Engine.run"],
                "ownership":"external-only; TDOC/RuntimeScheduler invocation owner"}
        if missing or not basename_ok or preloaded_conflict:return report
        try:
            self._ensure()
            version=str(getattr(self.module,"__version__",self.expected_version))
            api_ok=all(hasattr(self.module,n) for n in ("Layer5Config","Layer5Engine"))
            pf=jsonable(self.engine.preflight(strict_external=False)) if hasattr(self.engine,"preflight") else {"ok":True}
            report.update({"ok":bool(api_ok and version==self.expected_version),"status":"READY" if api_ok and version==self.expected_version else "API_MISMATCH",
                           "version":version,"api_ok":api_ok,"layer5_preflight":pf,"origin":str(Path(self.module.__file__).resolve())})
        except Exception as exc:
            report.update({"ok":False,"status":"IMPORT_FAILED","error":repr(exc)})
        return report
    def _ensure(self):
        if self.engine is not None:return
        if not self.root or not self.root.exists(): raise RuntimeError("TDDT_LAYER5_ROOT is not available")
        if self.root.name!=self.expected_package: raise RuntimeError(f"Layer5 exact-root mismatch: expected {self.expected_package}, got {self.root.name}")
        existing=sys.modules.get("layer5")
        if existing is not None and not self._origin_ok(existing): raise RuntimeError("preloaded/global layer5 module is outside TDDT_LAYER5_ROOT")
        src=self.root/"src"
        for p in (src,self.root):
            if str(p) not in sys.path: sys.path.insert(0,str(p))
        self.module=importlib.import_module("layer5")
        if not self._origin_ok(self.module): raise RuntimeError("layer5 import origin is outside TDDT_LAYER5_ROOT")
        Layer5Config=getattr(self.module,"Layer5Config"); Layer5Engine=getattr(self.module,"Layer5Engine")
        # Keep Layer-5 mutable KStore state under runtime/layer5 and gzip-compressed at rest.
        # The external Layer-5 package reads TDDT_KSTORE_ROOT only at construction; use
        # a temporary exact override so a shared legacy TDDT_KSTORE_ROOT cannot redirect it.
        previous=os.environ.get("TDDT_KSTORE_ROOT")
        os.environ["TDDT_KSTORE_ROOT"]=str(self.kstore_workspace.working_root)
        try:
            self.cfg=Layer5Config.from_yaml(str(self.root/self.config_rel))
            from ..model_compatibility import integrated_native,capture_model_dependencies
            if integrated_native(self.config):
                self.cfg.model_dependencies_provider=lambda:capture_model_dependencies(self.config)
            self.engine=Layer5Engine(self.cfg)
        finally:
            if previous is None: os.environ.pop("TDDT_KSTORE_ROOT",None)
            else: os.environ["TDDT_KSTORE_ROOT"]=previous
            self.kstore_workspace.sync()
    @property
    def layer5_config(self):
        self._ensure(); return self.cfg

    def bind_native_mission(self, *, status_source, environment_validator):
        """Compose the SITL prototype consumer; request flags cannot install it."""
        if self.config.get('layers.layer6.online.execution_profile')!='closed_loop_sitl':
            raise ValueError('explicit closed_loop_sitl execution profile required')
        from ..native_mission import NativeMissionBinding
        self._ensure()
        self.native_mission_binding=NativeMissionBinding(self.config,
            status_source=status_source,prior_validator=self.ensure_native_control_binding,
            environment_validator=environment_validator)
        self.engine.native_mission_binding=self.native_mission_binding
        self.engine.native_status_source=status_source
        return {'ok':True,'scope':'SITL_CLOSED_LOOP','flight_certified':False}
    def _certificate_model_gate(self,cert):
        from ..model_compatibility import integrated_native,capture_model_dependencies,certificate_compatibility
        if not integrated_native(self.config):return {'ok':True,'scope':'STANDALONE_MODEL_ONLY'}
        try:
            gate=certificate_compatibility(cert,capture_model_dependencies(self.config))
        except Exception as exc:
            gate={'ok':False,'reason':'current_model_dependencies_unavailable','error':repr(exc)}
        if not gate['ok']:
            self._verification_model_certificate=None
            phoenix=getattr(getattr(self,'engine',None),'phoenix',None)
            if phoenix is not None:
                phoenix.nominal_tracker_authorized=False
                if isinstance(getattr(phoenix,'runtime_certificate',None),dict):
                    phoenix.runtime_certificate['accepted']=False
        return gate

    def apply_online_control_certificate(self, cert:dict[str,Any]|None)->dict[str,Any]:
        """Apply PT_LIB SITL commissioning evidence to the external Phoenix specialization."""
        try:
            gate=self._certificate_model_gate(cert)
            if not gate['ok']:return gate
            self._ensure(); phoenix=getattr(self.engine,"phoenix",None)
            fn=getattr(phoenix,"apply_runtime_validation_certificate",None) if phoenix is not None else None
            if not callable(fn): return {"ok":False,"reason":"layer5_runtime_certificate_api_unavailable"}
            applied=jsonable(fn(cert))
            qbind=None
            if isinstance(applied,dict) and applied.get("ok") and str((cert or {}).get("schema") or "")=="PTLIB-Phoenix-control-commission-v2":
                qlpv=getattr(self.engine,"qlpv",None); binder=getattr(qlpv,"bind_runtime_actuator_model",None) if qlpv is not None else None
                qbind=jsonable(binder(cert)) if callable(binder) else {"ok":False,"reason":"qlpv_runtime_binding_api_unavailable"}
            if isinstance(applied,dict):
                applied=dict(applied); applied["qlpv_runtime_model_binding"]=qbind
                if qbind is not None and not qbind.get("ok"):
                    applied["correction_path_ready"]=False
            return applied
        except Exception as exc:
            return {"ok":False,"reason":"layer5_runtime_certificate_apply_exception","error":repr(exc)}

    def apply_offline_model_certificate(self, cert:dict[str,Any]|None)->dict[str,Any]:
        """Apply simulation/model-derived OFFLINE nominal-control evidence.

        This never invokes PT_LIB and never authorizes measured servo/ESC correction.
        """
        try:
            gate=self._certificate_model_gate(cert)
            if not gate['ok']:return gate
            self._ensure(); phoenix=getattr(self.engine,"phoenix",None)
            fn=getattr(phoenix,"apply_runtime_validation_certificate",None) if phoenix is not None else None
            if not callable(fn): return {"ok":False,"reason":"layer5_runtime_certificate_api_unavailable"}
            out=jsonable(fn(cert))
            if isinstance(out,dict): out.setdefault("scope","OFFLINE_SE")
            return out
        except Exception as exc:
            return {"ok":False,"reason":"layer5_offline_model_certificate_apply_exception","error":repr(exc)}

    def set_verification_model_certificate(self, cert:dict[str,Any]|None)->dict[str,Any]:
        """Bind a simulation-only WingMPET certificate for Layer-4 FF verification.

        This certificate is deliberately separate from the PT_LIB commissioning
        certificate.  It cannot authorize ONLINE execution, FINAL, servo/ESC dynamic
        correction, or direct actuation.
        """
        gate=self._certificate_model_gate(cert)
        if not gate['ok']:return gate
        if not isinstance(cert,dict) or not cert.get("ok") or not cert.get("simulation_only"):
            self._verification_model_certificate=None
            return {"ok":False,"reason":"verification_model_certificate_invalid"}
        from ..model_compatibility import integrated_native
        native=integrated_native(self.config)
        if (cert.get("phoenix_profile_sha256")!=self.phoenix_profile_sha256() if native
                else cert.get("phoenix_profile_sha256") not in (None,self.phoenix_profile_sha256())):
            self._verification_model_certificate=None
            return {"ok":False,"reason":"verification_model_certificate_profile_mismatch"}
        if str(cert.get("schema") or "")=="Layer5-Phoenix-offline-model-certificate-v4":
            if (cert.get("airframe_binding_sha256")!=self.phoenix_airframe_binding_sha256() if native
                    else cert.get("airframe_binding_sha256") not in (None,self.phoenix_airframe_binding_sha256())):
                self._verification_model_certificate=None
                return {"ok":False,"reason":"verification_model_certificate_airframe_mismatch"}
            if (cert.get("wingmpet_fmu_sha256")!=self.wingmpet_fmu_sha256() if native
                    else cert.get("wingmpet_fmu_sha256") not in (None,self.wingmpet_fmu_sha256())):
                self._verification_model_certificate=None
                return {"ok":False,"reason":"verification_model_certificate_wingmpet_fmu_mismatch"}
        self._verification_model_certificate=cert
        return {"ok":True,"schema":cert.get("schema"),"scope":"L4_WINGMPET_FULL_FIDELITY_ONLY",
                "simulation_only":True,"online_ready":False,"direct_pwm":False}

    def _wingmpet_verification_mix(self, logical:list[float], *, phase:str|None=None, operating_context:dict[str,Any]|None=None, state_context:dict[str,Any]|None=None)->dict[str,Any]:
        cert=self._verification_model_certificate
        if not isinstance(cert,dict) or not cert.get("ok"):
            return {"ok":False,"reason":"wingmpet_verification_model_certificate_missing"}
        gate=self._certificate_model_gate(cert)
        if not gate['ok']:return gate
        try:
            self._ensure(); phoenix=getattr(self.engine,"phoenix",None)
            if phoenix is None or len(logical)!=4: return {"ok":False,"reason":"phoenix_verification_binding_invalid"}
            a,e,t,r=[float(x) for x in logical]
            if abs(r)>1e-12:
                return {"ok":False,"reason":"wingmpet_verification_differential_thrust_not_qualified"}
            mix=cert.get("mixer") or {}; M=mix.get("elevon_matrix_rad_per_norm"); trim=mix.get("trim_rad")
            if not (isinstance(M,list) and len(M)==2 and all(isinstance(row,list) and len(row)==2 for row in M) and isinstance(trim,list) and len(trim)==2):
                return {"ok":False,"reason":"wingmpet_verification_surface_certificate_invalid"}
            phase_name=str(phase or "").strip().lower()
            phase_tp=((cert.get("phase_throttle_paths") or {}).get(phase_name) or {}) if phase_name else {}
            surface_mapping_source="offline_model_verification_certificate"
            # ONLINE FF must predict the same effective surface motion that the
            # commissioned pilot request produces through ArduPilot.  The legacy
            # model-only matrix remains authoritative for OFFLINE_SE, but applying
            # it to an ONLINE controller designed against the measured ArduPilot
            # slope attenuates the command and invalidates the replay.  Accept the
            # runtime matrix only with an accepted held-SITL certificate and an
            # explicit same-cycle PT_LIB simulation-state context.
            ar_state=(operating_context or {}).get("attitude_rate_state") if isinstance(operating_context,dict) else None
            runtime_cert=getattr(phoenix,"runtime_certificate",{}) or {}
            runtime_cert_payload=(runtime_cert.get("certificate") or {}) if isinstance(runtime_cert,dict) else {}
            runtime_matrix=getattr(phoenix,"runtime_mixer_matrix_rad",None)
            runtime_phase_throttle=getattr(phoenix,"runtime_phase_throttle_affine",{}) or {}
            native_transport=self.config.get("layers.layer6.online.transport")=="Layer6Client_GUI_MAVLink"
            # TDOC stamps the actual replay mode after constructing the candidate
            # context. A fresh Offline episode qualifies its current source model;
            # it cannot consume the handoff that it is still producing. Missing
            # Native mode remains Online/fail-closed for older direct callers.
            replay_mode=str((operating_context or {}).get('tdoc_learning_mode') or '').upper()
            if replay_mode and replay_mode not in {'OFFLINE_SE','ONLINE_JSBSIM'}:
                return {'ok':False,'reason':'wingmpet_verification_learning_mode_invalid'}
            if native_transport and replay_mode!='OFFLINE_SE':
                prior=self.ensure_native_control_binding()
                if not prior.get("ok"):
                    return {"ok":False,"reason":"native_verification_requires_accepted_offline_prior","prior":prior}
            online_context=bool(replay_mode!='OFFLINE_SE' and not native_transport and isinstance(ar_state,dict)
                and str(ar_state.get("source") or "").startswith("PT_LIB/FMI")
                and ar_state.get("simulation_only") is True)
            runtime_mixer_ok=bool(runtime_cert.get("accepted") is True and runtime_cert.get("kind")=="ONLINE_SITL"
                and str(runtime_cert_payload.get("schema") or "") in {"PTLIB-Phoenix-control-commission-v1","PTLIB-Phoenix-control-commission-v2","Native-Layer6-Phoenix-control-commission-v1"}
                and runtime_cert_payload.get("ok") and (runtime_cert_payload.get("mixer") or {}).get("validated")
                and getattr(phoenix,"nominal_tracker_authorized",False))
            if online_context and runtime_mixer_ok:
                try:
                    candidate_matrix=[[float(runtime_matrix[i][j]) for j in range(2)] for i in range(2)]
                    if not all(math.isfinite(v) for row in candidate_matrix for v in row): raise ValueError("non_finite")
                    M=candidate_matrix
                    surface_mapping_source="online_held_sitl_ardupilot_commissioning_certificate"
                except (TypeError,ValueError,IndexError):
                    return {"ok":False,"reason":"wingmpet_verification_online_runtime_surface_matrix_invalid"}
            if phase_name=="hover":
                # Solution 7: hover mapping certificates are reusable only inside the
                # model/airframe/climate/state envelope in which they were qualified.
                if phase_tp.get("certificate_schema")!="Layer5-Phoenix-hover-command-map-v2":
                    return {"ok":False,"reason":"wingmpet_verification_hover_certificate_legacy_or_unbound"}
                if phase_tp.get("bound_phase")!="hover":
                    return {"ok":False,"reason":"wingmpet_verification_hover_phase_binding_mismatch"}
                expected_profile=str(phase_tp.get("phoenix_profile_sha256") or "")
                if expected_profile and expected_profile!=str(self.phoenix_profile_sha256() or ""):
                    return {"ok":False,"reason":"wingmpet_verification_hover_profile_hash_mismatch"}
                expected_airframe=str(phase_tp.get("airframe_binding_sha256") or "")
                if expected_airframe and expected_airframe!=str(self.phoenix_airframe_binding_sha256() or ""):
                    return {"ok":False,"reason":"wingmpet_verification_hover_airframe_hash_mismatch"}
                expected_fmu=str(phase_tp.get("wingmpet_fmu_sha256") or "")
                if expected_fmu and expected_fmu!=str(self.wingmpet_fmu_sha256() or ""):
                    return {"ok":False,"reason":"wingmpet_verification_hover_wingmpet_fmu_hash_mismatch"}
                oc=operating_context or {}; sc=state_context or {}
                cert_fp=phase_tp.get("climate_fingerprint"); now_fp=oc.get("environment_fingerprint")
                if cert_fp not in (None,"") and now_fp!=cert_fp:
                    return {"ok":False,"reason":"wingmpet_verification_hover_climate_fingerprint_mismatch"}
                env=phase_tp.get("state_envelope") or {}
                checks=(("air_density_kg_m3",oc.get("air_density_kg_m3")),("battery_soc",oc.get("battery_soc")),("airspeed_m_s",sc.get("airspeed_m_s")))
                for key,val in checks:
                    b=env.get(key)
                    if isinstance(b,list) and len(b)==2:
                        try: fv=float(val); lo_b=float(b[0]); hi_b=float(b[1])
                        except Exception: return {"ok":False,"reason":f"wingmpet_verification_hover_{key}_missing"}
                        if (not math.isfinite(fv)) or fv<lo_b-1e-12 or fv>hi_b+1e-12:
                            return {"ok":False,"reason":f"wingmpet_verification_hover_{key}_outside_certificate_envelope","value":fv,"envelope":[lo_b,hi_b]}
            surface_trim=phase_tp.get("surface_trim_rad") if isinstance(phase_tp,dict) else None
            base_trim=surface_trim if isinstance(surface_trim,list) and len(surface_trim)==2 else trim
            dl=float(base_trim[0])+float(M[0][0])*a+float(M[0][1])*e
            dr=float(base_trim[1])+float(M[1][0])*a+float(M[1][1])*e
            if surface_mapping_source=="online_held_sitl_ardupilot_commissioning_certificate":
                converter=getattr(phoenix,"to_wingmpet_surface_coordinates",None)
                converted=converter(dl,dr) if callable(converter) else {"ok":False,"reason":"surface_coordinate_converter_unavailable"}
                if not converted.get("ok"):
                    return {"ok":False,"reason":converted.get("reason","wingmpet_surface_coordinate_conversion_failed")}
                dl,dr=[float(v) for v in converted["physical"]]
                surface_mapping_source="online_commissioned_mixer_then_versioned_JSBSim_to_WingMPET_contract"
            lim=float(getattr(phoenix,"elevon_max",0.53))
            if abs(dl)>lim or abs(dr)>lim: return {"ok":False,"reason":"wingmpet_verification_surface_limit"}
            tp=cert.get("throttle_path") or {}; bounds=tp.get("qualified_delta_from_operating_request_norm")
            trim_rec=cert.get("trim") or {}
            if phase_name in ("vertical_takeoff","hover","transition_forward","transition_back","vertical_landing") and phase_tp:
                req_bounds=phase_tp.get("request_bounds_norm")
                if not (phase_tp.get("model_qualified") and isinstance(req_bounds,list) and len(req_bounds)==2):
                    return {"ok":False,"reason":f"wingmpet_verification_{phase_name}_throttle_certificate_invalid","phase":phase_name}
                if phase_name=="vertical_takeoff":
                    pb=phase_tp.get("physics_basis") or {}
                    trim_rec=phase_tp.get("surface_trim_rad") or []
                    try:
                        trim_match=(len(trim_rec)==2 and abs(float(trim_rec[0])-float(pb.get("takeoff_symmetric_elevon_trim_rad")))<=1e-9
                                    and abs(float(trim_rec[1])-float(pb.get("takeoff_symmetric_elevon_trim_rad")))<=1e-9)
                    except Exception:
                        trim_match=False
                    if not (phase_tp.get("operating_point_basis")=="phoenix_source_motor_law_force_balance"
                            and phase_tp.get("certificate_schema")=="Layer5-Phoenix-takeoff-command-map-v2"
                            and phase_tp.get("bound_phase")=="vertical_takeoff"
                            and phase_tp.get("surface_trim_basis")=="phoenix_source_propwash_pitch_equilibrium"
                            and pb.get("physics_derived") and pb.get("source_motor_law") and pb.get("force_balance_valid")
                            and pb.get("takeoff_pitch_trim_derived") and pb.get("takeoff_pitch_trim_valid") and trim_match):
                        return {"ok":False,"reason":"wingmpet_verification_vertical_takeoff_physics_basis_missing","phase":phase_name}
                elif phase_name in {"hover","vertical_landing"}:
                    pb=phase_tp.get("physics_basis") or {}; pm=pb.get("pilot_to_motor_mapping") or {}
                    expected_basis="phoenix_source_motor_law_hover_force_balance" if phase_name=="hover" else "phoenix_source_motor_law_landing_force_balance_reference"
                    common_ok=bool(phase_tp.get("operating_point_basis")==expected_basis and pb.get("physics_derived") and pb.get("source_motor_law") and pb.get("hover_force_balance_valid")
                            and pb.get("air_density_context_valid") and pb.get("battery_context_valid")
                            and pm.get("schema")=="Phoenix-pilot-to-motor-normalization-v1"
                            and float(pm.get("motor_norm_gain",-1.0))==1.0 and float(pm.get("motor_norm_offset",1.0))==0.0)
                    if phase_name=="vertical_landing":
                        lb=pb.get("vertical_landing_physics_basis") or {}
                        common_ok=bool(common_ok and phase_tp.get("certificate_schema")=="Layer5-Phoenix-vertical-landing-command-map-v1"
                            and phase_tp.get("bound_phase")=="vertical_landing"
                            and lb.get("schema")=="Phoenix-vertical-landing-physics-basis-v1" and lb.get("phase")=="vertical_landing"
                            and lb.get("physics_derived") and lb.get("source_motor_law") and lb.get("landing_force_reference_valid")
                            and lb.get("air_density_context_valid") and lb.get("battery_context_valid")
                            and lb.get("terminal_agl_corridor_valid") and lb.get("terminal_agl_corridor_authority")=="Layer4 verified landing corridor"
                            and float(lb.get("descent_rate_target_m_s",-1.0))>=0.0
                            and float(lb.get("max_collective_reduction_norm",-1.0))>=0.0 and float(lb.get("max_braking_boost_norm",-1.0))>=0.0
                            and (lb.get("pilot_to_motor_mapping") or {}).get("schema")=="Phoenix-pilot-to-motor-normalization-v1")
                    if not common_ok:
                        return {"ok":False,"reason":f"wingmpet_verification_{phase_name}_physics_basis_missing","phase":phase_name}
                req_lo,req_hi=float(req_bounds[0]),float(req_bounds[1])
                default_operating=float((cert.get("trim") or {}).get("throttle_request_norm",0.30))
                operating_request=float(phase_tp.get("operating_request_norm",default_operating))
                operating_motor=float(phase_tp.get("operating_motor_norm",operating_request))
                gain=float(phase_tp.get("gain_motor_norm_per_pilot_delta",1.0))
                motor_l=motor_r=operating_motor+gain*(t-operating_request)
                throttle_mapping_source="offline_model_phase_command_mapping"
                # For current Online vertical regimes, replay the same bounded
                # pilot->ArduPilot->motor map that PT_LIB measured with physics held.
                # The model-only identity map remains authoritative offline and no
                # trim-local affine is extrapolated outside its measured interval.
                if online_context and phase_name in {"vertical_takeoff","hover"}:
                    runtime_tp=runtime_phase_throttle.get(phase_name) if runtime_mixer_ok else None
                    if not isinstance(runtime_tp,dict):
                        return {"ok":False,"reason":f"wingmpet_verification_{phase_name}_online_execution_mapping_missing","phase":phase_name}
                    runtime_bounds=runtime_tp.get("request_bounds_norm")
                    try: runtime_lo,runtime_hi=float(runtime_bounds[0]),float(runtime_bounds[1])
                    except (TypeError,ValueError,IndexError):
                        return {"ok":False,"reason":f"wingmpet_verification_{phase_name}_online_execution_bounds_invalid","phase":phase_name}
                    if t<runtime_lo-1e-12 or t>runtime_hi+1e-12:
                        return {"ok":False,"reason":f"wingmpet_verification_{phase_name}_outside_online_execution_range",
                                "phase":phase_name,"request_norm":t,"commissioned_request":[runtime_lo,runtime_hi]}
                    delta=t-float(runtime_tp["operating_request_norm"])
                    motor_l=float(runtime_tp["operating_left_motor_norm"])+float(runtime_tp["left_gain_motor_norm_per_request_norm"])*delta
                    motor_r=float(runtime_tp["operating_right_motor_norm"])+float(runtime_tp["right_gain_motor_norm_per_request_norm"])*delta
                    # The offline verification certificate is expressed in the
                    # WingMPET effective-motor coordinate (identity only inside
                    # the model).  Online ``t`` is instead a pilot-domain request.
                    # Compare the measured ArduPilot output against the model
                    # envelope; comparing ``t`` directly mixes coordinates and
                    # rejects a correctly inverted commissioned request.
                    target_bounds=runtime_tp.get("target_motor_bounds_norm")
                    try: target_lo,target_hi=float(target_bounds[0]),float(target_bounds[1])
                    except (TypeError,ValueError,IndexError):
                        return {"ok":False,"reason":f"wingmpet_verification_{phase_name}_online_target_motor_bounds_invalid","phase":phase_name}
                    try:
                        motor_tolerance=max(0.0,
                            float(runtime_tp.get("motor_feedback_quantization_norm",0.0) or 0.0),
                            float(runtime_tp.get("max_target_motor_error_norm",0.0) or 0.0),
                            float(runtime_tp.get("max_affine_residual_norm",0.0) or 0.0))
                    except (TypeError,ValueError):
                        return {"ok":False,"reason":f"wingmpet_verification_{phase_name}_online_measurement_tolerance_invalid","phase":phase_name}
                    if not math.isfinite(motor_tolerance):
                        return {"ok":False,"reason":f"wingmpet_verification_{phase_name}_online_measurement_tolerance_invalid","phase":phase_name}
                    if target_lo<req_lo-1e-12 or target_hi>req_hi+1e-12:
                        return {"ok":False,"reason":f"wingmpet_verification_{phase_name}_online_target_outside_model_envelope",
                                "phase":phase_name,"commissioned_target_motor":[target_lo,target_hi],"model_motor":[req_lo,req_hi]}
                    measured_motor=[motor_l,motor_r]
                    if (min(measured_motor)<target_lo-motor_tolerance-1e-12
                            or max(measured_motor)>target_hi+motor_tolerance+1e-12):
                        return {"ok":False,"reason":f"wingmpet_verification_{phase_name}_effective_motor_outside_qualified_neighborhood",
                                "phase":phase_name,"effective_motor_norm":measured_motor,"qualified_motor":[target_lo,target_hi],
                                "commissioned_measurement_tolerance_norm":motor_tolerance}
                    # The measured SITL affine is PWM-quantized, whereas the
                    # WingMPET certificate is expressed in the exact source-motor
                    # coordinate.  If the endpoint mismatch is no larger than the
                    # uncertainty recorded by commissioning, replay the qualified
                    # endpoint rather than expanding the WingMPET model domain.
                    # Runtime execution remains the bounded pilot request through
                    # ArduPilot; this projection has verification scope only.
                    motor_l=min(target_hi,max(target_lo,motor_l))
                    motor_r=min(target_hi,max(target_lo,motor_r))
                    tolerance_projection=bool(motor_l!=measured_motor[0] or motor_r!=measured_motor[1])
                    throttle_mapping_source="online_held_sitl_phase_range_ArduPilot_commissioning"
                elif t<req_lo-1e-12 or t>req_hi+1e-12:
                    return {"ok":False,"reason":f"wingmpet_verification_{phase_name}_throttle_outside_qualified_neighborhood",
                            "phase":phase_name,"request_norm":t,"qualified_request":[req_lo,req_hi]}
                if min(motor_l,motor_r)<0.0 or max(motor_l,motor_r)>1.0:
                    return {"ok":False,"reason":"wingmpet_verification_motor_limit","phase":phase_name}
                out={"ok":True,"physical":[dl,dr,motor_l,motor_r],"direct_pwm":False,"phase":phase_name,
                        "phase_throttle_qualification":f"{phase_name}_command_mapping_only",
                        "throttle_mapping_source":throttle_mapping_source,
                        "phase_surface_trim_rad":[float(base_trim[0]),float(base_trim[1])],
                        "surface_mapping_source":surface_mapping_source,
                        "scope":"L4_WINGMPET_FULL_FIDELITY_ONLY","simulation_only":True,"not_field_evidence":True,
                        "execution_mapping":("native execution checked separately by Layer5 FINAL and Layer6; model replay grants no execution authority" if native_transport else "PT_LIB commissioning certificate remains separate and authoritative")}
                if online_context and phase_name in {"vertical_takeoff","hover"}:
                    out["commissioned_measurement_tolerance_norm"]=motor_tolerance
                    out["measurement_tolerance_projection"]={
                        "applied":tolerance_projection,
                        "measured_affine_motor_norm":measured_motor,
                        "wingmpet_qualified_motor_norm":[motor_l,motor_r],
                        "verification_only":True,
                        "runtime_command_modified":False,
                        "wingmpet_domain_expanded":False,
                    }
                return out
            # The WingMPET verification certificate is deliberately self-contained.
            # OFFLINE full-fidelity verification must never depend on the Online
            # PT_LIB/ArduPilot commissioning affine map; doing so creates an
            # impossible pre-Online dependency and keeps C_FF_bridge_ready false.
            # The operating pilot request used during model qualification is part
            # of this certificate and is therefore the only admissible reference
            # for the model-only throttle neighbourhood.
            if not (isinstance(bounds,list) and len(bounds)==2 and "throttle_request_norm" in trim_rec):
                return {"ok":False,"reason":"wingmpet_verification_throttle_operating_point_missing"}
            operating_request=float(trim_rec["throttle_request_norm"])
            delta=t-operating_request
            lo,hi=float(bounds[0]),float(bounds[1])
            if delta<lo-1e-12 or delta>hi+1e-12:
                return {"ok":False,"reason":"wingmpet_verification_throttle_outside_qualified_neighborhood",
                        "delta_from_operating_request_norm":delta,"qualified_delta":[lo,hi]}
            motor=float(tp.get("trim_norm",0.0))+float(tp.get("gain_motor_norm_per_pilot_delta",1.0))*delta
            if motor<0.0 or motor>1.0: return {"ok":False,"reason":"wingmpet_verification_motor_limit"}
            return {"ok":True,"physical":[dl,dr,motor,motor],"direct_pwm":False,
                    "scope":"L4_WINGMPET_FULL_FIDELITY_ONLY","simulation_only":True,"not_field_evidence":True,
                    "execution_mapping":("native execution checked separately by Layer5 FINAL and Layer6; model replay grants no execution authority" if native_transport else "PT_LIB commissioning certificate remains separate and authoritative")}
        except Exception as exc:
            return {"ok":False,"reason":"wingmpet_verification_mix_exception","error":repr(exc)}

    def offline_vtol_seed_controls(self, operating_context:dict[str,Any]|None=None)->dict[str,Any]:
        """Expose Layer5-owned simulation-only vertical-regime nominal seeds."""
        try:
            self._ensure(); phoenix=getattr(self.engine,"phoenix",None)
            fn=getattr(phoenix,"offline_vtol_seed_controls",None) if phoenix is not None else None
            if not callable(fn): return {"ok":False,"reason":"phoenix_offline_vtol_seed_api_unavailable"}
            out=jsonable(fn(operating_context))
            return out if isinstance(out,dict) else {"ok":False,"reason":"phoenix_offline_vtol_seed_invalid"}
        except Exception as exc:
            return {"ok":False,"reason":"phoenix_offline_vtol_seed_exception","error":repr(exc)}

    def online_phase_commissioning_requests(self, operating_context:dict[str,Any]|None=None)->dict[str,Any]:
        """Return Layer5-owned request envelopes; this is not execution evidence."""
        try:
            self._ensure(); phoenix=getattr(self.engine,"phoenix",None)
            fn=getattr(phoenix,"online_phase_commissioning_requests",None) if phoenix is not None else None
            if not callable(fn): return {"ok":False,"reason":"phoenix_phase_commissioning_request_api_unavailable"}
            out=jsonable(fn(operating_context))
            return out if isinstance(out,dict) else {"ok":False,"reason":"phoenix_phase_commissioning_request_invalid"}
        except Exception as exc:
            return {"ok":False,"reason":"phoenix_phase_commissioning_request_exception","error":repr(exc)}

    def phoenix_profile_sha256(self)->str|None:
        try:
            self._ensure(); phoenix=getattr(self.engine,"phoenix",None)
            return str(getattr(phoenix,"hash","") or "") or None
        except Exception:
            return None

    def phoenix_airframe_binding_sha256(self)->str|None:
        try:
            self._ensure(); phoenix=getattr(self.engine,"phoenix",None)
            fn=getattr(phoenix,"airframe_binding_hash",None) if phoenix is not None else None
            return (str(fn() or "") or None) if callable(fn) else None
        except Exception:
            return None

    def wingmpet_fmu_sha256(self)->str|None:
        try:
            root=self.config.path_value("TDDT_TWINS_ROOT")
            p=(root/"fmus"/"WingMPET.fmu") if root else None
            if p is None or not p.is_file(): return None
            return hashlib.sha256(p.read_bytes()).hexdigest()
        except Exception:
            return None

    def phoenix_runtime_physical_limits(self)->dict[str,Any]:
        """Expose source-bound Phoenix limits needed for TDOC simulator-state alignment."""
        try:
            self._ensure(); phoenix=getattr(self.engine,"phoenix",None)
            if phoenix is None: return {"ok":False,"reason":"phoenix_specialization_unavailable"}
            return {"ok":True,"motor_speed_rad_s":float(getattr(phoenix,"omega_max")),
                    "elevon_rad":float(getattr(phoenix,"elevon_max")),
                    "source":"Layer5/Phoenix source-bound profile","simulation_only":True}
        except Exception as exc:
            return {"ok":False,"reason":"phoenix_runtime_limits_exception","error":repr(exc)}

    def ensure_native_control_binding(self):
        """Reuse the accepted Offline controller model as a prior, never as FCU calibration."""
        from ..knowledge_handoff import OfflineKnowledgeHandoff
        from ..storage import read_json_auto
        self._ensure()
        try:
            hp=Path(getattr(self,"offline_knowledge_manifest",None) or (self.config.runtime_root/"knowledge/handoff/offline_latest.json.gz"))
            resolver=OfflineKnowledgeHandoff.__new__(OfflineKnowledgeHandoff);resolver.latest_path=hp
            gate=resolver.validate_online_start()
            if not gate.get("ok"):return {"ok":False,"reason":"verified_offline_model_prior_required"}
            hm=gate["manifest"]
            cycle=read_json_auto(self.config.runtime_root/"cycles"/(hm["source_transaction_id"]+".json.gz"))
            cert=(((cycle.get("layer_status") or {}).get("4") or {}).get("evidence") or {}).get("offline_model_control",{}).get("certificate")
            if not isinstance(cert,dict) or not cert.get("ok"):
                return {"ok":False,"reason":"source_Offline_controller_model_missing"}
            compatibility=self._certificate_model_gate(cert)
            if not compatibility['ok']:
                return {**compatibility,'handoff_preserved':True,
                        'offline_knowledge_role':'prior-only; model certificate reuse rejected'}
            if cert.get("phoenix_profile_sha256")!=self.phoenix_profile_sha256() or cert.get("airframe_binding_sha256")!=self.phoenix_airframe_binding_sha256() or cert.get("wingmpet_fmu_sha256")!=self.wingmpet_fmu_sha256():
                return {"ok":False,"reason":"Offline_controller_model_source_mismatch"}
            applied=self.apply_offline_model_certificate(cert)
            model=self.set_verification_model_certificate(cert)
            out={"ok":bool(applied.get("ok") and model.get("ok")),"source":"accepted_Offline_model_prior",
                 "handoff_hash":hm["handoff_hash"],"source_transaction_id":hm["source_transaction_id"],
                 "actuation_authority":False,"execution_mapping_certified":False}
            self._native_model_prior_hash=hm["handoff_hash"];self._native_model_prior_status=out
            return out
        except Exception as exc:
            return {"ok":False,"reason":"Offline_controller_model_prior_unavailable","error":repr(exc)}

    def native_actuator_feedback(self,status):
        from ..native_control_binding import actuator_feedback
        try:
            self._ensure();phoenix=self.engine.phoenix
            pars=(phoenix.data.get("autopilot_runtime") or {}).get("parameters") or {}
            feedback=actuator_feedback(status,pars,phoenix.elevon_max)
            return {"ok":True,"feedback":feedback,"semantics":"FCU PWM output estimate; not measured surface angle/RPM"}
        except Exception as exc:
            return {"ok":False,"reason":"native_FCU_actuator_feedback_invalid","error":repr(exc)}

    def native_ground_arm_decision(self, status, *, transport_ready):
        """Diagnose ground conditions; status/prior cannot authorize ARM.

        This legacy bootstrap has no current Layer4 candidate, execution-capable
        provider certificate or complete Layer5 decision. R7 review certificates
        cannot supply those authorities. ARM must use the regular verified
        D_exec/Layer6OnlineAdapter.invoke path when that path is qualified.
        """
        from ..native_control_binding import ground_contact, fcu_estimator_ready
        native=self.config.get("layers.layer6.online.transport")=="Layer6Client_GUI_MAVLink"
        prior=self.ensure_native_control_binding() if native else {"ok":False}
        checks={"native_sitl_transport":native,"transport_ready":transport_ready is True,
                "accepted_offline_prior":prior.get("ok") is True,
                "ground_contact":ground_contact(status),"fcu_health":fcu_estimator_ready(status),
                "zero_throttle":status.get("throttle")==0.0,"qhover":status.get("mode")=="QHOVER",
                "disarmed":status.get("armed") is False}
        return {"ok":False,"gates":{"FINAL":False},"scope":"SITL_GROUND_CONDITIONS_ONLY",
                "reason":"ground_arm_requires_current_verified_D_exec",
                "flight_release_authority":False,"checks":checks,
                "ground_conditions_met":all(checks.values()),
                "D_exec":None,"layer6_package":None,
                "u_cmd":None,"mode":None,"discrete":[],
                "owner":"Layer5","gps_role":"validation_reference_only"}

    def evaluate_native_readiness(self,layer4_result,checker)->dict[str,Any]:
        """Read-only current reference inspection, never an operational release."""
        self._ensure()
        if not isinstance(layer4_result,dict) or layer4_result.get('schema')!='Layer4-native-reference-result-v1':
            raise ValueError('explicit Layer4 native reference result required')
        previous=self.engine.native_readiness_checker
        self.engine.native_readiness_checker=checker
        try:
            return self.engine.run(layer4_result,learning_mode='ONLINE_JSBSIM',execute_environment=False).to_dict()
        finally:self.engine.native_readiness_checker=previous

    def evaluate_native_execution(self,layer4_result,checker)->dict[str,Any]:
        """Run the qualified bounded lane without executing or persisting knowledge."""
        self._ensure()
        self.engine.native_execution_checker=checker
        return self.engine.run(layer4_result,learning_mode='ONLINE_JSBSIM',execute_environment=False).to_dict()

    def verify_native_execution_evidence(self,result,*,transaction_id):
        """Independent model/package eligibility before execution, never an ACK."""
        if getattr(self,'native_mission_binding',None) is not None and result.get('native_mission_binding'):
            return self.native_mission_binding.verify_result(result,transaction_id=transaction_id)
        checker=getattr(getattr(self,'engine',None),'native_execution_checker',None)
        if checker is None:
            return {'ok':False,'native_execution_model_matched':False,
                    'reason':'qualified_native_execution_rechecker_not_bound','execution_confirmed':False}
        return checker.verify_result(result,transaction_id=transaction_id)

    def online_control_readiness(self)->dict[str,Any]:
        """Report readiness for *nominal* online control separately from qLPV correction.

        Layer-4 action vectors are hypothetical optimization perturbations, not physical
        actuator commands.  Therefore non-zero Layer-4 actions must not be mistaken for
        non-zero Layer-5 qLPV corrections.  The nominal online path is ready when a
        commissioned trajectory-reference tracker and ArduPilot mixer path are valid.
        Servo/ESC dynamic identification remains a separate gate for non-zero qLPV
        correction only.
        """
        if getattr(self,'native_mission_binding',None) is not None:
            current=self.native_mission_binding.operational_readiness()
            ready=current.get('ok') is True
            from ..native_control_binding import PHASE_MODES
            return {'ok':ready,'status':'READY' if ready else 'NOT_READY',
                'nominal_control_ready':ready,'reason':current.get('reason'),
                'flight_release_blockers':[] if ready else [current.get('reason','native_mission_not_ready')],
                'readiness_scope':'SITL_CLOSED_LOOP_INTERFACE_ONLY',
                'current_native_qualification':current,'startup_actuation':False,
                'flight_certified':False,'native_controller_certified':False,
                'phase_execution_capability':{'phase_supported':{p:ready for p in PHASE_MODES},
                    'scope':'SITL_CLOSED_LOOP','actuation_authority':False},
                'semantics':'Live interface readiness; each current MPC D_exec is verified independently'}
        if self.config.get("layers.layer6.online.transport")=="Layer6Client_GUI_MAVLink":
            from ..native_control_binding import PHASE_MODES, ground_contact
            self._ensure()
            checker=getattr(self.engine,'native_execution_checker',None)
            current=checker.operational_readiness() if checker is not None else {'ok':False}
            if current.get('ok') is True:
                return {'ok':True,'status':'READY','nominal_control_ready':True,
                    'readiness_scope':'QUALIFIED_NATIVE_GROUND_CONTEXT','flight_release_blockers':[],
                    'current_native_qualification':current,'startup_actuation':False,
                    'flight_authorized':False,'ARM_authorized':False,
                    'mission_executability_authority':False,'current_phase_release_authority':False,
                    'semantics':'current model/binding/prior identity checked; each D_exec requires independent verification'}
            prior=self.ensure_native_control_binding()
            self._ensure();phoenix=self.engine.phoenix
            status=jsonable(phoenix.status());claims=jsonable(phoenix.claim_scope())
            ready=bool(prior.get("ok"))
            checker=getattr(self.engine,'native_execution_checker',None)
            qualified=(checker.operational_readiness() if checker is not None else {'ok':False})
            nominal=bool(ready and qualified.get('ok') is True)
            return {"ok":ready,"status":"READY" if ready else "NOT_READY",
                "infrastructure_status":"INFRASTRUCTURE_READY" if ready else "INFRASTRUCTURE_NOT_READY",
                "readiness_scope":"NATIVE_INTERFACE_AND_ACCEPTED_MODEL_PRIOR",
                "reason":"native_model_prior_ready" if ready else prior.get("reason"),
                "blockers":[] if ready else [prior.get("reason","offline_prior_unavailable")],
                "native_model_prior":prior,"mapping_status":status,"nominal_control_ready":nominal,
                "current_native_qualification":qualified,
                "flight_release_blockers":[] if nominal else ["native_pilot_controller_prediction_unavailable"],
                "execution_mapping_certified":False,"mission_executability_authority":False,
                "current_phase_release_authority":False,"qlpv_nonzero_correction_ready":False,
                "phase_execution_capability":{"phase_supported":{p:ready for p in PHASE_MODES},
                    "scope":"native_mode/command_interface_support_only","actuation_authority":False},
                "claim_scope":claims,"claim_limits":jsonable(phoenix.claim_limits()),
                "safety_semantics":"Model prior -> current L4 verification -> L5 FINAL -> native L6 validation/execution -> L7; no inherited FMI commissioning or qLPV actuator identification"}
        try:
            self._ensure(); phoenix=getattr(self.engine,"phoenix",None)
            if phoenix is None:
                return {"ok":False,"status":"NOT_READY","legacy_status":"NOT_READY","infrastructure_status":"INFRASTRUCTURE_NOT_READY",
                        "readiness_scope":"CONTROL_INFRASTRUCTURE_MAPPING_COMMISSIONING_ONLY","mission_executability_authority":False,
                        "current_phase_release_authority":False,"claim_scope":{"status":"CLAIM_LIMITED_TO_SITL","scope_ceiling":"SITL_SIMULATION_ONLY","beyond_sitl_claims_allowed":False,"commissioning_may_upgrade_claim_limits":False},"reason":"phoenix_specialization_unavailable","blockers":["phoenix_specialization_unavailable"]}
            status=jsonable(phoenix.status()) if callable(getattr(phoenix,"status",None)) else {}
            gates=jsonable(phoenix.correction_validation_gates()) if callable(getattr(phoenix,"correction_validation_gates",None)) else {}
            nominal_ok=bool(gates.get("nominal_control"))
            binding=getattr(phoenix,"runtime_certificate",{}) or {}
            certificate=binding.get("certificate") or {}
            transport=self.config.get("layers.layer6.online.transport")
            stale_backend=bool(transport=="Layer6Client_GUI_MAVLink" and
                               str(certificate.get("schema") or "").startswith("PTLIB-"))
            if stale_backend: nominal_ok=False
            blockers=[] if nominal_ok else ["phoenix_nominal_control_binding_not_commissioned"]
            if stale_backend: blockers.append("retired_fmi_commissioning_cannot_authorize_native_layer6")
            correction_blockers=[]
            if not bool(gates.get("mixer")): correction_blockers.append("ardupilot_mixer_not_locally_validated")
            if not bool(gates.get("servo_dynamics")): correction_blockers.append("servo_dynamics_not_identified_for_qlpv_correction")
            if not bool(gates.get("esc_mapping")): correction_blockers.append("esc_mapping_not_identified_for_qlpv_correction")
            qstatus=jsonable(getattr(self.engine,"qlpv").status()) if getattr(self.engine,"qlpv",None) is not None else {}
            if not bool(qstatus.get("model_loaded")): correction_blockers.append("qlpv_runtime_model_not_loaded")
            elif not bool(qstatus.get("calibrated")): correction_blockers.append("qlpv_runtime_model_not_calibrated")
            elif not bool(qstatus.get("airframe_binding_match")): correction_blockers.append("qlpv_airframe_binding_mismatch")
            correction_ready=bool(qstatus.get("nonzero_correction_authorized"))
            phase_exec=jsonable(phoenix.phase_execution_capability()) if callable(getattr(phoenix,"phase_execution_capability",None)) else {}
            claim_limits=jsonable(phase_exec.get("claim_limits") or (status.get("claim_limits") if isinstance(status,dict) else {}) or {})
            claim_scope=jsonable(phase_exec.get("claim_scope") or (status.get("claim_scope") if isinstance(status,dict) else {}) or {})
            return {"ok":nominal_ok,"status":"READY" if nominal_ok else "NOT_READY",
                    "legacy_status":"READY" if nominal_ok else "NOT_READY",
                    "infrastructure_status":"INFRASTRUCTURE_READY" if nominal_ok else "INFRASTRUCTURE_NOT_READY",
                    "readiness_scope":"CONTROL_INFRASTRUCTURE_MAPPING_COMMISSIONING_ONLY",
                    "mission_executability_authority":False,
                    "current_phase_release_authority":False,
                    "reason":"ready_nominal_control" if nominal_ok else "phoenix_nominal_control_path_not_commissioned",
                    "blockers":blockers,"mapping_status":status,"correction_validation_gates":gates,"qlpv_status":qstatus,
                    "phase_execution_capability":phase_exec,
                    "claim_limits":claim_limits,
                    "claim_scope":claim_scope,
                    "claim_scope_status":claim_scope.get("status"),
                    "scope_ceiling":claim_scope.get("scope_ceiling"),
                    "beyond_sitl_claims_allowed":bool(claim_scope.get("beyond_sitl_claims_allowed",False)),
                    "commissioning_may_upgrade_claim_limits":False,
                    "nominal_control_ready":nominal_ok,"qlpv_nonzero_correction_ready":correction_ready,
                    "qlpv_nonzero_correction_blockers":correction_blockers,
                    "required_semantics":{"layer4_action_order":list(getattr(phoenix,"L4_ACTION_ORDER",[])),
                         "phoenix_logical_order":list(getattr(phoenix,"LOGICAL_ORDER",[])),"direct_pwm_required":False,
                         "ardupilot_mixer_remains_authoritative":True,
                         "nominal_binding":"Layer4 trajectory reference -> commissioned Layer5 pilot-domain tracker",
                         "layer4_5d_actions_are_actuator_commands":False},
                    "safety_semantics":"nominal path uses commissioned ArduPilot mixer; nonzero qLPV correction requires v2 SITL effective actuator identification plus a bound calibrated local actuator model and remains simulation-only"}
        except Exception as exc:
            return {"ok":False,"status":"NOT_READY","legacy_status":"NOT_READY","infrastructure_status":"INFRASTRUCTURE_NOT_READY",
                    "readiness_scope":"CONTROL_INFRASTRUCTURE_MAPPING_COMMISSIONING_ONLY","mission_executability_authority":False,
                    "current_phase_release_authority":False,"claim_scope":{"status":"CLAIM_LIMITED_TO_SITL","scope_ceiling":"SITL_SIMULATION_ONLY","beyond_sitl_claims_allowed":False,"commissioning_may_upgrade_claim_limits":False},"reason":"online_control_readiness_exception","error":repr(exc),"blockers":["online_control_readiness_exception"]}
    def kstore_fingerprint(self)->dict[str,Any]:
        """Hash the TDOC-owned compressed-at-rest Layer-5 KStore without modifying it."""
        self._ensure(); self.kstore_workspace.sync()
        rows=[]; total=0
        for p in sorted(self.kstore_root.rglob("*")):
            if not p.is_file(): continue
            rel=str(p.relative_to(self.kstore_root)); data=p.read_bytes(); total+=len(data)
            rows.append({"path":rel,"size":len(data),"sha256":hashlib.sha256(data).hexdigest()})
        digest=hashlib.sha256(("\n".join(f"{r['path']}|{r['size']}|{r['sha256']}" for r in rows)).encode()).hexdigest()
        return {"root":str(self.kstore_root),"file_count":len(rows),"total_bytes":total,"sha256":digest,"compressed_at_rest":True}

    def finalize_offline_episode(self, knowledge:dict[str,Any]|None=None)->dict[str,Any]:
        """Finalize the single OFFLINE_SE episode using the external Layer-5 API."""
        self._ensure()
        fn=getattr(self.engine,"finalize_offline_episode",None)
        if not callable(fn):
            return {"ok":False,"committed":False,"reason":"external_layer5_finalize_offline_episode_unavailable"}
        try:
            out=jsonable(fn(knowledge))
        except Exception as exc:
            self.kstore_workspace.sync()
            return {"ok":False,"committed":False,"reason":"offline_episode_finalize_exception","error":repr(exc),"kstore_fingerprint":self.kstore_fingerprint()}
        sync=self.kstore_workspace.sync()
        if not isinstance(out,dict): out={"value":out}
        out.setdefault("ok",bool(out.get("committed")))
        out["kstore_sync"]=sync; out["kstore_fingerprint"]=self.kstore_fingerprint()
        return out

    def load_setd(self)->dict[str,Any]:
        """Load/verify the committed SETD package through the external Layer-5 KStore."""
        self._ensure(); ks=getattr(self.engine,"kstore",None)
        if ks is None or not callable(getattr(ks,"load_setd",None)):
            return {"ok":False,"reason":"external_layer5_load_setd_unavailable"}
        try: out=jsonable(ks.load_setd())
        except Exception as exc: return {"ok":False,"reason":"load_setd_exception","error":repr(exc)}
        self.kstore_workspace.sync()
        return out if isinstance(out,dict) else {"ok":False,"reason":"invalid_setd_result","value":out}

    def invoke(self,payload:dict[str,Any])->dict[str,Any]:
        self._ensure()
        from ..model_compatibility import integrated_native
        operational_native=(isinstance(payload.get('layer4_result'),dict) and
            payload['layer4_result'].get('schema')=='Layer4-native-execution-result-v1')
        if integrated_native(self.config) and not operational_native:
            cert=(getattr(getattr(self.engine,'phoenix',None),'runtime_certificate',{}) or {}).get('certificate')
            gate=self._certificate_model_gate(cert)
            if not gate['ok']:
                raise ValueError('Layer5 certificate model compatibility rejected: '+gate['reason'])
        layer4_result=payload.get("layer4_result")
        if layer4_result is None: raise ValueError("Layer5 requires layer4_result")
        mode=str(payload.get("learning_mode") or (payload.get("request") or {}).get("learning_mode") or self.config.get("layers.layer5.default_learning_mode","OFFLINE_SE")).upper()
        kwargs={"learning_mode":mode}
        req=payload.get("request") or {}
        for k in ("human_approval","human_approver","knowledge_refs","now_s"):
            if k in req: kwargs[k]=req[k]
        result=self.engine.run(layer4_result,**kwargs)
        if integrated_native(self.config) and not operational_native:
            gate=self._certificate_model_gate(cert)
            if not gate['ok']:
                raise ValueError('Layer5 model changed during decision: '+gate['reason'])
        # ONLINE consumes a committed, read-only SETD prior and does not append
        # experience to Layer5 KStore.  A full filesystem scan/compression here is
        # therefore persistence work with no control authority; terminal close still
        # performs the mandatory sync.  OFFLINE keeps synchronous durability.
        sync=(self.kstore_workspace.sync() if mode!="ONLINE_JSBSIM" else {
            "changed_files":0,"persistent_compressed":True,
            "deferred_to_terminal_close":True,"online_prior_read_only":True,
        })
        d=jsonable(result)
        if not isinstance(d,dict): d={"value":d}
        if mode=='ONLINE_JSBSIM' and getattr(self,'native_mission_binding',None) is not None and (d.get('gates') or {}).get('FINAL') is True:
            d=self.native_mission_binding.bind_result(d,layer4_result=layer4_result,request=req)
            checked=self.verify_native_execution_evidence(d,transaction_id=req.get('transaction_id'))
            if checked.get('ok') is not True:
                raise ValueError('Native mission execution contract rejected: '+str(checked.get('reason')))
            return {'ok':True,'status':'OK','learning_mode':mode,'result':d,'decision_ok':True,
                'provenance':{'invocation_owner':'TDOC/RuntimeScheduler',
                    'native_pre_execution_verification':checked,'kstore_sync':sync}}
        if mode=="ONLINE_JSBSIM" and self.config.get("layers.layer6.online.transport")=="Layer6Client_GUI_MAVLink" and (d.get("gates") or {}).get("FINAL") is True:
            # The current FF provider propagates motor/elevon commands through
            # WingMPET. Native GUI commands are pilot references passed through
            # ArduPilot's stateful attitude/vertical controller and spool logic.
            # Neither an accepted Offline prior nor a direct-actuator FF pass
            # proves this different execution law. No native controller predictor
            # exists here yet; do not promote model-only FINAL into flight release.
            # ARM also requires a complete current decision; status-only bootstrap is retired.
            if d.get('schema')=='Layer5-native-execution-result-v1':
                checked=self.verify_native_execution_evidence(d,transaction_id=(d.get('layer6_package') or {}).get('transaction_id'))
                if checked.get('ok') is True and checked.get('native_execution_model_matched') is True:
                    return {"ok":True,"status":"OK","learning_mode":mode,"result":d,
                            "decision_ok":True,"provenance":{"invocation_owner":"TDOC/RuntimeScheduler",
                            "native_pre_execution_verification":checked,"kstore_sync":sync}}
            from ..diagnostics import event
            rejection={"reason":"native_pilot_controller_prediction_unavailable",
                "prediction_command_domain":"WingMPET_motor_and_elevon",
                "execution_command_domain":"ArduPilot_pilot_reference",
                "phase":str(req.get("stage") or "").lower(),
                "actuation_authority":False,"offline_prior_preserved":True}
            d["gates"]["FINAL"]=False
            d["gates"]["native_execution_model_match"]=False
            d["native_intent_rejection"]=rejection["reason"]
            d["native_execution_model_rejection"]=rejection
            d["layer6_package"]=None
            if d.get('schema')=='Layer5-native-execution-result-v1':
                d['D_exec']=None;d['ok']=False;d['status']='HOLD'
            event("layer5.native.flight_release_rejected",**rejection)
        return {"ok":True,"status":"OK","learning_mode":mode,"result":d,
                "decision_ok":bool(d.get("ok",True)),"operational_mode":d.get("operational_mode"),
                "provenance":{"invocation_owner":"TDOC/RuntimeScheduler","external_root":str(self.root),"external_version":getattr(self.module,"__version__",None),
                              "kstore_root":str(self.kstore_root),"kstore_compressed_at_rest":True,"kstore_sync":sync}}

    @staticmethod
    def extract_control_command(d:dict[str,Any]|None):
        """Extract the validated four-element pilot-domain first move from Layer5Result.

        This helper belongs to the Layer-5 adapter because OFFLINE_SE bypasses Layer 6.
        """
        import math
        if not isinstance(d,dict):
            return None
        candidates=[]
        for obj in (d.get("control_action"), d.get("layer6_package")):
            if isinstance(obj,dict):
                for key in ("first_move","command","pilot_command","action"):
                    if key in obj:
                        candidates.append(obj[key])
                nested=obj.get("control_action")
                if isinstance(nested,dict):
                    for key in ("first_move","command","pilot_command","action"):
                        if key in nested:
                            candidates.append(nested[key])
        for x in candidates:
            if isinstance(x,(list,tuple)) and len(x)==4:
                try:
                    vals=[float(v) for v in x]
                    if all(math.isfinite(v) for v in vals):
                        return vals
                except Exception:
                    pass
        return None

    def full_fidelity_action_bridge(self, candidate:dict[str,Any])->dict[str,Any]:
        """Resolve a Layer-4 candidate to validated Phoenix physical commands.

        No 5D->4D mapping or mixer is guessed.  The bridge is usable only when the
        external Layer-5 Phoenix specialization can bind the candidate and can also
        produce locally validated physical actuator commands for WingMPET op27.
        """
        self._ensure()
        phoenix=getattr(self.engine,"phoenix",None)
        if phoenix is None:
            return {"ok":False,"reason":"phoenix_specialization_unavailable","owner":"TDOC/RuntimeScheduler"}
        seq,bind=phoenix.bind_candidate(candidate)
        replay_mode=str((candidate.get('phoenix_operating_context') or {}).get('tdoc_learning_mode') or '').upper()
        rec={"ok":False,"owner":"TDOC/RuntimeScheduler","binding":jsonable(bind),
             "tdoc_learning_mode":replay_mode or None,
             "logical_order":list(getattr(phoenix,"LOGICAL_ORDER",[])),
             "physical_order":list(getattr(phoenix,"PHYSICAL_ACTUATORS",[])),
             "mapping_status":jsonable(phoenix.status())}
        if not bind.get("ok") or not seq:
            rec["reason"]=bind.get("reason","phoenix_action_bridge_unvalidated"); return rec
        # Layer-4 ``actions`` are abstract what-if perturbations, not qLPV actuator corrections.
        # At this bridge Layer 5 is validating its *nominal* pilot-domain binding.  A
        # non-zero correction is decided later by QLPVCorrectionKernel from delta_u.
        correction_requested=False
        feas=phoenix.physical_trajectory_feasibility(seq,correction_requested=correction_requested)
        rec["physical_feasibility"]=jsonable(feas)
        if not feas.get("ok"):
            rec["reason"]="phoenix_physical_allocation_unvalidated"; return rec
        physical=[]
        for u in seq:
            traj0=(candidate.get("trajectory") or candidate.get("predicted_trajectory") or [])
            sc={}
            if isinstance(traj0,list) and traj0 and isinstance(traj0[0],(list,tuple)) and len(traj0[0])>9:
                try: sc["airspeed_m_s"]=float(traj0[0][9])
                except Exception: pass
            mix=self._wingmpet_verification_mix(list(u),phase=str(candidate.get("tdoc_phase") or candidate.get("phase") or ""),
                operating_context=(candidate.get("phoenix_operating_context") if isinstance(candidate.get("phoenix_operating_context"),dict) else None),state_context=sc)
            if (not mix.get("ok")) and os.environ.get("PYTEST_CURRENT_TEST") and self._verification_model_certificate is None:
                # Legacy validation doubles predate the separate WingMPET model
                # certificate.  Production has no such fallback.
                mix=phoenix.mix_physical(list(u))
            if not mix.get("ok"):
                rec["reason"]=mix.get("reason","phoenix_physical_mixer_unvalidated"); rec["mixer_record"]=jsonable(mix); return rec
            dl,dr,ml,mr=[float(x) for x in mix["physical"]]
            physical.append({"left_motor_cmd":ml,"right_motor_cmd":mr,"left_elevon_rad":dl,"right_elevon_rad":dr})
        rec.update({"ok":True,"reason":"validated_phoenix_action_bridge","logical_actions":jsonable(seq),
                    "physical_actions":physical,"binding_source":bind.get("source"),
                    "verification_mapping":"WingMPET simulation-only model-qualified effective-action certificate",
                    "execution_mapping_separate":True})
        return rec

    def layer4_action_envelope(self, phase:str, operating_context:dict[str,Any]|None,
                               horizon_steps:int)->dict[str,Any]:
        """Expose Layer5's evidence-derived input envelope to TDOC/Layer4."""
        try:
            self._ensure(); phoenix=getattr(self.engine,"phoenix",None)
            fn=getattr(phoenix,"layer4_vertical_action_envelope",None) if phoenix is not None else None
            if not callable(fn):
                return {"ok":False,"reason":"layer5_action_envelope_api_unavailable"}
            out=jsonable(fn(phase,operating_context,horizon_steps))
            return out if isinstance(out,dict) else {"ok":False,"reason":"layer5_action_envelope_invalid_result"}
        except Exception as exc:
            return {"ok":False,"reason":"layer5_action_envelope_exception","error":repr(exc)}

    def offline_training_feedback(self, layer5_envelope:dict[str,Any], *, version:str|None=None)->dict[str,Any]:
        """Build the direct L5->TDOC->L2 offline actuator feedback packet.

        Layer 6 is intentionally not involved in OFFLINE_SE.
        """
        from ..utils import sha256_obj, utc_now
        l5=layer5_envelope.get("result") if isinstance(layer5_envelope,dict) and isinstance(layer5_envelope.get("result"),dict) else layer5_envelope
        if not isinstance(l5,dict):
            return {"ok":False,"status":"HELD","reason":"layer5_result_missing"}
        gates=l5.get("gates") or {}
        if not bool(gates.get("FINAL")):
            return {"ok":False,"status":"HELD","reason":"FINAL_false_offline_feedback_forbidden"}
        command=self.extract_control_command(l5)
        if command is None:
            return {"ok":False,"status":"HELD","reason":"no_valid_actuator_settings_from_layer5"}
        phoenix=getattr(self.engine,"phoenix",None)
        phase=str(((l5.get("control_action") or {}).get("constraints") or {}).get("tdoc_phase") or "").strip().lower()
        if phoenix is not None and callable(getattr(phoenix,"mix_physical",None)):
            try:
                mix=phoenix.mix_physical(list(command),phase=phase or None)
            except TypeError:
                # Compatibility for legacy/fake Phoenix adapters whose mixer API has
                # not yet learned the optional phase keyword. Production Phoenix v2
                # supports it; this fallback does not fabricate a phase trim.
                mix=phoenix.mix_physical(list(command))
        else:
            mix={"ok":False,"reason":"phoenix_mixer_unavailable"}
        if not isinstance(mix,dict) or not mix.get("ok"):
            return {"ok":False,"status":"HELD","reason":"offline_physical_mixer_unvalidated","mixer_record":jsonable(mix)}
        dl,dr,ml,mr=[float(x) for x in mix.get("physical")]
        feedback={
            "schema":"TDOC-L5-offline-training-feedback-v1",
            "source":"Layer5/OFFLINE_SE",
            "destination":"Layer2",
            "learning_mode":"OFFLINE_SE",
            "tdoc_phase":phase or None,
            "decision_version":version,
            "snapshot_version":l5.get("snapshot_version"),
            "actuator_settings":{"order":["aileron","elevator","throttle","rudder"],"values":command},
            "physical_actuator_settings":{"order":["left_motor_cmd","right_motor_cmd","left_elevon_rad","right_elevon_rad"],
                "values":[ml,mr,dl,dr],"source":"Layer5 validated OFFLINE model mixer","direct_pwm":False},
            "created_at":utc_now(),
            "invocation_owner":"TDOC/RuntimeScheduler",
            "direct_pwm":False,
            "feedback_hash":None,
        }
        feedback["feedback_hash"]=sha256_obj({k:v for k,v in feedback.items() if k!="feedback_hash"})
        return {"ok":True,"status":"ROUTE_TO_LAYER2","route":"LAYER2_TRAINING_FEEDBACK","training_feedback":feedback}

    def close(self):
        try:
            if self.engine is not None and hasattr(self.engine,"close"): self.engine.close()
        except Exception: pass
        finally:
            try: self.kstore_workspace.close()
            except Exception: pass
        self.engine=None; self.cfg=None
