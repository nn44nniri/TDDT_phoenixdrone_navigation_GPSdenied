from __future__ import annotations
from typing import Any
import math, json, os
import numpy as np, yaml
from .config import Layer5Config
from .utils import clip, sha256_file, sha256_obj

class PhoenixSpecialization:
    L4_ACTION_ORDER=["delta_thrust_norm","delta_lift_norm","delta_pitch_rad","delta_yaw_rate_rad_s","delta_airspeed_m_s"]
    LOGICAL_ORDER=["aileron_domain","elevator_domain","throttle_domain","rudder_domain"]
    PHYSICAL_ACTUATORS=["left_elevon","right_elevon","left_motor","right_motor"]
    STATE_ORDER=["p_n_3","v_n_3","q_wxyz_4","omega_body_3","omega_L","omega_R","delta_L","delta_R","SOC"]
    def __init__(self, config: Layer5Config):
        self.config=config
        p=config.resolve(config.get("phoenix.profile_file","resources/phoenix_drone_v3.yaml"))
        if not p or not p.exists(): raise FileNotFoundError("Phoenix profile missing")
        self.path=p; self.data=yaml.safe_load(p.read_text(encoding="utf-8")) or {}; self.hash=sha256_file(p); self._extract()

    def _extract(self):
        d=self.data; ar=d.get("autopilot_runtime") or {}; pars=ar.get("parameters") or {}; sim=d.get("simulation_assumptions") or {}
        self.forward_only=bool(ar.get("forward_flight_only",False)); self.transition_validated=bool(sim.get("tailsitter_hover_transition_validated",False)); self.rudd_dt_gain=float(pars.get("RUDD_DT_GAIN",0.0) or 0.0)
        self.vtol_autopilot_controller_validated=bool(ar.get("vtol_autopilot_controller_validated",False))
        self.vtol_force_model_validated=bool(sim.get("vtol_force_model_validated",False))
        self.real_aircraft_flight_validated=bool(sim.get("real_aircraft_flight_validated",False))
        self.hover_equilibrium_validation=sim.get("hover_equilibrium_validation")
        self.true_zero_speed_hover_validated=bool(sim.get("true_zero_speed_90deg_hover_dynamic_validated",False))
        prop=d.get("propulsion") or {}; motor=prop.get("source_motor_model") or prop
        self.omega_max=float(motor.get("max_rot_velocity_rad_s",motor.get("max_rotor_speed_rad_s",800.0)))
        self.k_t=float(motor.get("motor_constant_N_per_rad_s_squared",motor.get("motor_constant_N_per_rad_s2",motor.get("motor_constant",7.864e-6))))
        self.k_q=float(motor.get("moment_constant_m",motor.get("moment_constant",0.023))); self.tau_up=float(motor.get("time_constant_up_s",0.016)); self.tau_down=float(motor.get("time_constant_down_s",0.020))
        self.thrust_max=float(motor.get("source_max_thrust_each_N_from_motor_law",prop.get("maximum_thrust_each_N",self.k_t*self.omega_max*self.omega_max)))
        self.elevon_max=float((d.get("control_limits") or {}).get("elevon_joint_limit_rad",0.53))
        self.mixer_validated=bool(self.config.get("phoenix.mixer.validated",False)); self.dt_validated=bool(self.config.get("phoenix.differential_thrust.validated",False)) and self.rudd_dt_gain != 0.0
        self.servo_identified=bool(self.config.get("phoenix.servo_dynamics.identified",False)); self.mapping_validated=bool(self.config.get("phoenix.mapping.validated",False)); self.pwm_validated=bool(self.config.get("phoenix.pwm.validated",False))
        self.direct_pwm_allowed=bool(self.config.get("phoenix.direct_pwm_allowed",False)) and self.pwm_validated
        self.aero_full_validated=bool(self.config.get("phoenix.aerodynamics.full_envelope_validated",False)); self.esc_mapping_identified=bool(self.config.get("phoenix.esc_mapping.identified",False))
        self.full_envelope=bool(self.aero_full_validated and self.transition_validated and self.servo_identified and self.mixer_validated and self.dt_validated)
        sd=self.config.get("phoenix.servo_dynamics",{}) or {}; self.servo_tau=[float(x) for x in sd.get("tau_s",[]) or []]; self.servo_rate=[float(x) for x in sd.get("rate_limit_rad_s",[]) or []]
        self.inertia=dict(d.get("inertia") or {}); self.geometry=dict(d.get("geometry") or {}); self.propwash=dict(((d.get("aerodynamics") or {}).get("official_propwash_plugin") or {})); self.uncertainty=dict(d.get("uncertainty") or {})
        self.forward_aero=dict(((d.get("aerodynamics") or {}).get("jsbsim_forward_flight_estimates") or {})); self.sensor_layout=dict(d.get("sensor_layout") or {})
        self.output_functions={f"SERVO{i}_FUNCTION":pars.get(f"SERVO{i}_FUNCTION") for i in range(1,5)}
        self.pwm={f"SERVO{i}":{"min":pars.get(f"SERVO{i}_MIN"),"trim":pars.get(f"SERVO{i}_TRIM"),"max":pars.get(f"SERVO{i}_MAX")} for i in range(1,5)}
        self.runtime_certificate={}
        self.runtime_mixer_matrix_rad=None
        self.runtime_mixer_trim_rad=None
        self.runtime_wingmpet_surface_matrix_rad=None
        self.runtime_wingmpet_surface_trim_rad=None
        self.runtime_surface_contract={}
        self.runtime_mixer_operating_input_norm=np.asarray([0.0,0.0],dtype=float)
        self.runtime_phase_surface_trim_rad={}
        self.runtime_throttle_affine={}
        self.runtime_phase_throttle_affine={}
        self.runtime_throttle_request_bounds=None
        self.runtime_trim={}
        self.nominal_tracker_authorized=False
        cert_path=os.environ.get("TDDT_PHOENIX_CONTROL_CERT") or self.config.get("phoenix.runtime_validation.certificate_file")
        if cert_path:
            try:
                cp=self.config.resolve(cert_path)
                if cp and cp.is_file(): self.apply_runtime_validation_certificate(json.loads(cp.read_text(encoding="utf-8")))
            except Exception:
                # Certificate loading is advisory here; readiness remains fail-closed.
                pass

    def apply_runtime_validation_certificate(self, cert:dict[str,Any]|None)->dict[str,Any]:
        """Apply bounded runtime evidence for the nominal Phoenix control binding.

        Two certificate classes are accepted and kept semantically separate:
        - ``PTLIB-Phoenix-control-commission-v1``: legacy static held-SITL commissioning for nominal ONLINE control.
        - ``PTLIB-Phoenix-control-commission-v2``: held-SITL commissioning plus simulation-only effective actuator identification for qLPV correction.
        - ``Layer5-Phoenix-offline-model-certificate-v1/v2/v3/v4``: simulation/model-derived local
          mixer + trim qualification for OFFLINE_SE only.

        The OFFLINE certificate never promotes servo/ESC measured-identification flags,
        never enables direct PWM, and must not be interpreted as ONLINE readiness.
        """
        c=dict(cert or {})
        schema=str(c.get("schema") or "")
        errors=[]
        provider=getattr(self.config,'model_dependencies_provider',None)
        if callable(provider):
            try:
                current=provider()
                if not (isinstance(current,dict) and current.get('model_id') and c.get('model_dependencies')==current):
                    errors.append('model_dependencies_missing_or_changed')
            except Exception:
                errors.append('current_model_dependencies_unavailable')
        is_online=(schema in {"PTLIB-Phoenix-control-commission-v1","PTLIB-Phoenix-control-commission-v2"})
        is_offline=(schema in {"Layer5-Phoenix-offline-model-certificate-v1","Layer5-Phoenix-offline-model-certificate-v2","Layer5-Phoenix-offline-model-certificate-v3","Layer5-Phoenix-offline-model-certificate-v4"})
        if not (is_online or is_offline): errors.append("schema")
        if not bool(c.get("ok")): errors.append("certificate_not_ok")
        if is_online:
            if not bool(c.get("sitl_only")): errors.append("not_sitl_only")
            if not bool(c.get("physics_held_during_commissioning")): errors.append("physics_not_held")
        if is_offline:
            if str(c.get("learning_mode") or "").upper()!="OFFLINE_SE": errors.append("offline_learning_mode_required")
            if not bool(c.get("simulation_only")): errors.append("offline_simulation_only_required")
            if bool(c.get("online_ready")): errors.append("offline_certificate_cannot_claim_online_ready")
            if bool(c.get("direct_pwm")): errors.append("direct_pwm_forbidden")
        ph=str(c.get("phoenix_profile_sha256") or "")
        if callable(provider) and not ph:errors.append('profile_sha256_missing')
        if ph and ph!=self.hash: errors.append("profile_sha256_mismatch")
        if schema=="Layer5-Phoenix-offline-model-certificate-v4":
            ah=str(c.get("airframe_binding_sha256") or "")
            if callable(provider) and not ah:errors.append('airframe_binding_sha256_missing')
            if ah and ah!=self.airframe_binding_hash(): errors.append("airframe_binding_sha256_mismatch")
            hp=((c.get("phase_throttle_paths") or {}).get("hover") or {})
            if hp.get("certificate_schema")!="Layer5-Phoenix-hover-command-map-v2": errors.append("hover_certificate_v2_required")
            if hp.get("legacy_hover_operating_point_invalidated") is not True: errors.append("legacy_hover_operating_point_not_invalidated")
        mixer=c.get("mixer") or {}; trim=c.get("trim") or {}; throttle=c.get("throttle_path") or {}
        mat=mixer.get("elevon_matrix_rad_per_norm"); trim_rad=mixer.get("trim_rad")
        try:
            M=np.asarray(mat,dtype=float); BT=np.asarray(trim_rad,dtype=float)
            if M.shape!=(2,2) or not np.isfinite(M).all(): errors.append("mixer_matrix_invalid")
            if BT.shape!=(2,) or not np.isfinite(BT).all(): errors.append("mixer_trim_invalid")
            if BT is not None and np.isfinite(BT).all() and float(np.max(np.abs(BT)))>self.elevon_max:
                errors.append("mixer_trim_exceeds_elevon_limit")
            # A mixer coefficient is a gain, not a physical deflection. Validate the
            # actual physical elevon output over the bounded nominal-tracker command
            # envelope. This preserves the 0.53-rad hard limit without incorrectly
            # rejecting a locally measured ArduPilot mixer merely because one gain is
            # numerically greater than 0.53. Non-zero qLPV correction remains gated
            # independently by measured servo/ESC identification.
            if M is not None and BT is not None and np.isfinite(M).all() and np.isfinite(BT).all():
                tracker=self.config.get("phoenix.nominal_tracking",{}) or {}
                amax=max(0.0,min(1.0,float(tracker.get("max_aileron_norm",0.45))))
                emax=max(0.0,min(1.0,float(tracker.get("max_elevator_norm",0.65))))
                # PT_LIB commissioning returns trim_rad as the measured physical output
                # at the pilot-domain operating request [aileron=0, elevator=e0].
                # Therefore the local affine surface model is b + M(u-u0), not b+M*u.
                # OFFLINE model certificates use e0=0, so the same expression is exact.
                e_oper=float(trim.get("pitch_request_norm",0.0))
                u0=np.asarray([0.0,e_oper],dtype=float)
                corners=[]
                for aa in (-amax,amax):
                    for ee in (-emax,emax):
                        corners.append(M@(np.asarray([aa,ee],dtype=float)-u0)+BT)
                peak=float(max(np.max(np.abs(x)) for x in corners)) if corners else float(np.max(np.abs(BT)))
                if peak>self.elevon_max+1e-9:
                    errors.append("mixer_nominal_envelope_exceeds_elevon_limit")
        except Exception:
            M=None; BT=None; errors.append("mixer_matrix_invalid")
        mixer_valid=bool(mixer.get("validated")) if is_online else bool(mixer.get("model_qualified"))
        if not mixer_valid: errors.append("mixer_not_validated")
        if not bool(trim.get("ready")): errors.append("trim_not_ready")
        throttle_valid=bool(throttle.get("validated")) if is_online else bool(throttle.get("model_qualified"))
        if not throttle_valid: errors.append("throttle_path_not_validated")
        phase_surface_trims={}
        if is_offline:
            for phase_name,rec in dict(c.get("phase_throttle_paths") or {}).items():
                tr=(rec or {}).get("surface_trim_rad") if isinstance(rec,dict) else None
                try:
                    arr=np.asarray(tr,dtype=float)
                    if arr.shape==(2,) and np.isfinite(arr).all() and float(np.max(np.abs(arr)))<=self.elevon_max+1e-12:
                        phase_surface_trims[str(phase_name)]=arr
                except Exception:
                    pass
        if errors:
            self.runtime_certificate={"accepted":False,"errors":errors,"raw":c}
            self.nominal_tracker_authorized=False
            return {"ok":False,"accepted":False,"errors":errors}
        cert_kind="ONLINE_SITL" if is_online else "OFFLINE_MODEL"
        self.runtime_certificate={"accepted":True,"kind":cert_kind,"certificate":c}
        self.runtime_mixer_matrix_rad=M
        self.runtime_mixer_trim_rad=BT
        surface_contract=dict(c.get("plant_surface_contract") or {}) if is_online else {}
        surface_contract_ok=bool(
            surface_contract.get("schema")=="Phoenix-JSBSim-WingMPET-surface-contract-v1"
            and surface_contract.get("transform")=="wing_left=-raw_right;wing_right=-raw_left"
            and surface_contract.get("simulation_only") is True
            and surface_contract.get("direct_pwm") is False
            and surface_contract.get("ardupilot_mixer_authoritative") is True
        )
        self.runtime_surface_contract=surface_contract if surface_contract_ok else {}
        if is_online and surface_contract_ok:
            # Preserve the differential/aileron channel while reversing only the
            # symmetric/elevator convention of the coupled JSBSim plant.
            transform=np.asarray([[0.0,-1.0],[-1.0,0.0]],dtype=float)
            self.runtime_wingmpet_surface_matrix_rad=transform@M
            self.runtime_wingmpet_surface_trim_rad=transform@BT
        elif is_offline:
            # Offline certificates are already expressed in WingMPET coordinates.
            self.runtime_wingmpet_surface_matrix_rad=M
            self.runtime_wingmpet_surface_trim_rad=BT
        else:
            self.runtime_wingmpet_surface_matrix_rad=None
            self.runtime_wingmpet_surface_trim_rad=None
        self.runtime_phase_surface_trim_rad=phase_surface_trims if is_offline else {}
        self.runtime_mixer_operating_input_norm=np.asarray([0.0,float(trim.get("pitch_request_norm",0.0))],dtype=float)
        # Optional v2 commissioning fields preserve the measured local throttle
        # operating point. Older certificates remain accepted and fall back to the
        # historical identity throttle mapping, without promoting evidence.
        tp=dict(throttle or {})
        affine_keys=("operating_request_norm","operating_left_motor_norm","operating_right_motor_norm",
                     "left_gain_motor_norm_per_request_norm","right_gain_motor_norm_per_request_norm")
        self.runtime_throttle_affine={k:float(tp[k]) for k in affine_keys if tp.get(k) is not None}
        self.runtime_phase_throttle_affine={}
        if is_online:
            for phase_name,rec in dict(c.get("phase_throttle_paths") or {}).items():
                if not isinstance(rec,dict) or rec.get("validated") is not True:
                    continue
                bounds=rec.get("request_bounds_norm")
                target_bounds=rec.get("target_motor_bounds_norm")
                keys=("operating_request_norm","operating_left_motor_norm","operating_right_motor_norm",
                      "left_gain_motor_norm_per_request_norm","right_gain_motor_norm_per_request_norm")
                try:
                    vals={k:float(rec[k]) for k in keys}
                    lo,hi=float(bounds[0]),float(bounds[1])
                    target_lo,target_hi=float(target_bounds[0]),float(target_bounds[1])
                    target_op=float(rec["target_motor_operating_norm"])
                    quantization=max(0.0,float(rec.get("motor_feedback_quantization_norm",0.0) or 0.0))
                    target_error=max(0.0,float(rec.get("max_target_motor_error_norm",0.0) or 0.0))
                    affine_residual=max(0.0,float(rec.get("max_affine_residual_norm",0.0) or 0.0))
                    finite=all(math.isfinite(v) for v in (*vals.values(),lo,hi,target_lo,target_op,target_hi,
                                                           quantization,target_error,affine_residual))
                    if (finite and 0.0<=lo<=vals["operating_request_norm"]<=hi<=1.0
                            and 0.0<=target_lo<=target_op<=target_hi<=1.0
                            and rec.get("measurement_scope")=="held_sitl_pilot_domain_through_ArduPilot_mixer"):
                        self.runtime_phase_throttle_affine[str(phase_name)]={**vals,"request_bounds_norm":[lo,hi],
                            "target_motor_bounds_norm":[target_lo,target_hi],"target_motor_operating_norm":target_op,
                            "motor_feedback_quantization_norm":quantization,
                            "max_target_motor_error_norm":target_error,
                            "max_affine_residual_norm":affine_residual,
                            "measurement_scope":rec.get("measurement_scope"),"source":rec.get("source")}
                except (KeyError,TypeError,ValueError,IndexError):
                    continue
        self.runtime_trim={
            "elevator_norm":float(trim.get("pitch_request_norm",0.0)),
            "throttle_norm":float(trim.get("throttle_request_norm",0.0)),
        }
        # Keep the nominal reference tracker inside the same pilot-domain
        # neighborhood that the accepted runtime certificate actually qualified.
        # This is command saturation, not an enlargement of evidence: Layer4 full
        # fidelity, Layer5 FINAL and the downstream physical gate remain mandatory.
        self.runtime_throttle_request_bounds=None
        qualified_delta=tp.get("qualified_delta_from_operating_request_norm")
        try:
            if isinstance(qualified_delta,list) and len(qualified_delta)==2:
                operating_request=float(trim["throttle_request_norm"])
                lo=operating_request+float(qualified_delta[0])
                hi=operating_request+float(qualified_delta[1])
                if math.isfinite(lo) and math.isfinite(hi) and 0.0<=lo<=hi<=1.0:
                    self.runtime_throttle_request_bounds=(lo,hi)
        except Exception:
            self.runtime_throttle_request_bounds=None
        self.mixer_validated=True
        self.nominal_tracker_authorized=bool(c.get("nominal_tracker_authorized",True))
        # Measured actuator identification is ONLINE evidence only.  A model-derived
        # OFFLINE certificate can qualify the nominal zero-correction path but can
        # never unlock non-zero qLPV correction.
        if is_online:
            sd=c.get("servo_dynamics") or {}
            sd_scope=str(sd.get("scope") or "")
            if bool(sd.get("identified")) and bool(sd.get("measured")) and (schema.endswith("-v1") or sd_scope=="effective_sitl_command_path"):
                vals=[float(x) for x in sd.get("tau_s",[]) or []]; rates=[float(x) for x in sd.get("rate_limit_rad_s",[]) or []]
                if len(vals)>=2 and len(rates)>=2 and all(math.isfinite(x) and x>0 for x in vals[:2]) and all(math.isfinite(x) and x>0 for x in rates[:2]):
                    self.servo_identified=True; self.servo_tau=vals; self.servo_rate=rates
            esc=c.get("esc_mapping") or {}
            esc_scope=str(esc.get("scope") or "")
            if bool(esc.get("identified")) and bool(esc.get("measured")) and (schema.endswith("-v1") or esc_scope=="effective_sitl_command_path"):
                self.esc_mapping_identified=True
                # v2 carries the measured local throttle affine map; make it authoritative
                # for simulation correction without enabling direct PWM.
                keys=("operating_request_norm","operating_left_motor_norm","operating_right_motor_norm",
                      "left_gain_motor_norm_per_request_norm","right_gain_motor_norm_per_request_norm")
                if all(esc.get(k) is not None for k in keys):
                    self.runtime_throttle_affine={k:float(esc[k]) for k in keys}
        return {"ok":True,"accepted":True,"certificate_kind":cert_kind,
                "nominal_tracker_authorized":self.nominal_tracker_authorized,"mixer_validated":self.mixer_validated,
                "correction_path_ready":bool(self.servo_identified and self.esc_mapping_identified),
                "online_ready":bool(is_online and self.nominal_tracker_authorized and self.mixer_validated),
                "offline_model_ready":bool(is_offline and self.nominal_tracker_authorized and self.mixer_validated)}

    def claim_limits(self) -> dict[str,Any]:
        """Return source-bound validation claims that runtime commissioning must not upgrade.

        These flags describe the *scope of evidence*, not whether the current SITL
        command path is commissioned.  A successful held-SITL commissioning can make
        the external Layer5 -> PT_LIB -> ArduPilot mixer path usable in simulation, but
        it is not evidence of an autonomous ArduPilot VTOL controller, full-envelope
        aerodynamics, or real-aircraft flight validation.
        """
        return {
            "vtol_autopilot_controller_validated": bool(self.vtol_autopilot_controller_validated),
            "true_zero_speed_hover_dynamic_validated": bool(self.true_zero_speed_hover_validated),
            "aerodynamics_full_envelope_validated": bool(self.aero_full_validated),
            "qlpv_nonzero_correction_ready": bool(self.servo_identified and self.esc_mapping_identified),
            "real_aircraft_flight_validated": bool(self.real_aircraft_flight_validated),
        }

    def claim_scope(self) -> dict[str,Any]:
        limits=self.claim_limits()
        beyond_keys=(
            "vtol_autopilot_controller_validated",
            "aerodynamics_full_envelope_validated",
            "real_aircraft_flight_validated",
        )
        unmet=[k for k in beyond_keys if not bool(limits.get(k))]
        beyond_ok=not unmet
        return {
            "schema":"Layer5-Phoenix-claim-scope-v1",
            "status":"FULL_VALIDATION_SCOPE" if beyond_ok else "CLAIM_LIMITED_TO_SITL",
            "scope_ceiling":"REAL_AIRCRAFT_AND_FULL_ENVELOPE" if beyond_ok else "SITL_SIMULATION_ONLY",
            "current_sitl_category_permitted":True,
            "claim_limits_block_current_sitl_by_themselves":False,
            "beyond_sitl_claims_allowed":beyond_ok,
            "unmet_beyond_sitl_claims":unmet,
            "claim_limits":limits,
            "commissioning_may_upgrade_claim_limits":False,
            "runtime_certificate_scope":"SITL_CONTROL_PATH_COMMISSIONING_ONLY",
            "allowed_claims":[
                "SITL simulation execution through external Layer5 control and authoritative ArduPilot mixer",
                "simulation-level bounded VTOL plant/control-path evidence when the corresponding phase capability passes",
            ],
            "prohibited_claims_when_limited":[
                "validated autonomous ArduPilot tailsitter VTOL controller",
                "validated full-envelope Phoenix aerodynamics",
                "real-aircraft flight validation",
            ] if not beyond_ok else [],
            "authority":"source_bound_profile_plus_explicit_validation_evidence",
        }

    def phase_execution_capability(self) -> dict[str,Any]:
        """Return phase-scoped L6 execution-path evidence without upgrading claim boundaries.

        This is intentionally different from ``vtol_autopilot_controller_validated``.
        Layer 5 is the external controller in TDDT; ArduPilot remains the authoritative
        mixer/actuator gateway.  Therefore L6 execution support is compositional evidence
        that a commissioned pilot-domain command can traverse the ArduPilot mixer into a
        VTOL-capable simulation plant.  It does not claim autonomous ArduPilot tailsitter
        control, exact zero-speed dynamic hover validation, real-aircraft validation, or
        non-zero qLPV correction readiness.
        """
        cert_ok=bool((self.runtime_certificate or {}).get("accepted"))
        command_path=bool(cert_ok and self.nominal_tracker_authorized and self.mixer_validated and not self.direct_pwm_allowed)
        hover_static=str(self.hover_equilibrium_validation or "").lower() in {"analytic_force_balance_pass","pass","validated"}
        transition=bool(self.transition_validated)
        plant=bool(self.vtol_force_model_validated)
        common=bool(command_path and plant)
        phase_throttle=self.runtime_phase_throttle_affine if cert_ok else {}
        phases={
            "vertical_takeoff": bool(common and hover_static and "vertical_takeoff" in phase_throttle),
            "hover": bool(common and hover_static and "hover" in phase_throttle),
            "transition_forward": bool(common and transition),
            "transition_back": bool(common and transition),
            "vertical_landing": bool(common and hover_static),
        }
        blockers={}
        for phase,ok in phases.items():
            b=[]
            if not cert_ok: b.append("runtime_control_certificate_not_accepted")
            if not self.nominal_tracker_authorized: b.append("layer5_nominal_tracker_not_authorized")
            if not self.mixer_validated: b.append("ardupilot_mixer_not_commissioned")
            if self.direct_pwm_allowed: b.append("direct_pwm_must_remain_disabled")
            if not plant: b.append("vtol_force_model_not_validated")
            if phase in {"transition_forward","transition_back"} and not transition: b.append("bounded_transition_plant_not_validated")
            if phase in {"vertical_takeoff","hover","vertical_landing"} and not hover_static: b.append("hover_force_balance_not_validated")
            if phase in {"vertical_takeoff","hover"} and phase not in phase_throttle:
                b.append("phase_throttle_execution_range_not_commissioned")
            blockers[phase]=b
        return {
            "schema":"Layer5-Phoenix-phase-execution-capability-v1",
            "execution_authority":"Layer5_external_controller_via_PT_LIB_to_ArduPilot_mixer",
            "ardupilot_mixer_authoritative":True,
            "direct_pwm":False,
            "phase_supported":phases,
            "phase_blockers":blockers,
            "evidence":{
                "runtime_control_certificate_accepted":cert_ok,
                "nominal_tracker_authorized":bool(self.nominal_tracker_authorized),
                "mixer_validated":bool(self.mixer_validated),
                "vtol_force_model_validated":plant,
                "bounded_transition_plant_validated":transition,
                "hover_equilibrium_validation":self.hover_equilibrium_validation,
                "commissioned_phase_throttle_ranges":sorted(phase_throttle),
            },
            "claim_limits":self.claim_limits(),
            "claim_scope":self.claim_scope(),
        }

    def status(self) -> dict[str,Any]:
        return {"profile":str(self.path),"profile_sha256":self.hash,"forward_only":self.forward_only,"transition_validated":self.transition_validated,
          "vtol_autopilot_controller_validated":self.vtol_autopilot_controller_validated,"vtol_force_model_validated":self.vtol_force_model_validated,
          "hover_equilibrium_validation":self.hover_equilibrium_validation,"true_zero_speed_hover_validated":self.true_zero_speed_hover_validated,
          "environment_authorized":"FULL_ENVELOPE_CANDIDATE" if self.full_envelope else ("VTOL_PLANT_BOUNDED_CONTROLLER_FORWARD_ONLY" if self.vtol_force_model_validated else "FORWARD_SITL"),"mapping_validated":self.mapping_validated,"mixer_validated":self.mixer_validated,
          "servo_dynamics_identified":self.servo_identified,"aerodynamics_full_envelope_validated":self.aero_full_validated,"esc_mapping_identified":self.esc_mapping_identified,
          "phase_execution_capability":self.phase_execution_capability(),
          "claim_limits":self.claim_limits(),"claim_scope":self.claim_scope(),
          "real_aircraft_flight_validated":self.real_aircraft_flight_validated,
          "differential_thrust_gain":self.rudd_dt_gain,"differential_thrust_authorized":self.dt_validated,"direct_pwm_allowed":self.direct_pwm_allowed,"pwm_mapping_validated":self.pwm_validated,
          "physical_actuators":self.PHYSICAL_ACTUATORS,"logical_command_order":self.LOGICAL_ORDER,"state_contract":self.STATE_ORDER,
          "limits":{"elevon_rad":self.elevon_max,"motor_speed_rad_s":self.omega_max,"motor_thrust_N":self.thrust_max},
          "motor_model":{"tau_up_s":self.tau_up,"tau_down_s":self.tau_down,"k_T":self.k_t,"k_Q":self.k_q},
          "output_functions":self.output_functions,"pwm_anchors":self.pwm,"sensor_semantics":self.sensor_contract(),"airframe_binding":self.airframe_binding(),
          "runtime_validation_certificate":self.runtime_certificate,"nominal_tracker_authorized":self.nominal_tracker_authorized,
          "nominal_control_ready":bool(self.nominal_tracker_authorized and self.mixer_validated),
          "actuator_identification_evidence":{"offline_model_prior_available":True,"online_measured_servo_dynamics":bool(self.servo_identified),
              "online_measured_esc_mapping":bool(self.esc_mapping_identified),"identification_scope":"effective_sitl_command_path" if (self.servo_identified and self.esc_mapping_identified) else None,
              "simulation_only_identification":bool(self.servo_identified and self.esc_mapping_identified),"nonzero_qlpv_requires_measured_evidence":True,
              "zero_correction_nominal_path_allowed_without_measured_identification":True}}

    def sensor_contract(self)->dict[str,Any]:
        sensors=(self.sensor_layout.get("sensors") or {}) if isinstance(self.sensor_layout,dict) else {}
        return {"groups":sorted(sensors.keys()),"truth_observation_separated":True,
                "wind_semantics":{"sensor_observation":"ULTRASONIC_WIND","autopilot_estimate":"WIND","environment_truth":"JSBSim current_state.wind"},
                "twin_contract":["MESA_NAV:state+uncertainty","WingMPET:trajectory+feasibility+energy","Climate:rho+wind+disturbance"],
                "raw_sensor_to_pwm":False}

    def airframe_binding(self)->dict[str,Any]:
        return {"inertia":self.inertia,"geometry":self.geometry,"propwash":self.propwash,"uncertainty_ranges":self.uncertainty,
                "forward_aero":{"confidence":self.forward_aero.get("confidence"),"provenance":self.forward_aero.get("provenance"),"source_validated":False},
                "source_validated_components":["inertia","motor_law","elevon_joint_limit","propwash_coefficients"],"engineering_estimate_not_source_validated":True}

    def airframe_binding_hash(self)->str:
        return sha256_obj(self.airframe_binding())

    def correction_validation_gates(self)->dict[str,Any]:
        mixer_shape=False
        try:
            M=np.asarray(self.config.get("phoenix.mixer.elevon_matrix"),float); b=np.asarray(self.config.get("phoenix.mixer.trim_rad",[0.0,0.0]),float); mixer_shape=(M.shape==(2,2) and b.shape==(2,))
        except Exception: pass
        servo_ready=bool(self.servo_identified and len(self.servo_tau)>=2 and len(self.servo_rate)>=2 and all(x>0 for x in self.servo_tau[:2]) and all(x>0 for x in self.servo_rate[:2]))
        runtime_mixer=bool(self.runtime_mixer_matrix_rad is not None and np.asarray(self.runtime_mixer_matrix_rad).shape==(2,2))
        return {"mixer":bool(self.mixer_validated and (mixer_shape or runtime_mixer)),"servo_dynamics":servo_ready,"esc_mapping":bool(self.esc_mapping_identified),
                "transition":bool(self.transition_validated),"full_envelope":bool(self.full_envelope),"direct_pwm":bool(self.direct_pwm_allowed),
                "nominal_tracker":bool(self.nominal_tracker_authorized),
                "nominal_control":bool(self.nominal_tracker_authorized and self.mixer_validated and (mixer_shape or runtime_mixer)),
                "qlpv_nonzero_correction":bool(self.mixer_validated and (mixer_shape or runtime_mixer) and servo_ready and self.esc_mapping_identified),
                "airframe_binding_sha256":self.airframe_binding_hash(),"profile_sha256":self.hash}

    def bind_candidate(self, candidate: dict[str,Any]) -> tuple[list[list[float]], dict[str,Any]]:
        for loc in (candidate.get("logical_actions"), (candidate.get("provenance") or {}).get("phoenix_logical_actions")):
            if isinstance(loc,list) and loc and all(isinstance(x,(list,tuple)) and len(x)==4 for x in loc):
                seq=[[float(v) for v in x] for x in loc]
                return seq,{"ok":self._logical_bounds(seq),"source":"candidate_explicit_phoenix_logical_actions","validated":True,"order":self.LOGICAL_ORDER}
        if self.mapping_validated:
            M=np.asarray(self.config.get("phoenix.mapping.matrix"),dtype=float); b=np.asarray(self.config.get("phoenix.mapping.bias"),dtype=float); A=np.asarray(candidate.get("actions") or [],dtype=float)
            if M.shape!=(4,5) or b.shape!=(4,) or A.ndim!=2 or A.shape[1]!=5: return [],{"ok":False,"source":"configured_mapping","reason":"validated_mapping_shape_invalid"}
            seq=[list(map(float,(M@row+b))) for row in A]
            return seq,{"ok":self._logical_bounds(seq),"source":"configured_validated_mapping","version":self.config.get("phoenix.mapping.version"),"order":self.LOGICAL_ORDER}
        if self.nominal_tracker_authorized:
            seq,rec=self._trajectory_nominal_controls(candidate)
            if seq:
                return seq,{"ok":self._logical_bounds(seq),"source":"commissioned_layer4_trajectory_reference_tracker","validated":True,
                           "order":self.LOGICAL_ORDER,"tracker":rec,"mapping_semantics":"Layer4 trajectory -> Layer5 nominal pilot-domain control; no 5D abstract-action mapping"}
            return [],{"ok":False,"source":"commissioned_layer4_trajectory_reference_tracker",
                       "reason":str(rec.get("reason") or "layer4_to_phoenix_nominal_control_binding_not_validated"),
                       "tracker":rec,"layer4_action_order":self.L4_ACTION_ORDER,
                       "required_logical_order":self.LOGICAL_ORDER}
        return [],{"ok":False,"source":"none","reason":"layer4_to_phoenix_nominal_control_binding_not_validated","layer4_action_order":self.L4_ACTION_ORDER,
                   "required_logical_order":self.LOGICAL_ORDER,"accepted_paths":["explicit logical_actions","validated 5D mapping","commissioned trajectory reference tracker"]}

    def layer4_vertical_action_envelope(self, phase:str, operating_context:dict[str,Any]|None=None,
                                        horizon_steps:int=1)->dict[str,Any]:
        """Publish the commissioned vertical envelope as a Layer-4 planning contract.

        The contract narrows Layer-4's hypothetical ``delta_thrust_norm`` domain;
        it is not an actuator command and cannot authorize execution.  All values
        are derived from the accepted held-SITL certificate, Phoenix source motor
        equilibrium and (for the first commit) the same-cycle motor state.
        """
        phase_name=str(phase or "").strip().lower()
        horizon=max(1,int(horizon_steps))
        if phase_name not in {"vertical_takeoff","hover"}:
            return {"ok":True,"applied":False,"phase":phase_name,
                    "reason":"phase_has_no_commissioned_vertical_delta_contract"}
        runtime=dict(self.runtime_certificate or {})
        if not (runtime.get("accepted") is True and runtime.get("kind")=="ONLINE_SITL"
                and self.nominal_tracker_authorized):
            return {"ok":False,"applied":False,"phase":phase_name,
                    "reason":"online_nominal_tracker_certificate_unavailable"}
        rec=(self.runtime_phase_throttle_affine or {}).get(phase_name)
        if not isinstance(rec,dict):
            return {"ok":False,"applied":False,"phase":phase_name,
                    "reason":"commissioned_phase_throttle_envelope_unavailable"}
        seed=self.offline_vtol_seed_controls(operating_context)
        if not isinstance(seed,dict) or seed.get("ok") is not True:
            return {"ok":False,"applied":False,"phase":phase_name,
                    "reason":"source_vertical_equilibrium_unavailable","seed":seed}
        try:
            equilibrium=float(seed["equilibrium_collective_norm"])
            target_lo,target_hi=[float(v) for v in rec["target_motor_bounds_norm"]]
            cfg=self.config.get("phoenix.nominal_tracking",{}) or {}
            phase_seed=float((seed.get("phases") or {})[phase_name]["throttle_request_norm"])
            correction=(float(cfg.get("vertical_takeoff_max_feedback_correction_norm",0.04))
                        if phase_name=="vertical_takeoff"
                        else float(cfg.get("hover_max_feedback_correction_norm",0.04)))
            # The commissioning request deliberately includes a bounded
            # sub-equilibrium segment for takeoff-to-hover braking as well as
            # hover capture.  Re-clamping that measured lower endpoint to static
            # equilibrium makes upward momentum uncontrollable over the L4
            # horizon and creates a circular phase gate.  Keep exactly the
            # measured endpoint; it is still narrowed by the source tracker,
            # robust optimization, WingMPET FF, L5 FINAL and L6 gates.
            motor_lo=target_lo
            motor_hi=min(target_hi,phase_seed+max(0.0,correction))
            if not all(math.isfinite(v) for v in (equilibrium,target_lo,target_hi,phase_seed,correction,motor_lo,motor_hi)):
                raise ValueError("non_finite_vertical_envelope")
            if motor_lo>motor_hi:
                raise ValueError("commissioned_source_vertical_envelope_empty")
            per_step=[{"lower":motor_lo-equilibrium,"upper":motor_hi-equilibrium}
                      for _ in range(horizon)]
            transient={"applied":False,"reason":"same_cycle_motor_state_unavailable"}
            motor_state=(operating_context or {}).get("motor_speed_state") if isinstance(operating_context,dict) else None
            if phase_name=="vertical_takeoff" and isinstance(motor_state,dict):
                execution_segment=float((operating_context or {})["execution_segment_s"])
                current_motor=min(float(motor_state["omega_left_rad_s"]),
                                  float(motor_state["omega_right_rad_s"]))/float(self.omega_max)
                decay=math.exp(-execution_segment/float(self.tau_up))
                # Layer5's first-move inverse must command no more than motor_hi.
                # Therefore the first exit target (and the look-ahead target used
                # by that inverse) cannot exceed this source-dynamics value.
                exit_hi=motor_hi*(1.0-decay)+current_motor*decay
                if not all(math.isfinite(v) for v in (execution_segment,current_motor,decay,exit_hi)):
                    raise ValueError("takeoff_transient_envelope_non_finite")
                if execution_segment<=0.0 or self.tau_up<=0.0 or current_motor<0.0:
                    raise ValueError("takeoff_transient_envelope_invalid")
                transient_upper=exit_hi-equilibrium
                for index in range(min(2,horizon)):
                    per_step[index]["upper"]=min(per_step[index]["upper"],transient_upper)
                transient={"applied":True,"method":"source_motor_first_order_commit_inverse",
                           "current_min_motor_norm":current_motor,"motor_tau_up_s":float(self.tau_up),
                           "execution_segment_s":execution_segment,"decay":decay,
                           "maximum_exit_motor_norm":exit_hi,
                           "lookahead_steps_constrained":min(2,horizon)}
            if any(float(x["lower"])>float(x["upper"]) for x in per_step):
                raise ValueError("commissioned_transient_vertical_envelope_empty")
            contract={"schema":"Layer5-to-Layer4-commissioned-action-envelope-v1",
                      "ok":True,"applied":True,"phase":phase_name,
                      "action":"delta_thrust_norm","action_semantics":"motor collective delta about Phoenix source hover equilibrium",
                      "source_equilibrium_motor_norm":equilibrium,
                      "commissioned_target_motor_bounds_norm":[target_lo,target_hi],
                      "source_tracker_motor_bounds_norm":[motor_lo,motor_hi],
                      "per_step_delta_bounds":per_step,"horizon_steps":horizon,
                      "commit_transient":transient,"certificate_schema":str((runtime.get("certificate") or {}).get("schema") or ""),
                      "not_actuator_command":True,"actuation_authority":False,
                      "layer4_full_fidelity_required":True,"layer5_final_required":True,
                      "layer6_executor_gate_required":True,"ardupilot_mixer_authoritative":True,
                      "direct_jsbsim_actuation":False}
            contract["contract_sha256"]=sha256_obj(contract)
            return contract
        except (KeyError,TypeError,ValueError,IndexError,ZeroDivisionError) as exc:
            return {"ok":False,"applied":False,"phase":phase_name,
                    "reason":"commissioned_vertical_action_envelope_invalid","error":repr(exc)}

    @staticmethod
    def _wrap_pi(x:float)->float:
        return (float(x)+math.pi)%(2.0*math.pi)-math.pi

    @staticmethod
    def _shortest_roll_pitch_axis_allocation(axis_y:float,axis_z:float,
                                             separation_rad:float)->tuple[float,float,dict[str,Any]]:
        """Choose the shortest equivalent roll/pitch decomposition.

        A rotation about ``-body-Y`` by ``theta`` is identical, for thrust-axis
        alignment, to a rotation about ``body-Y`` by ``-theta``.  Canonicalizing
        this axis symmetry prevents a numerically tiny thrust-axis correction near
        ``-body-Y`` from requesting an unnecessary 180-degree body-X roll.  This is
        purely kinematic allocation; it neither changes a gain nor relaxes a gate.
        """
        y=float(axis_y); z=float(axis_z); separation=float(separation_rad)
        if not all(math.isfinite(v) for v in (y,z,separation)) or separation<0.0:
            raise ValueError("invalid_roll_pitch_axis_allocation")
        raw_roll=math.atan2(z,y)
        roll=raw_roll; pitch_sign=1.0
        if roll>math.pi/2.0:
            roll-=math.pi; pitch_sign=-1.0
        elif roll<-math.pi/2.0:
            roll+=math.pi; pitch_sign=-1.0
        alignment=max(0.0,math.cos(roll))
        pitch=pitch_sign*separation*alignment
        return roll,pitch,{
            "raw_roll_alignment_increment_rad":raw_roll,
            "canonical_axis_symmetry_applied":pitch_sign<0.0,
            "pitch_axis_sign":pitch_sign,
            "pitch_alignment_factor":alignment,
            "method":"shortest_equivalent_body_y_axis_v1",
        }

    def offline_vtol_seed_controls(self, operating_context:dict[str,Any]|None=None)->dict[str,Any]:
        """Return bounded OFFLINE/SITL seed controls for vertical regimes.

        These values are *not* control evidence and never authorize Online execution.
        They provide a physically meaningful nominal candidate around the Phoenix
        analytic hover equilibrium so Layer4 Full-Fidelity can test the candidate.
        Verification/FINAL/control-effect remain mandatory downstream.
        """
        operating_context=operating_context or {}
        sim=self.data.get("simulation_assumptions") or {}
        ana=sim.get("analytic_hover_validation") or {}
        mass_rec=self.data.get("mass") or {}; prop=self.data.get("propulsion") or {}; motor=prop.get("source_motor_model") or {}
        mass=float(mass_rec.get("empty_or_flight_mass_kg",ana.get("mass_kg",0.65)))
        motor_count=max(1,int(prop.get("motor_count",2)))
        k_t=float(motor.get("motor_constant_N_per_rad_s_squared",self.k_t))
        omega_max=float(motor.get("max_rot_velocity_rad_s",self.omega_max))
        thrust_law_total=float(motor_count*k_t*omega_max*omega_max)
        configured_total=float(prop.get("maximum_total_thrust_N",thrust_law_total))
        available_total=max(1e-12,min(thrust_law_total,configured_total))
        g=9.80665
        eq=math.sqrt(max(0.0,mass*g)/available_total)
        cfg=self.config.get("phoenix.nominal_tracking",{}) or {}
        # Solution 4: hover base collective is also derived in thrust space.
        # A small configurable thrust margin preserves force support without an
        # arbitrary throttle offset; altitude/down-velocity correction is applied
        # later by the phase-aware nominal tracker and remains bounded.
        hover_thrust_margin=max(0.0,float(cfg.get("hover_thrust_margin_fraction",0.064)))
        required_hover_thrust=mass*g*(1.0+hover_thrust_margin)
        hover_force_balance=math.sqrt(max(0.0,required_hover_thrust)/available_total)
        # Solution 5: bind the hover operating point to the same-cycle environment
        # without inventing unsupported thrust derating. WingMPET op27 uses the
        # source-identified T=k_T*omega^2 motor law (k_T is not rho/voltage-scaled),
        # so rho and SOC are validity gates/provenance, not hidden gain factors.
        rho=float(operating_context.get("air_density_kg_m3", operating_context.get("rho",1.225)))
        soc=float(operating_context.get("battery_soc", operating_context.get("soc",1.0)))
        rho_min=float(cfg.get("hover_air_density_min_kg_m3",0.80)); rho_max=float(cfg.get("hover_air_density_max_kg_m3",1.35))
        min_soc=float(cfg.get("hover_min_battery_soc",0.20))
        environment_ok=bool(math.isfinite(rho) and rho_min<=rho<=rho_max)
        battery_ok=bool(math.isfinite(soc) and min_soc<=soc<=1.0)
        # Solution 3: takeoff margin is expressed in thrust space, not as an
        # arbitrary throttle offset.  This keeps the operating point tied to the
        # source Phoenix motor law T=k_T*omega^2 and the mission mass.
        takeoff_thrust_margin=max(0.0,float(cfg.get("vertical_takeoff_thrust_margin_fraction",0.064)))
        climb_rate_ref=float(operating_context.get("vertical_takeoff_climb_rate_ref_m_s",cfg.get("vertical_takeoff_reference_climb_rate_m_s",2.0)) or 0.0)
        if operating_context.get("vertical_takeoff_accel_request_m_s2") is not None:
            vertical_accel_ref=max(0.0,float(operating_context.get("vertical_takeoff_accel_request_m_s2") or 0.0))
            takeoff_thrust_basis="same_cycle_vertical_acceleration_request"
        else:
            # Backward-compatible default: the existing 6.4% thrust margin is exactly
            # the acceleration reserve a_z=g*margin. The climb-rate request is tracked
            # by the bounded vertical-rate controller around this operating point.
            vertical_accel_ref=float(g*takeoff_thrust_margin)
            takeoff_thrust_basis="configured_thrust_margin_as_vertical_acceleration_reserve"
        required_takeoff_thrust=mass*(g+vertical_accel_ref)
        takeoff_force_balance=math.sqrt(max(0.0,required_takeoff_thrust)/available_total)
        lo=max(0.0,float(cfg.get("vertical_collective_min_norm",0.0)))
        hi=min(1.0,float(cfg.get("vertical_collective_max_norm",0.95)))
        hover=clip(hover_force_balance,lo,hi)
        takeoff=clip(takeoff_force_balance,lo,hi)
        hover_force_balance_valid=bool(math.isfinite(hover_force_balance) and hover_force_balance<=hi and required_hover_thrust<=available_total and environment_ok and battery_ok)
        force_balance_valid=bool(math.isfinite(takeoff_force_balance) and takeoff_force_balance<=hi and required_takeoff_thrust<=available_total)
        hover_omega_cmd=float(omega_max*hover)
        hover_predicted_total_thrust=float(min(available_total,motor_count*k_t*hover_omega_cmd*hover_omega_cmd))

        # Solution2-v2: derive the vertical-takeoff symmetric elevon trim from the
        # source Phoenix propwash pitch model. The default pitch-acceleration request
        # is zero, so a near-neutral trim emerges from moment balance instead of being
        # inherited from the forward-flight trim. Full-Fidelity remains authoritative.
        propwash=dict(self.propwash or {})
        k_lift=float(propwash.get("k_lift",0.0) or 0.0)
        k_pitch=float(propwash.get("k_pitch",0.0) or 0.0)
        cp_l=propwash.get("left_cp_m") or [0.0,0.0,0.0]
        cp_r=propwash.get("right_cp_m") or [0.0,0.0,0.0]
        try: cp_z=0.5*(float(cp_l[2])+float(cp_r[2]))
        except Exception: cp_z=0.0
        try:
            # In the Phoenix source frame y is also canonical FRD y.  Opposite
            # elevon deflections therefore create the roll moment
            # Mx = (|y_L|+|y_R|) k_lift omega^2 delta_differential.
            cp_roll_arm=0.5*(abs(float(cp_l[1]))+abs(float(cp_r[1])))
        except Exception: cp_roll_arm=0.0
        takeoff_omega_cmd=float(omega_max*takeoff)
        pitch_slope_per_rad=float(motor_count*(takeoff_omega_cmd**2)*(k_pitch+cp_z*k_lift))
        roll_slope_per_rad=float(motor_count*(takeoff_omega_cmd**2)*cp_roll_arm*k_lift)
        iyy=float(self.inertia.get("Iyy_kg_m2",0.0) or 0.0)
        ixx=float(self.inertia.get("Ixx_kg_m2",0.0) or 0.0)
        requested_pitch_accel=float(operating_context.get("vertical_takeoff_pitch_accel_request_rad_s2",0.0) or 0.0)
        target_pitch_moment=float(iyy*requested_pitch_accel)
        trim_limit=min(self.elevon_max,float(cfg.get("vertical_takeoff_trim_limit_rad",0.12)))
        if math.isfinite(pitch_slope_per_rad) and abs(pitch_slope_per_rad)>1e-12 and math.isfinite(target_pitch_moment):
            takeoff_elevon_trim=clip(target_pitch_moment/pitch_slope_per_rad,-trim_limit,trim_limit)
            takeoff_pitch_trim_valid=True
        elif abs(target_pitch_moment)<=1e-12:
            takeoff_elevon_trim=0.0; takeoff_pitch_trim_valid=True
        else:
            takeoff_elevon_trim=0.0; takeoff_pitch_trim_valid=False
        takeoff_pitch_residual=float(pitch_slope_per_rad*takeoff_elevon_trim-target_pitch_moment)

        # Solution3-v2: landing uses the same source-identified motor law and the
        # hover force-balance operating point as a braking/descent reference, but
        # it has its own phase-specific physics contract.  The contract describes
        # bounded descent-rate modulation and the Layer4 terminal AGL corridor; it
        # does not copy hover semantics and never bypasses WingMPET FF.
        landing_descent_ref=max(0.0,float(operating_context.get(
            "vertical_landing_descent_rate_ref_m_s",
            cfg.get("vertical_landing_reference_descent_rate_m_s",1.0)) or 0.0))
        landing_ff_gain=max(0.0,float(cfg.get("vertical_landing_descent_feedforward_gain_per_m_s",0.012)))
        landing_fb_gain=max(0.0,float(cfg.get("vertical_landing_descent_error_gain_per_m_s",0.020)))
        landing_reduction_max=max(0.0,float(cfg.get("vertical_landing_max_collective_reduction_norm",0.08)))
        landing_braking_boost=max(0.0,float(cfg.get("vertical_landing_max_braking_boost_norm",0.06)))
        landing_nominal_reduction=min(landing_reduction_max,landing_ff_gain*landing_descent_ref)
        landing_min_collective=max(lo,hover-landing_reduction_max)
        landing_max_collective=min(hi,hover+landing_braking_boost)
        landing_thrust_to_weight=float(hover_predicted_total_thrust/max(mass*g,1e-12))
        landing_physics_basis={
            "schema":"Phoenix-vertical-landing-physics-basis-v1",
            "phase":"vertical_landing",
            "physics_derived":True,
            "source_motor_law":True,
            "hover_force_balance_valid":bool(hover_force_balance_valid),
            "landing_force_reference_valid":bool(hover_force_balance_valid and landing_min_collective<=hover<=landing_max_collective),
            "mass_kg":mass,"gravity_m_s2":g,
            "required_weight_support_N":float(mass*g),
            "reference_collective_norm":float(hover),
            "reference_omega_cmd_rad_s_per_motor":float(hover_omega_cmd),
            "reference_total_thrust_N":float(hover_predicted_total_thrust),
            "reference_thrust_to_weight_ratio":landing_thrust_to_weight,
            "descent_rate_target_m_s":landing_descent_ref,
            "descent_feedforward_gain_per_m_s":landing_ff_gain,
            "descent_error_gain_per_m_s":landing_fb_gain,
            "nominal_descent_collective_reduction_norm":float(landing_nominal_reduction),
            "max_collective_reduction_norm":landing_reduction_max,
            "max_braking_boost_norm":landing_braking_boost,
            "collective_modulation_envelope_norm":[float(landing_min_collective),float(landing_max_collective)],
            "terminal_agl_corridor_authority":"Layer4 verified landing corridor",
            "terminal_agl_target_m":0.0,
            "terminal_agl_corridor_valid":True,
            "air_density_kg_m3":rho,"air_density_context_valid":environment_ok,
            "battery_soc":soc,"battery_context_valid":battery_ok,
            "environment_fingerprint":operating_context.get("environment_fingerprint"),
            "pilot_to_motor_mapping":{"schema":"Phoenix-pilot-to-motor-normalization-v1",
                "pilot_throttle_norm_range":[0.0,1.0],"motor_norm_gain":1.0,"motor_norm_offset":0.0,
                "omega_cmd_rad_s_per_motor":"omega_max * motor_norm",
                "thrust_per_motor_N":"min(k_T * omega_cmd^2, T_max_each)",
                "mixer_authority":"ArduPilot for Online; identity effective-action mapping only for WingMPET FF"},
            "semantics":"landing reference is source-motor-law hover force balance plus bounded descent/braking modulation; physical feasibility remains WingMPET FF authority",
        }
        return {
            "ok":bool(math.isfinite(eq) and 0.0<=eq<=1.0 and hover_force_balance_valid and force_balance_valid and takeoff_pitch_trim_valid),
            "source":"Phoenix source motor thrust law + static vertical-regime force balance; bounded phase margins",
            "simulation_only":True,"not_field_evidence":True,"direct_pwm":False,
            "equilibrium_collective_norm":float(eq),
            "physics_basis":{
                "physics_derived":True,"source_motor_law":True,"force_balance_valid":force_balance_valid,
                "mass_kg":mass,"gravity_m_s2":g,"motor_count":motor_count,
                "motor_k_t_N_per_rad_s2":k_t,"motor_omega_max_rad_s":omega_max,
                "available_total_thrust_N":available_total,"required_hover_thrust_N":required_hover_thrust,
                "hover_thrust_margin_fraction":hover_thrust_margin,
                "hover_force_balance_valid":hover_force_balance_valid,
                "vertical_takeoff_thrust_margin_fraction":takeoff_thrust_margin,
                "vertical_takeoff_required_thrust_N":required_takeoff_thrust,
                "vertical_takeoff_climb_rate_ref_m_s":climb_rate_ref,
                "vertical_takeoff_accel_request_m_s2":vertical_accel_ref,
                "vertical_takeoff_thrust_basis":takeoff_thrust_basis,
                "takeoff_pitch_trim_derived":True,
                "takeoff_pitch_trim_valid":bool(takeoff_pitch_trim_valid),
                "takeoff_symmetric_elevon_trim_rad":float(takeoff_elevon_trim),
                "takeoff_pitch_accel_request_rad_s2":requested_pitch_accel,
                "takeoff_target_pitch_moment_Nm":target_pitch_moment,
                "takeoff_propwash_pitch_slope_Nm_per_rad":pitch_slope_per_rad,
                "takeoff_pitch_moment_residual_Nm":takeoff_pitch_residual,
                "takeoff_propwash_terms":{"k_lift":k_lift,"k_pitch":k_pitch,"cp_z_m":cp_z,
                    "cp_roll_arm_m":cp_roll_arm,"omega_cmd_rad_s":takeoff_omega_cmd,
                    "Ixx_kg_m2":ixx,"Iyy_kg_m2":iyy},
                "takeoff_propwash_roll_slope_Nm_per_rad":roll_slope_per_rad,
                "takeoff_pitch_trim_semantics":"source-propwash moment equilibrium; no forward-flight trim inheritance; FF authority retained",
                "air_density_kg_m3":rho,"air_density_valid_range_kg_m3":[rho_min,rho_max],"air_density_context_valid":environment_ok,
                "battery_soc":soc,"minimum_battery_soc":min_soc,"battery_context_valid":battery_ok,
                "environment_fingerprint":operating_context.get("environment_fingerprint"),
                "hover_state_envelope":{"airspeed_m_s":[0.0,float(self.config.get("phoenix.transition.v_forward_min",9.0))],
                    "air_density_kg_m3":[rho_min,rho_max],"battery_soc":[min_soc,1.0]},
                "hover_action_envelope":{"throttle_delta_norm":float(self.config.get("phoenix.nominal_tracking.hover_max_feedback_correction_norm",0.04)),
                    "surface_trim_rad":[0.0,0.0]},
                "air_density_semantics":"same-cycle validity/provenance only; source motor k_T is not rho-scaled in WingMPET op27",
                "battery_semantics":"SOC is a propulsion-availability gate; no unsupported voltage/SOC thrust derating is inferred",
                "pilot_to_motor_mapping":{"schema":"Phoenix-pilot-to-motor-normalization-v1","pilot_throttle_norm_range":[0.0,1.0],
                    "motor_norm_gain":1.0,"motor_norm_offset":0.0,"omega_cmd_rad_s_per_motor":"omega_max * motor_norm",
                    "thrust_per_motor_N":"min(k_T * omega_cmd^2, T_max_each)","mixer_authority":"ArduPilot for Online; identity effective-action mapping only for WingMPET FF"},
                "vertical_landing_physics_basis":landing_physics_basis},
            "phases":{
                "vertical_takeoff":{"throttle_request_norm":float(takeoff),"aileron_norm":0.0,"elevator_norm":0.0,"rudder_norm":0.0,
                    "surface_trim_rad":[float(takeoff_elevon_trim),float(takeoff_elevon_trim)],
                    "symmetric_elevon_trim_rad":float(takeoff_elevon_trim),
                    "operating_point_basis":"phoenix_source_motor_law_force_balance",
                    "surface_trim_basis":"phoenix_source_propwash_pitch_equilibrium"},
                "hover":{"throttle_request_norm":float(hover),"aileron_norm":0.0,"elevator_norm":0.0,"rudder_norm":0.0,
                    "operating_point_basis":"phoenix_source_motor_law_hover_force_balance",
                    "pilot_throttle_norm":float(hover),"motor_norm":float(hover),"omega_cmd_rad_s_per_motor":hover_omega_cmd,
                    "predicted_total_thrust_N":hover_predicted_total_thrust,
                    "operating_context":{"air_density_kg_m3":rho,"battery_soc":soc,
                        "environment_fingerprint":operating_context.get("environment_fingerprint"),
                        "air_density_context_valid":environment_ok,"battery_context_valid":battery_ok}},
                "vertical_landing":{"throttle_request_norm":float(hover),"aileron_norm":0.0,"elevator_norm":0.0,"rudder_norm":0.0,
                    "operating_point_basis":"phoenix_source_motor_law_landing_force_balance_reference",
                    "landing_physics_basis_schema":"Phoenix-vertical-landing-physics-basis-v1",
                    "descent_rate_target_m_s":landing_descent_ref,
                    "terminal_agl_corridor_authority":"Layer4 verified landing corridor",
                    "pilot_throttle_norm":float(hover),"motor_norm":float(hover),
                    "operating_context":{"air_density_kg_m3":rho,"battery_soc":soc,
                        "environment_fingerprint":operating_context.get("environment_fingerprint"),
                        "air_density_context_valid":environment_ok,"battery_context_valid":battery_ok}},
            },
        }

    def online_phase_commissioning_requests(self, operating_context:dict[str,Any]|None=None)->dict[str,Any]:
        """Declare source-derived pilot-throttle ranges that PT_LIB must measure.

        This method produces no execution evidence.  It only binds held-SITL
        commissioning probes to the same vertical-regime request envelope that the
        Layer5 tracker can issue.  PT_LIB/ArduPilot feedback remains the authority for
        the request-to-motor map and WingMPET Full-Fidelity remains mandatory.
        """
        seed=self.offline_vtol_seed_controls(operating_context)
        if not seed.get("ok"):
            return {"ok":False,"reason":"vertical_seed_not_available","seed":seed}
        phases=seed.get("phases") or {}
        cfg=self.config.get("phoenix.nominal_tracking",{}) or {}
        vertical_min=max(0.0,float(cfg.get("vertical_collective_min_norm",0.0)))
        vertical_max=min(1.0,float(cfg.get("vertical_collective_max_norm",0.95)))
        equilibrium=float(seed.get("equilibrium_collective_norm",vertical_min))
        takeoff=float((phases.get("vertical_takeoff") or {}).get("throttle_request_norm"))
        hover=float((phases.get("hover") or {}).get("throttle_request_norm"))
        takeoff_delta=max(0.0,float(cfg.get("vertical_takeoff_max_feedback_correction_norm",0.04)))
        hover_delta=max(0.0,float(cfg.get("hover_max_feedback_correction_norm",0.04)))
        # A takeoff trajectory can legitimately request a short braking segment
        # before the hover boundary.  The old lower probe stopped at static hover
        # equilibrium, so Layer5 could not consume a verified Layer4 first move
        # that reduced an already-positive climb rate.  Commission the union of
        # the existing source-derived takeoff and hover feedback envelopes.  This
        # introduces no new throttle constant: both endpoints come from the
        # Phoenix motor law and configured bounded feedback trust regions, and the
        # measured PT_LIB/ArduPilot inverse remains authoritative Online.
        takeoff_braking_min=max(vertical_min,min(takeoff-takeoff_delta,hover-hover_delta))
        requests={
            "vertical_takeoff":{
                "target_motor_operating_norm":takeoff,
                "target_motor_bounds_norm":[takeoff_braking_min,min(vertical_max,takeoff+takeoff_delta)],
                "basis":"source_force_balance_takeoff_plus_source_bound_braking_feedback"},
            "hover":{
                "target_motor_operating_norm":hover,
                "target_motor_bounds_norm":[max(vertical_min,hover-hover_delta),min(vertical_max,hover+hover_delta)],
                "basis":"source_force_balance_hover_plus_bounded_feedback"},
        }
        valid=all(0.0<=float(v["target_motor_bounds_norm"][0])<=float(v["target_motor_operating_norm"])<=float(v["target_motor_bounds_norm"][1])<=1.0
                  for v in requests.values())
        return {"ok":bool(valid),"schema":"Layer5-Phoenix-online-phase-commissioning-request-v1",
                "requests":requests,"direct_pwm":False,"mixer_owner":"ArduPilot",
                "evidence_semantics":"source-motor target envelope only; PT_LIB must invert and measure the pilot-domain range through ArduPilot"}

    def _forward_nominal_control(self, cur:list[float], nxt:list[float], *, cfg:dict[str,Any], trim:tuple[float,float]) -> tuple[list[float],dict[str,Any]]:
        """Legacy wing-borne reference tracker, isolated so VTOL regimes cannot inherit it accidentally."""
        max_a=max(0.01,float(cfg.get("max_aileron_norm",0.45))); max_e=max(0.01,float(cfg.get("max_elevator_norm",0.65)))
        k_h=float(cfg.get("heading_gain_per_rad",1.2)); k_g=float(cfg.get("flightpath_gain_per_rad",1.0)); k_v=float(cfg.get("airspeed_gain_per_m_s",0.035))
        cruise=float(cfg.get("reference_airspeed_m_s",13.0)); tmin=max(0.0,float(cfg.get("throttle_min_norm",0.05))); tmax=min(1.0,float(cfg.get("throttle_max_norm",0.75)))
        certificate_bounds=getattr(self,"runtime_throttle_request_bounds",None)
        if isinstance(certificate_bounds,(list,tuple)) and len(certificate_bounds)==2:
            tmin=max(tmin,float(certificate_bounds[0])); tmax=min(tmax,float(certificate_bounds[1]))
        if tmin>tmax:
            # An accepted certificate must never silently create an empty tracker
            # domain.  Returning a non-finite command makes the existing logical
            # bounds gate fail closed instead of selecting an unqualified request.
            return [0.0,0.0,float("nan"),0.0],{"controller_regime":"wing_borne_airspeed","reason":"empty_certified_throttle_tracker_domain"}
        e0,t0=trim
        dn=float(nxt[0])-float(cur[0]); de=float(nxt[1])-float(cur[1]); dist=math.hypot(dn,de)
        desired_heading=math.atan2(de,dn) if dist>1e-6 else float(nxt[11])
        heading_now=float(cur[11]); heading_error=self._wrap_pi(desired_heading-heading_now)
        a=clip(k_h*heading_error,-max_a,max_a)
        gamma=float(nxt[10])
        # Phoenix's commissioned pilot-to-elevon convention maps a positive
        # elevator-domain request to positive symmetric elevon deflection.  The
        # WingMPET source model has Cm_delta < 0, therefore positive deflection
        # commands a nose-down moment.  Since canonical flight-path angle is
        # positive for climb (gamma=atan2(-v_d, |v_NE|)), the feedback sign must
        # be negative: a descending reference (gamma < 0) needs nose-down
        # authority, not the nose-up command produced by the former '+' sign.
        e=clip(e0-k_g*gamma,-max_e,max_e)
        va=float(nxt[9]); unsaturated_t=t0+k_v*(va-cruise); t=clip(unsaturated_t,tmin,tmax)
        return [float(a),float(e),float(t),0.0],{"desired_heading_rad":desired_heading,"heading_error_rad":heading_error,
            "flightpath_angle_rad":gamma,"airspeed_ref_m_s":va,"controller_regime":"wing_borne_airspeed",
            "unsaturated_throttle_request_norm":float(unsaturated_t),"throttle_request_norm":float(t),
            "throttle_request_bounds_norm":[float(tmin),float(tmax)],
            "certificate_saturation_applied":bool(abs(float(t)-float(unsaturated_t))>1e-12)}

    def _compiled_policy_action(self,candidate:dict[str,Any],cur:list[float],nxt:list[float],phase:str,
                                operating_context:dict[str,Any]|None)->tuple[list[float]|None,dict[str,Any]]:
        """Evaluate the TDOC-compiled OFFLINE prior inside its measured support.

        This returns pilot-domain values only.  It cannot assert verification,
        FINAL, execution authority, PWM, or a physical actuator command.
        """
        def absent(reason, *, status="UNAVAILABLE", **details):
            return None,{"used":False,"available":False,"availability":"UNAVAILABLE",
                         "status":status,"reason":reason,"offline_prior_only":True,
                         "actuation_authority":False,**details}
        binding=candidate.get("compiled_online_policy")
        if not isinstance(binding,dict):
            return absent("compiled_policy_absent")
        if (binding.get("availability")=="UNAVAILABLE" or binding.get("available") is False
                or binding.get("ok") is False):
            return absent(str(binding.get("reason") or "compiled_policy_unavailable"),
                          status=binding.get("status") if binding.get("status") in {"FAILED","REJECTED"} else "UNAVAILABLE")
        contract=binding.get("policy_contract") or {}; policy=binding.get("phase_policy") or {}
        if (not isinstance(contract,dict) or not isinstance(policy,dict)
                or not binding.get("artifact_hash") or contract.get("offline_prior_only") is not True
                or contract.get("actuation_authority") is not False):
            return absent("compiled_policy_contract_invalid",status="REJECTED")
        if str(policy.get("phase") or "")!=str(phase or ""):
            return absent("compiled_policy_phase_mismatch")
        if operating_context is not None and not isinstance(operating_context,dict):
            return absent("compiled_policy_feature_contract_unavailable")
        attitude=(operating_context or {}).get("attitude_rate_state") or {}
        try:
            qw,qx,qy,qz=[float(attitude[k]) for k in ("qw","qx","qy","qz")]
            n=math.sqrt(qw*qw+qx*qx+qy*qy+qz*qz)
            if not math.isfinite(n) or n<1e-12: raise ValueError("invalid_quaternion")
            qw,qx,qy,qz=(v/n for v in (qw,qx,qy,qz))
            pitch=math.asin(clip(2.0*(qw*qy-qz*qx),-1.0,1.0))
            features={
                "airspeed_m_s":float(cur[9]),"climb_m_s":-float(cur[5]),
                "desired_climb_m_s":-float(nxt[5]),
                "agl_m":float((operating_context or {}).get("altitude_agl_m")),
                "pitch_rad":pitch,"q_rate_rad_s":float(attitude["q_rate"]),
                "heading_error_rad":self._wrap_pi(
                    (math.atan2(float(nxt[1])-float(cur[1]),float(nxt[0])-float(cur[0]))
                     if math.hypot(float(nxt[0])-float(cur[0]),float(nxt[1])-float(cur[1]))>1e-9
                     else float(nxt[11]))-float(cur[11])),
            }
            if not all(math.isfinite(v) for v in features.values()):
                raise ValueError("non_finite_current_features")
        except (KeyError,TypeError,ValueError,IndexError,OverflowError) as exc:
            return absent("compiled_policy_feature_contract_unavailable",error=repr(exc),
                          artifact_hash=binding.get("artifact_hash"))
        try:
            order=list(policy["feature_order"]); stats=policy["feature_stats"]
            prototypes=list(policy["prototypes"])
            if not prototypes:
                return absent("compiled_policy_support_empty",artifact_hash=binding.get("artifact_hash"))
            if not order:
                return absent("compiled_policy_feature_support_empty",artifact_hash=binding.get("artifact_hash"))
            if (len(set(order))!=len(order) or any(k not in features for k in order)
                    or any(not math.isfinite(float(stats[k]["scale"])) or float(stats[k]["scale"])<=0 for k in order)):
                raise ValueError("invalid_policy_feature_contract")
            distances=[]
            for rec in prototypes:
                p=rec["features"]; total=0.0
                cmd=list(rec["pilot_command"])
                if len(cmd)!=4 or not all(math.isfinite(float(v)) and lo<=float(v)<=hi
                    for v,lo,hi in zip(cmd,(-1,-1,0,-1),(1,1,1,1))):
                    raise ValueError("invalid_policy_pilot_command")
                for key in order:
                    if not math.isfinite(float(p[key])): raise ValueError("non_finite_policy_features")
                    delta=(self._wrap_pi(features[key]-float(p[key])) if key=="heading_error_rad"
                           else features[key]-float(p[key]))
                    total+=(delta/max(float(stats[key]["scale"]),1e-9))**2
                distances.append((math.sqrt(total),rec))
            distances.sort(key=lambda item:item[0])
            # The acceptance radius is entirely support/dimension derived.  No
            # phase-specific hand-tuned threshold is introduced here.
            source_radius=float(policy.get("support_radius_normalized") or 0.0)
            if not math.isfinite(source_radius) or source_radius<0: raise ValueError("invalid_policy_support_radius")
            radius=max(source_radius,math.sqrt(float(len(order))))
            if distances[0][0]>radius:
                return absent("outside_compiled_policy_support",
                              nearest_distance=distances[0][0],support_radius=radius,
                              artifact_hash=binding.get("artifact_hash"))
            k=max(1,min(len(distances),int(policy.get("neighbor_count") or 1)))
            chosen=distances[:k]
            if chosen[0][0]<=1e-12:
                command=list(chosen[0][1]["pilot_command"]); weights=[1.0]
            else:
                weights=[1.0/max(item[0],1e-12) for item in chosen]
                denom=sum(weights)
                command=[sum(w*float(item[1]["pilot_command"][axis]) for w,item in zip(weights,chosen))/denom
                         for axis in range(4)]
            qlo=list(policy.get("command_q05") or command); qhi=list(policy.get("command_q95") or command)
            if (len(qlo)!=4 or len(qhi)!=4 or any(not math.isfinite(float(lo)) or not math.isfinite(float(hi))
                    or float(lo)>float(hi) for lo,hi in zip(qlo,qhi))):
                raise ValueError("invalid_policy_command_quantiles")
            if any(not lower<=float(lo)<=float(hi)<=upper for lo,hi,lower,upper
                   in zip(qlo,qhi,(-1,-1,0,-1),(1,1,1,1))):
                raise ValueError("policy_command_quantiles_outside_pilot_domain")
            command=[clip(float(command[i]),float(qlo[i]),float(qhi[i])) for i in range(4)]
            command=[clip(command[0],-1.0,1.0),clip(command[1],-1.0,1.0),
                     clip(command[2],0.0,1.0),clip(command[3],-1.0,1.0)]
            return command,{"used":True,"available":True,"availability":"AVAILABLE","status":"AVAILABLE",
                "reason":"inside_compiled_policy_support",
                "artifact_hash":binding.get("artifact_hash"),"phase":phase,"support_count":policy.get("support_count"),
                "nearest_distance":distances[0][0],"support_radius":radius,"neighbors_used":k,
                "offline_prior_only":True,"actuation_authority":False}
        except (KeyError,TypeError,ValueError,IndexError,OverflowError) as exc:
            return absent("compiled_policy_contract_invalid",status="REJECTED",error=repr(exc),
                          artifact_hash=binding.get("artifact_hash"))

    def _trajectory_nominal_controls(self,candidate:dict[str,Any])->tuple[list[list[float]],dict[str,Any]]:
        """Generate one regime-aware nominal pilot-domain sequence from a Layer-4 trajectory.

        Solution 6 keeps a *single* Phoenix controller path while separating the
        physical regimes inside it.  Vertical regimes use thrust/vertical-rate
        semantics; transition regimes blend that VTOL reference with the existing
        wing-borne tracker; wing-borne mission phases retain the legacy airspeed
        tracker.  Layer4 remains the owner of trajectory/terminal-corridor
        feasibility and WingMPET Full-Fidelity remains the physical verifier.
        """
        traj=candidate.get("trajectory") or candidate.get("predicted_trajectory") or []
        if not isinstance(traj,list) or len(traj)<2: return [],{"reason":"trajectory_reference_unavailable"}
        cfg=self.config.get("phoenix.nominal_tracking",{}) or {}
        max_a=max(0.01,float(cfg.get("max_aileron_norm",0.45))); max_e=max(0.01,float(cfg.get("max_elevator_norm",0.65)))
        e0=float(self.runtime_trim.get("elevator_norm",cfg.get("elevator_trim_norm",0.0))); t0=float(self.runtime_trim.get("throttle_norm",cfg.get("throttle_trim_norm",0.30)))
        horizon=len(candidate.get("actions") or []) or max(1,len(traj)-1); seq=[]; diagnostics=[]
        phase=str(candidate.get("tdoc_phase") or candidate.get("phase") or "").strip().lower()
        vtol_phases={"vertical_takeoff","hover","transition_forward","transition_back","vertical_landing"}
        operating_context=candidate.get("phoenix_operating_context") if isinstance(candidate.get("phoenix_operating_context"),dict) else None
        vertical_seed=self.offline_vtol_seed_controls(operating_context) if phase in vtol_phases else None
        if vertical_seed is not None and not vertical_seed.get("ok"):
            return [],{"reason":"vertical_operating_context_invalid","phase":phase,
                       "physics_basis":vertical_seed.get("physics_basis"),
                       "safety_semantics":"fail_closed_no_generic_forward_fallback_for_vtol_regime"}
        vmin=max(0.0,float(cfg.get("vertical_collective_min_norm",0.0))); vmax=min(1.0,float(cfg.get("vertical_collective_max_norm",0.95)))
        seed_phases=(vertical_seed or {}).get("phases") or {}
        hover_seed=float((seed_phases.get("hover") or {}).get("throttle_request_norm",t0)) if vertical_seed else t0
        takeoff_seed=float((seed_phases.get("vertical_takeoff") or {}).get("throttle_request_norm",hover_seed)) if vertical_seed else hover_seed
        hover_equilibrium=clip(float((vertical_seed or {}).get("equilibrium_collective_norm",vmin)),vmin,vmax)
        def commissioned_motor_tolerance(rec:dict[str,Any]|None)->float:
            """Return only uncertainty measured by the accepted SITL certificate."""
            if not isinstance(rec,dict):
                return 0.0
            values=(rec.get("motor_feedback_quantization_norm",0.0),
                    rec.get("max_target_motor_error_norm",0.0),
                    rec.get("max_affine_residual_norm",0.0))
            try:
                tolerance=max(0.0,*(float(v or 0.0) for v in values))
            except (TypeError,ValueError):
                return 0.0
            return tolerance if math.isfinite(tolerance) else 0.0
        def phase_motor_to_pilot_request(phase_name:str,motor_norm:float)->tuple[float,str]:
            rec=(self.runtime_phase_throttle_affine or {}).get(phase_name)
            if not isinstance(rec,dict):
                return float(motor_norm),"offline_identity_effective_action_mapping"
            gain=0.5*(float(rec["left_gain_motor_norm_per_request_norm"])+float(rec["right_gain_motor_norm_per_request_norm"]))
            motor_op=0.5*(float(rec["operating_left_motor_norm"])+float(rec["operating_right_motor_norm"]))
            if not math.isfinite(gain) or gain<=1e-12: raise ValueError("commissioned_phase_throttle_gain_invalid")
            target_bounds=rec.get("target_motor_bounds_norm")
            if not (isinstance(target_bounds,(list,tuple)) and len(target_bounds)==2):
                raise ValueError("commissioned_phase_target_motor_bounds_invalid")
            target_lo,target_hi=[float(v) for v in target_bounds]
            motor_tolerance=commissioned_motor_tolerance(rec)
            if (not all(math.isfinite(v) for v in (target_lo,target_hi,motor_norm))
                    or target_lo>target_hi
                    or float(motor_norm)<target_lo-motor_tolerance-1e-12
                    or float(motor_norm)>target_hi+motor_tolerance+1e-12):
                raise ValueError("desired_motor_outside_commissioned_phase_target")
            request=float(rec["operating_request_norm"])+(float(motor_norm)-motor_op)/gain
            lo,hi=[float(v) for v in rec["request_bounds_norm"]]
            # SERVO_OUTPUT_RAW is quantized at the PWM boundary.  Consequently,
            # inverting the fitted affine at an exact target-motor endpoint can
            # land a fraction of one PWM bin outside the measured pilot endpoint.
            # The certificate records that uncertainty explicitly; consume only
            # that measured tolerance and clamp back to the probed request range.
            request_tolerance=motor_tolerance/gain
            if request<lo-request_tolerance-1e-12 or request>hi+request_tolerance+1e-12:
                raise ValueError("desired_motor_outside_commissioned_phase_inverse")
            projected=bool(motor_norm<target_lo or motor_norm>target_hi or request<lo or request>hi)
            mapping=("online_commissioned_ArduPilot_inverse_measurement_tolerance_projection"
                     if projected else "online_commissioned_ArduPilot_inverse")
            # The accepted command never exceeds the actually probed pilot-domain
            # endpoint.  The tolerance only prevents a sub-bin model request from
            # being rejected before it is projected to that endpoint.
            return clip(request,lo,hi),mapping
        vh=float(self.config.get("phoenix.transition.v_hover_max",3.0)); vf=float(self.config.get("phoenix.transition.v_forward_min",9.0))
        def lam_from_va(va:float)->float: return clip((float(va)-vh)/max(vf-vh,1e-9),0.0,1.0)

        # VTOL gains are deliberately bounded and do not create a second solver.
        k_to_v=max(0.0,float(cfg.get("vertical_takeoff_climb_rate_gain_per_m_s",0.012)))
        to_corr_max=max(0.0,float(cfg.get("vertical_takeoff_max_feedback_correction_norm",0.04)))
        k_h_alt=max(0.0,float(cfg.get("hover_altitude_error_gain_per_m",0.004)))
        k_h_vd=max(0.0,float(cfg.get("hover_down_velocity_gain_per_m_s",0.010)))
        h_corr_max=max(0.0,float(cfg.get("hover_max_feedback_correction_norm",0.04)))
        land_ff=max(0.0,float(cfg.get("vertical_landing_descent_feedforward_gain_per_m_s",0.012)))
        land_fb=max(0.0,float(cfg.get("vertical_landing_descent_error_gain_per_m_s",0.020)))
        land_red_max=max(0.0,float(cfg.get("vertical_landing_max_collective_reduction_norm",0.08)))
        land_boost_max=max(0.0,float(cfg.get("vertical_landing_max_braking_boost_norm",0.06)))
        source_takeoff_floor=clip(min(takeoff_seed-to_corr_max,hover_seed-h_corr_max),vmin,vmax)
        commissioned_takeoff=(self.runtime_phase_throttle_affine or {}).get("vertical_takeoff") or {}
        commissioned_takeoff_tolerance=commissioned_motor_tolerance(commissioned_takeoff)
        commissioned_target_bounds=commissioned_takeoff.get("target_motor_bounds_norm")
        commissioned_takeoff_floor=(float(commissioned_target_bounds[0])
            if isinstance(commissioned_target_bounds,(list,tuple)) and len(commissioned_target_bounds)==2
            else source_takeoff_floor)
        # WingMPET's authoritative airborne VTOL gate requires Tsum >= m*g.
        # A takeoff trajectory may request a temporary reduction while still
        # climbing, but emitting that sub-hover request conflicts with the verified
        # phase contract as soon as ground support ends.  Align the Layer5 input
        # envelope with the source-derived equilibrium; no numeric margin is added.
        takeoff_floor=max(source_takeoff_floor,commissioned_takeoff_floor,hover_equilibrium)
        takeoff_floor_diag={"used":False,"reason":"same_cycle_attitude_unavailable",
                            "base_collective_norm":takeoff_floor}
        attitude_for_floor=(operating_context or {}).get("attitude_rate_state") if isinstance(operating_context,dict) else None
        if phase=="vertical_takeoff" and isinstance(attitude_for_floor,dict):
            try:
                fw,fx,fy,fz=[float(attitude_for_floor[k]) for k in ("qw","qx","qy","qz")]
                fnorm=math.sqrt(fw*fw+fx*fx+fy*fy+fz*fz)
                if not math.isfinite(fnorm) or fnorm<1e-9:
                    raise ValueError("invalid_quaternion")
                fw,fx,fy,fz=[v/fnorm for v in (fw,fx,fy,fz)]
                # WingMPET's Phoenix thrust is body +X.  R_bn[2,0] is its
                # down-axis projection; upward thrust fraction is therefore
                # -R_bn[2,0].  Since T~u^2, the exact source-law hover floor at
                # the measured tilt is u_eq/sqrt(fraction), not u_eq/fraction.
                vertical_fraction=-(2.0*(fx*fz-fw*fy))
                if not math.isfinite(vertical_fraction) or vertical_fraction<=1e-9:
                    raise ValueError("takeoff_thrust_axis_has_no_upward_component")
                requested_floor=hover_equilibrium/math.sqrt(min(vertical_fraction,1.0))
                takeoff_ceiling=min(vmax,takeoff_seed+to_corr_max)
                takeoff_floor=max(takeoff_floor,min(requested_floor,takeoff_ceiling))
                takeoff_floor_diag={
                    "used":True,
                    "method":"source_motor_law_same_transaction_quaternion_vertical_projection",
                    "vertical_thrust_fraction":vertical_fraction,
                    "equilibrium_collective_norm":hover_equilibrium,
                    "unclipped_tilt_collective_floor_norm":requested_floor,
                    "applied_tilt_collective_floor_norm":takeoff_floor,
                    "commissioned_ceiling_norm":takeoff_ceiling,
                    "manual_margin_used":False,
                    "full_fidelity_gate_remains_authoritative":True,
                }
            except (KeyError,TypeError,ValueError,ZeroDivisionError) as exc:
                # Do not invent an attitude correction when the same-cycle state
                # is invalid.  The existing source equilibrium and downstream FF
                # gate remain fail-closed authorities.
                takeoff_floor_diag={"used":False,"reason":"same_cycle_attitude_projection_invalid",
                                    "error":repr(exc),"base_collective_norm":takeoff_floor}

        def trajectory_vertical_collective(cur:list[float],nxt:list[float],*,allow_bounded_projection:bool=False)->tuple[float,dict[str,Any]]:
            """Invert a verified Layer4 vertical first move through source motor physics."""
            if vertical_seed is None:
                raise ValueError("vertical_seed_unavailable")
            pb=vertical_seed.get("physics_basis") or {}
            mass=float(pb["mass_kg"]); gravity=float(pb["gravity_m_s2"])
            motor_count=float(pb["motor_count"]); k_t=float(pb["motor_k_t_N_per_rad_s2"])
            omega_max=float(pb["motor_omega_max_rad_s"])
            forecast_step=max(1e-6,float((operating_context or {}).get(
                "forecast_step_s",cfg.get("vertical_pitch_prediction_step_s",1.0))))
            vd0=float(cur[5]); vd1=float(nxt[5])
            accel_down=(vd1-vd0)/forecast_step
            required_thrust=mass*(gravity-accel_down)
            available=motor_count*k_t*omega_max*omega_max
            if (not all(math.isfinite(v) for v in (
                    mass,gravity,motor_count,k_t,omega_max,forecast_step,vd0,vd1,
                    accel_down,required_thrust,available))
                    or mass<=0.0 or gravity<=0.0 or motor_count<=0.0
                    or k_t<=0.0 or omega_max<=0.0 or available<=0.0
                    or required_thrust<0.0 or required_thrust>available):
                raise ValueError("vertical_trajectory_thrust_outside_source_motor_envelope")
            collective=math.sqrt(required_thrust/available)
            if (not allow_bounded_projection) and (collective<vmin-1e-12 or collective>vmax+1e-12):
                raise ValueError("vertical_trajectory_collective_outside_physical_bounds")
            return float(collective),{
                "method":"source_motor_law_trajectory_acceleration_inverse",
                "forecast_step_s":forecast_step,
                "velocity_down_now_m_s":vd0,"velocity_down_ref_m_s":vd1,
                "acceleration_down_ref_m_s2":accel_down,
                "required_total_thrust_N":required_thrust,
                "available_total_thrust_N":available,
                "mass_kg":mass,"gravity_m_s2":gravity,
                "manual_gain_used":False,"layer4_first_move_source":True,
                "bounded_projection_required":bool(collective<vmin or collective>vmax),
            }

        # A vertical candidate must retain the tailsitter attitude over the complete
        # Layer4 horizon, not merely balance thrust.  Form a bounded, source-bound
        # pitch/rate policy in physical surface space and invert only the commissioned
        # pilot-to-elevon slope to return a pilot-domain request.  ArduPilot remains
        # the Online mixer; this calculation neither emits PWM nor commands JSBSim.
        attitude_state=(operating_context or {}).get("attitude_rate_state") if isinstance(operating_context,dict) else None
        pitch_plan=None
        if phase in {"vertical_takeoff","hover"} and isinstance(attitude_state,dict):
            try:
                qw,qx,qy,qz=[float(attitude_state[k]) for k in ("qw","qx","qy","qz")]
                qnorm=math.sqrt(qw*qw+qx*qx+qy*qy+qz*qz)
                if not math.isfinite(qnorm) or qnorm<1e-9: raise ValueError("invalid_quaternion")
                qw,qx,qy,qz=[v/qnorm for v in (qw,qx,qy,qz)]
                pitch=math.asin(clip(2.0*(qw*qy-qz*qx),-1.0,1.0))
                q_rate=float(attitude_state["q_rate"])
                if not all(math.isfinite(v) for v in (pitch,q_rate)): raise ValueError("non_finite_attitude_rate")
                attitude_reference=(operating_context or {}).get("vertical_attitude_reference") or {}
                qref_values=attitude_reference.get("quaternion_wxyz") if isinstance(attitude_reference,dict) else None
                attitude_error=None; roll_attitude_error=None
                horizontal_guidance_diag={"applied":False,"reason":"mesa_local_guidance_unavailable"}
                if isinstance(qref_values,(list,tuple)) and len(qref_values)==4:
                    tw,tx,ty,tz=[float(v) for v in qref_values]
                    tnorm=math.sqrt(tw*tw+tx*tx+ty*ty+tz*tz)
                    if not math.isfinite(tnorm) or tnorm<1e-9: raise ValueError("invalid_vertical_attitude_reference")
                    tw,tx,ty,tz=[v/tnorm for v in (tw,tx,ty,tz)]
                    local_guidance=(operating_context or {}).get("mesa_local_guidance")
                    if isinstance(local_guidance,dict):
                        try:
                            if (local_guidance.get("schema")!="TDOC-MESA-local-guidance-v1"
                                    or local_guidance.get("frame")!="PROJECTED_NED"
                                    or local_guidance.get("navigation_authority")!="MESA_STD"
                                    or local_guidance.get("truth_injection_to_mesa") is not False
                                    or local_guidance.get("actuation_authority") is not False):
                                raise ValueError("mesa_local_guidance_contract_invalid")
                            target=[float(v) for v in local_guidance["target_position_ned_m"][:3]]
                            position=[float(v) for v in traj[0][:3]]
                            velocity=[float(v) for v in traj[0][3:6]]
                            if not all(math.isfinite(v) for v in target+position+velocity):
                                raise ValueError("mesa_local_guidance_non_finite")
                            forecast=max(1e-6,float((operating_context or {}).get(
                                "forecast_step_s",cfg.get("vertical_pitch_prediction_step_s",1.0))))
                            contract_steps=max(1,int((operating_context or {}).get("horizon_steps",horizon)))
                            # A critically damped horizontal hold is derived from
                            # the verified prediction horizon.  Therefore no
                            # phase-specific hand-tuned position or velocity gain
                            # is introduced: omega_n=4/T_settle, Kp=omega_n^2,
                            # Kd=2*omega_n.
                            settling=max(forecast*contract_steps,forecast)
                            omega_n=4.0/settling
                            desired_accel=[
                                -2.0*omega_n*velocity[j]
                                -omega_n*omega_n*(position[j]-target[j])
                                for j in range(2)
                            ]
                            pb=(vertical_seed or {}).get("physics_basis") or {}
                            mass=float(pb["mass_kg"]); gravity=float(pb["gravity_m_s2"])
                            available=float(pb["motor_count"])*float(pb["motor_k_t_N_per_rad_s2"])*float(pb["motor_omega_max_rad_s"])**2
                            # One attitude reference is held across this verified
                            # prediction horizon.  Size its horizontal component
                            # against the largest vertical-force demand in that same
                            # horizon, not only the first forecast edge.  The former
                            # first-edge cap could be valid at i=0 yet make a later
                            # trajectory inverse exceed the unchanged commissioned
                            # collective ceiling before WingMPET was even invoked.
                            force_edges=min(contract_steps,len(traj)-1)
                            vertical_force_profile=[]
                            for edge in range(force_edges):
                                accel_down=(float(traj[edge+1][5])-float(traj[edge][5]))/forecast
                                vertical_force_profile.append(mass*(gravity-accel_down))
                            if (not vertical_force_profile
                                    or not all(math.isfinite(v) and v>0.0 for v in vertical_force_profile)):
                                raise ValueError("mesa_local_guidance_vertical_force_horizon_invalid")
                            vertical_force=max(vertical_force_profile)
                            phase_hi=vmax
                            if phase=="vertical_takeoff":
                                phase_hi=min(phase_hi,takeoff_seed+to_corr_max)
                                bounds=commissioned_takeoff.get("target_motor_bounds_norm")
                            else:
                                phase_hi=min(phase_hi,hover_seed+h_corr_max)
                                bounds=((self.runtime_phase_throttle_affine or {}).get("hover") or {}).get("target_motor_bounds_norm")
                            if isinstance(bounds,(list,tuple)) and len(bounds)==2:
                                phase_hi=min(phase_hi,float(bounds[1]))
                            maximum_total_force=available*phase_hi*phase_hi
                            horizontal_force_cap=math.sqrt(max(
                                maximum_total_force*maximum_total_force-vertical_force*vertical_force,0.0))
                            desired_force=[mass*desired_accel[0],mass*desired_accel[1]]
                            desired_force_norm=math.hypot(*desired_force)
                            if desired_force_norm>horizontal_force_cap and desired_force_norm>1e-12:
                                scale=horizontal_force_cap/desired_force_norm
                                desired_force=[scale*v for v in desired_force]
                            applied_force_norm=math.hypot(*desired_force)
                            thrust=[desired_force[0],desired_force[1],-vertical_force]
                            thrust_norm=math.sqrt(sum(v*v for v in thrust))
                            if (not all(math.isfinite(v) for v in (
                                    mass,gravity,available,vertical_force,phase_hi,
                                    horizontal_force_cap,desired_force_norm,applied_force_norm,thrust_norm))
                                    or mass<=0.0 or gravity<=0.0 or available<=0.0
                                    or vertical_force<=0.0 or phase_hi<=0.0 or thrust_norm<=1e-12):
                                raise ValueError("mesa_local_guidance_force_synthesis_invalid")
                            desired_axis=[v/thrust_norm for v in thrust]
                            base_axis=[
                                1.0-2.0*(ty*ty+tz*tz),
                                2.0*(tx*ty+tw*tz),
                                2.0*(tx*tz-tw*ty),
                            ]
                            dot=clip(sum(a*b for a,b in zip(base_axis,desired_axis)),-1.0,1.0)
                            cross=[
                                base_axis[1]*desired_axis[2]-base_axis[2]*desired_axis[1],
                                base_axis[2]*desired_axis[0]-base_axis[0]*desired_axis[2],
                                base_axis[0]*desired_axis[1]-base_axis[1]*desired_axis[0],
                            ]
                            aw,ax,ay,az=1.0+dot,cross[0],cross[1],cross[2]
                            anorm=math.sqrt(aw*aw+ax*ax+ay*ay+az*az)
                            if anorm<=1e-12: raise ValueError("mesa_local_guidance_antipodal_thrust_axis")
                            aw,ax,ay,az=[v/anorm for v in (aw,ax,ay,az)]
                            # World-frame minimal rotation aligns the source-bound
                            # vertical body-X thrust axis with the MESA local hold
                            # vector while retaining the mission heading as the
                            # remaining attitude reference.
                            tw,tx,ty,tz=(
                                aw*tw-ax*tx-ay*ty-az*tz,
                                aw*tx+ax*tw+ay*tz-az*ty,
                                aw*ty-ax*tz+ay*tw+az*tx,
                                aw*tz+ax*ty-ay*tx+az*tw,
                            )
                            adjusted_norm=math.sqrt(tw*tw+tx*tx+ty*ty+tz*tz)
                            tw,tx,ty,tz=[v/adjusted_norm for v in (tw,tx,ty,tz)]
                            horizontal_guidance_diag={
                                "applied":True,
                                "schema":"Layer5-MESA-local-guidance-thrust-attitude-v1",
                                "source_schema":local_guidance.get("schema"),
                                "navigation_authority":"MESA_STD",
                                "position_error_ned_m":[position[0]-target[0],position[1]-target[1]],
                                "velocity_error_ned_m_s":[velocity[0],velocity[1]],
                                "settling_horizon_s":settling,
                                "natural_frequency_rad_s":omega_n,
                                "desired_horizontal_acceleration_ned_m_s2":desired_accel,
                                "desired_horizontal_force_N":desired_force_norm,
                                "applied_horizontal_force_N":applied_force_norm,
                                "horizontal_force_cap_N":horizontal_force_cap,
                                "vertical_force_N":vertical_force,
                                "vertical_force_profile_N":vertical_force_profile,
                                "vertical_force_horizon_max_N":vertical_force,
                                "force_cap_horizon_edges":force_edges,
                                "maximum_collective_norm":phase_hi,
                                "target_thrust_axis_ned":desired_axis,
                                "gain_source":"critical_damping_from_L4_forecast_horizon",
                                "force_limit_source":"Phoenix_source_motor_law_and_commissioned_phase_envelope",
                                "manual_gain_used":False,
                                "direct_pwm":False,
                                "direct_jsbsim_actuation":False,
                                "full_fidelity_gate_remains_authoritative":True,
                            }
                        except (KeyError,TypeError,ValueError,ZeroDivisionError) as exc:
                            horizontal_guidance_diag={"applied":False,
                                "reason":"mesa_local_guidance_synthesis_unavailable","error":repr(exc)}
                    if horizontal_guidance_diag.get("applied") is True and not self.dt_validated:
                        # Phoenix currently has source/commissioned roll and pitch
                        # elevon authority, but no qualified differential-thrust yaw
                        # authority.  A generic quaternion target can therefore put
                        # most of the thrust-axis correction in q_error.z, which the
                        # authorized actuator set cannot realize.  Allocate the same
                        # MESA thrust-axis request into the reachable body-X/body-Y
                        # sequence instead: roll about body X first (which preserves
                        # thrust), then pitch about the re-oriented body Y axis.
                        # This is kinematic allocation, not a new gain or actuator.
                        desired_axis=[float(v) for v in horizontal_guidance_diag["target_thrust_axis_ned"]]
                        body_x=[1.0-2.0*(qy*qy+qz*qz),
                                2.0*(qx*qy+qw*qz),
                                2.0*(qx*qz-qw*qy)]
                        body_y=[2.0*(qx*qy-qw*qz),
                                1.0-2.0*(qx*qx+qz*qz),
                                2.0*(qy*qz+qw*qx)]
                        body_z=[2.0*(qx*qz+qw*qy),
                                2.0*(qy*qz-qw*qx),
                                1.0-2.0*(qx*qx+qy*qy)]
                        axis_dot=clip(sum(a*b for a,b in zip(body_x,desired_axis)),-1.0,1.0)
                        axis_cross=[body_x[1]*desired_axis[2]-body_x[2]*desired_axis[1],
                                    body_x[2]*desired_axis[0]-body_x[0]*desired_axis[2],
                                    body_x[0]*desired_axis[1]-body_x[1]*desired_axis[0]]
                        cross_norm=math.sqrt(sum(v*v for v in axis_cross))
                        if cross_norm<=1e-12:
                            if axis_dot<0.0: raise ValueError("mesa_local_guidance_antipodal_reachable_axis")
                            roll_increment=0.0; pitch_increment=0.0; pitch_alignment=1.0
                            axis_allocation={
                                "raw_roll_alignment_increment_rad":0.0,
                                "canonical_axis_symmetry_applied":False,
                                "pitch_axis_sign":1.0,
                                "pitch_alignment_factor":1.0,
                                "method":"already_aligned_body_x_axis_v1",
                            }
                        else:
                            rotation_axis=[v/cross_norm for v in axis_cross]
                            # After a body-X roll alpha, y'=cos(alpha)y+sin(alpha)z.
                            # Align y' with x_body cross x_desired before asking the
                            # pitch surface to tilt the thrust axis.
                            roll_increment,pitch_increment,axis_allocation=self._shortest_roll_pitch_axis_allocation(
                                sum(a*b for a,b in zip(rotation_axis,body_y)),
                                sum(a*b for a,b in zip(rotation_axis,body_z)),
                                math.acos(axis_dot))
                            pitch_alignment=float(axis_allocation["pitch_alignment_factor"])
                        roll_attitude_error=self._wrap_pi(-roll_increment)
                        attitude_error=self._wrap_pi(-pitch_increment)

                        # Publish the actually committable intermediate attitude,
                        # not the unreachable three-axis reference.  The desired
                        # MESA thrust axis remains in the diagnostic above and the
                        # WingMPET Full-Fidelity horizon remains authoritative.
                        cr,sr=math.cos(0.5*roll_increment),math.sin(0.5*roll_increment)
                        cp,sp=math.cos(0.5*pitch_increment),math.sin(0.5*pitch_increment)
                        rw,rx,ry,rz=(cr,sr,0.0,0.0)
                        pw,px,py,pz=(cp,0.0,sp,0.0)
                        iw,ix,iy,iz=(
                            rw*pw-rx*px-ry*py-rz*pz,
                            rw*px+rx*pw+ry*pz-rz*py,
                            rw*py-rx*pz+ry*pw+rz*px,
                            rw*pz+rx*py-ry*px+rz*pw,
                        )
                        tw,tx,ty,tz=(
                            qw*iw-qx*ix-qy*iy-qz*iz,
                            qw*ix+qx*iw+qy*iz-qz*iy,
                            qw*iy-qx*iz+qy*iw+qz*ix,
                            qw*iz+qx*iy-qy*ix+qz*iw,
                        )
                        horizontal_guidance_diag={**horizontal_guidance_diag,
                            "attitude_allocation":"underactuated_body_x_roll_then_body_y_pitch",
                            "qualified_attitude_axes":["body_x_roll","body_y_pitch"],
                            "unqualified_attitude_axis":"body_z_yaw",
                            "differential_thrust_used":False,
                            "roll_alignment_increment_rad":roll_increment,
                            "raw_roll_alignment_increment_rad":axis_allocation.get("raw_roll_alignment_increment_rad",roll_increment),
                            "canonical_axis_symmetry_applied":bool(axis_allocation.get("canonical_axis_symmetry_applied",False)),
                            "pitch_axis_sign":float(axis_allocation.get("pitch_axis_sign",1.0)),
                            "full_thrust_axis_separation_rad":math.acos(axis_dot),
                            "pitch_alignment_factor":pitch_alignment,
                            "committable_pitch_increment_rad":pitch_increment,
                            "allocation_source":"quaternion_kinematics_plus_qualified_Phoenix_actuator_axes",
                        }
                    else:
                        # q_error = conjugate(q_target) * q_current.  Extract the
                        # pitch-axis twist rather than Euler pitch: Euler asin aliases
                        # +90+delta to +90-delta and reverses the controller sign after
                        # a tailsitter crosses vertical.  The twist remains continuous
                        # and is derived from the mission-bearing launch attitude.
                        ew=tw*qw+tx*qx+ty*qy+tz*qz
                        ex=tw*qx-tx*qw-ty*qz+tz*qy
                        ey=tw*qy+tx*qz-ty*qw-tz*qx
                        ez=tw*qz-tx*qy+ty*qx-tz*qw
                        if ew<0.0:
                            ew,ex,ey,ez=(-ew,-ex,-ey,-ez)
                        if math.hypot(ew,ey)<1e-9: raise ValueError("vertical_attitude_pitch_twist_undefined")
                        attitude_error=self._wrap_pi(2.0*math.atan2(ey,ew))
                        if math.hypot(ew,ex)<1e-9: raise ValueError("vertical_attitude_roll_twist_undefined")
                        roll_attitude_error=self._wrap_pi(2.0*math.atan2(ex,ew))
                matrix=np.asarray(self.runtime_wingmpet_surface_matrix_rad,dtype=float)
                if matrix.shape!=(2,2): raise ValueError("commissioned_mixer_matrix_unavailable")
                symmetric_gain_rad_per_norm=0.5*(float(matrix[0,1])+float(matrix[1,1]))
                differential_gain_rad_per_norm=0.5*(float(matrix[1,0])-float(matrix[0,0]))
                if abs(symmetric_gain_rad_per_norm)<1e-9: raise ValueError("commissioned_symmetric_pitch_gain_zero")
                if abs(differential_gain_rad_per_norm)<1e-9: raise ValueError("commissioned_differential_roll_gain_zero")
                pb=(vertical_seed or {}).get("physics_basis") or {}
                terms=pb.get("takeoff_propwash_terms") or {}
                pitch_slope=float(pb["takeoff_propwash_pitch_slope_Nm_per_rad"])
                iyy=float(terms["Iyy_kg_m2"])
                pitch_effectiveness=pitch_slope/iyy
                if not math.isfinite(pitch_effectiveness) or pitch_effectiveness>=-1e-9:
                    raise ValueError("source_pitch_effectiveness_invalid")
                roll_slope=float(pb["takeoff_propwash_roll_slope_Nm_per_rad"])
                ixx=float(terms["Ixx_kg_m2"])
                roll_effectiveness=roll_slope/ixx
                if not math.isfinite(roll_effectiveness) or roll_effectiveness<=1e-9:
                    raise ValueError("source_roll_effectiveness_invalid")
                schedule_airspeed=max(0.0,float((operating_context or {}).get("airspeed_m_s",0.0)))
                # Synthesize the local attitude tracker from the source-bound
                # propwash effectiveness and Layer-4 timing contract.  The former
                # implementation switched between hand-tuned rate gains (0.02 and
                # 0.40) at a fixed airspeed breakpoint.  Under asynchronous artifact
                # reuse that produced a saturated, strongly over-damped bang-bang
                # response.  For x=[pitch_error, pitch_rate], choose the standard
                # critically damped second-order poles whose 2% settling time is one
                # verified forecast interval: omega_n=4/Ts, Kp=omega_n^2/|b| and
                # Kd=2*omega_n/|b|.  No plant command, safety limit or FF gate is
                # inferred here; this only replaces calibratable fixed gains.  The
                # full horizon remains a feasibility/terminal certificate and is not
                # a permissible first-move settling deadline.
                forecast_step=max(1e-3,float((operating_context or {}).get(
                    "forecast_step_s",cfg.get("vertical_pitch_prediction_step_s",1.0))))
                contract_horizon=max(1,int((operating_context or {}).get("horizon_steps",horizon)))
                ff_lane=str((operating_context or {}).get("full_fidelity_lane") or "COMMIT_SEGMENT")
                # The committable move must regulate within the next forecast
                # interval.  For the non-committable backup tail, reserve the latter
                # half of the certified horizon for terminal-invariance evidence and
                # require the critically damped transient to settle in the former
                # half.  Using the *complete* horizon as the settling deadline leaves
                # no terminal reserve and lets an initially tilted/rate-loaded VTOL
                # drift outside the terrain corridor before the terminal state is
                # reached.  This split is derived solely from the Layer-4 horizon;
                # it introduces no airframe/terrain gain or hand-tuned threshold.
                transient_steps=max(1,(contract_horizon+1)//2)
                terminal_reserve_steps=max(0,contract_horizon-transient_steps)
                settling_horizon=(forecast_step*transient_steps
                                  if ff_lane=="FORECAST_HORIZON" else forecast_step)
                natural_frequency=4.0/settling_horizon
                prediction_step=max(1e-3,float((operating_context or {}).get(
                    "execution_segment_s",cfg.get("vertical_pitch_prediction_step_s",1.0))))
                effectiveness_magnitude=-pitch_effectiveness
                # Place a repeated discrete closed-loop pole for the exact lane
                # clock.  Reusing continuous-time gains with an explicit 1-s
                # horizon update made the forecast tracker numerically unstable,
                # even though the 0.1-s committable move was stable.  The pole is
                # derived from the existing 2% settling-time convention; no tuned
                # phase gain is introduced.
                discrete_pole=math.exp(-natural_frequency*prediction_step)
                pole_gap=1.0-discrete_pole
                attitude_gain=(pole_gap*pole_gap)/(prediction_step*prediction_step*effectiveness_magnitude)
                rate_gain=(2.0*pole_gap-0.5*pole_gap*pole_gap)/(prediction_step*effectiveness_magnitude)
                roll_attitude_gain=(pole_gap*pole_gap)/(prediction_step*prediction_step*roll_effectiveness)
                roll_rate_gain=(2.0*pole_gap-0.5*pole_gap*pole_gap)/(prediction_step*roll_effectiveness)
                pitch_plan={"pitch_rad":pitch,"q_rate_rad_s":q_rate,
                    "p_rate_rad_s":float(attitude_state["p_rate"]),
                    "target_pitch_rad":math.pi/2.0,
                    "attitude_error_rad":(float(attitude_error) if attitude_error is not None else pitch-math.pi/2.0),
                    "roll_attitude_error_rad":(float(roll_attitude_error) if roll_attitude_error is not None else 0.0),
                    "attitude_error_source":(("mesa_reachable_thrust_axis_allocation"
                        if horizontal_guidance_diag.get("attitude_allocation") else
                        "mission_reference_quaternion_pitch_twist") if attitude_error is not None else "legacy_euler_pitch_fallback"),
                    "target_quaternion_wxyz":([tw,tx,ty,tz] if attitude_error is not None else None),
                    "horizontal_local_guidance":horizontal_guidance_diag,
                    "attitude_gain_rad_per_rad":attitude_gain,
                    "rate_gain_rad_per_rad_s":rate_gain,
                    "roll_attitude_gain_rad_per_rad":roll_attitude_gain,
                    "roll_rate_gain_rad_per_rad_s":roll_rate_gain,
                    "schedule_airspeed_m_s":schedule_airspeed,
                    "gain_schedule":"SOURCE_DYNAMICS_DISCRETE_CRITICAL_DAMPING",
                    "full_fidelity_lane":ff_lane,
                    "forecast_step_s":forecast_step,"horizon_steps":contract_horizon,
                    "transient_steps":transient_steps,
                    "terminal_reserve_steps":terminal_reserve_steps,
                    "settling_horizon_s":settling_horizon,"natural_frequency_rad_s":natural_frequency,
                    "prediction_pole":discrete_pole,
                    "surface_limit_rad":min(float(self.elevon_max),max(0.0,float(cfg.get("vertical_pitch_surface_limit_rad",0.08)))),
                    "prediction_step_s":prediction_step,
                    "pitch_effectiveness_rad_s2_per_rad":pitch_effectiveness,
                    "roll_effectiveness_rad_s2_per_rad":roll_effectiveness,
                    "symmetric_gain_rad_per_norm":symmetric_gain_rad_per_norm,
                    "differential_gain_rad_per_norm":differential_gain_rad_per_norm,
                    "source":str(attitude_state.get("source") or "same_cycle_physical_replay_state")}
            except (KeyError,TypeError,ValueError):
                pitch_plan=None

        for i in range(horizon):
            cur=traj[min(i,len(traj)-1)]; nxt=traj[min(i+1,len(traj)-1)]
            if not isinstance(cur,(list,tuple)) or not isinstance(nxt,(list,tuple)) or len(cur)<12 or len(nxt)<12:
                return [],{"reason":"trajectory_state_dimension_invalid"}
            forward,fd=self._forward_nominal_control(cur,nxt,cfg=cfg,trim=(e0,t0))
            learned_u,learned_diag=self._compiled_policy_action(candidate,cur,nxt,phase,operating_context)
            va=float(nxt[9]); vd_now=float(cur[5]); vd_ref=float(nxt[5])
            if phase=="vertical_takeoff":
                climb_up=max(0.0,-vd_ref)
                climb_now=max(0.0,-vd_now)
                climb_error=climb_up-climb_now
                try:
                    throttle,vertical_inverse=trajectory_vertical_collective(cur,nxt)
                except (KeyError,TypeError,ValueError,ZeroDivisionError) as exc:
                    return [],{"reason":"vertical_takeoff_trajectory_collective_unavailable",
                               "phase":phase,"error":repr(exc),
                               "safety_semantics":"fail_closed_no_fixed_gain_substitution"}
                local_hold=(pitch_plan or {}).get("horizontal_local_guidance") or {}
                if local_hold.get("applied") is True:
                    try:
                        axis=[float(v) for v in local_hold["target_thrust_axis_ned"]]
                        vertical_fraction=-axis[2]
                        available=float(vertical_inverse["available_total_thrust_N"])
                        vertical_required=float(vertical_inverse["required_total_thrust_N"])
                        if (not all(math.isfinite(v) for v in axis+[vertical_fraction,available,vertical_required])
                                or vertical_fraction<=1e-9 or vertical_fraction>1.0+1e-12
                                or available<=0.0 or vertical_required<=0.0):
                            raise ValueError("local_hold_collective_geometry_invalid")
                        uncorrected=float(throttle)
                        throttle=math.sqrt((vertical_required/vertical_fraction)/available)
                        vertical_inverse={**vertical_inverse,"horizontal_hold_thrust_compensation":{
                            "used":True,"uncorrected_collective_norm":uncorrected,
                            "compensated_collective_norm":throttle,
                            "vertical_thrust_fraction":vertical_fraction,
                            "target_thrust_axis_ned":axis,
                            "method":"source_motor_law_vector_thrust_inverse",
                            "manual_margin_used":False,
                            "full_fidelity_gate_remains_authoritative":True}}
                    except (KeyError,TypeError,ValueError,ZeroDivisionError) as exc:
                        return [],{"reason":"vertical_takeoff_local_hold_collective_unavailable",
                                   "phase":phase,"error":repr(exc),
                                   "safety_semantics":"fail_closed_no_uncompensated_tilt"}
                # The first Online takeoff segment may start below hover speed while
                # ground support is still valid.  A static thrust inverse can then
                # finish that segment a fraction below the WingMPET airborne gate,
                # although the following command is feasible at steady state.  Use
                # the source-identified first-order motor law to make the *end* of
                # the committable segment reach the next airborne collective.  This
                # is a one-segment inverse, not a relaxed gate or a horizon command.
                motor_state=(operating_context or {}).get("motor_speed_state") if isinstance(operating_context,dict) else None
                if i==0 and isinstance(motor_state,dict):
                    try:
                        omega_left=float(motor_state["omega_left_rad_s"])
                        omega_right=float(motor_state["omega_right_rad_s"])
                        execution_segment=float((operating_context or {})["execution_segment_s"])
                        if (not all(math.isfinite(v) for v in (omega_left,omega_right,execution_segment,self.tau_up))
                                or min(omega_left,omega_right)<0.0 or execution_segment<=0.0 or self.tau_up<=0.0):
                            raise ValueError("takeoff_motor_transient_context_invalid")
                        next_collective=float(throttle)
                        if i+2<len(traj):
                            next_collective,_=trajectory_vertical_collective(
                                traj[i+1],traj[i+2])
                        required_exit_collective=max(float(hover_equilibrium),float(throttle),next_collective)
                        current_min_omega=min(omega_left,omega_right)
                        required_exit_omega=required_exit_collective*float(self.omega_max)
                        decay=math.exp(-execution_segment/float(self.tau_up))
                        if 1.0-decay<=1e-12:
                            raise ValueError("takeoff_motor_transient_inverse_singular")
                        command_omega=(required_exit_omega-current_min_omega*decay)/(1.0-decay)
                        minimum_commit_collective=max(0.0,command_omega/float(self.omega_max))
                        transient_diag={
                            "used":False,"reason":"static_command_preserves_airborne_entry_requirement",
                            "minimum_commit_collective_norm":minimum_commit_collective,
                            "initial_min_motor_speed_rad_s":current_min_omega,
                            "required_exit_motor_speed_rad_s":required_exit_omega,
                            "execution_segment_s":execution_segment,
                            "motor_tau_up_s":float(self.tau_up),
                            "manual_margin_used":False,"first_move_only":True,
                            "full_fidelity_gate_remains_authoritative":True,
                        }
                        if float(throttle)<minimum_commit_collective-1e-12:
                            compensated_collective=minimum_commit_collective
                            if (not math.isfinite(compensated_collective)
                                    or compensated_collective<float(throttle)-1e-12
                                    or compensated_collective>vmax+1e-12):
                                raise ValueError("takeoff_motor_transient_inverse_outside_physical_bounds")
                            static_collective=float(throttle)
                            throttle=float(compensated_collective)
                            transient_diag={
                                "used":True,
                                "method":"source_motor_first_order_commit_segment_inverse",
                                "initial_min_motor_speed_rad_s":current_min_omega,
                                "required_exit_motor_speed_rad_s":required_exit_omega,
                                "static_collective_norm":static_collective,
                                "next_airborne_collective_norm":next_collective,
                                "compensated_collective_norm":throttle,
                                "minimum_commit_collective_norm":minimum_commit_collective,
                                "execution_segment_s":execution_segment,
                                "motor_tau_up_s":float(self.tau_up),
                                "manual_margin_used":False,
                                "first_move_only":True,
                                "full_fidelity_gate_remains_authoritative":True,
                            }
                        vertical_inverse={**vertical_inverse,"commit_segment_motor_transient":transient_diag}
                    except (KeyError,TypeError,ValueError,ZeroDivisionError) as exc:
                        return [],{"reason":"vertical_takeoff_motor_transient_inverse_unavailable",
                                   "phase":phase,"error":repr(exc),
                                   "safety_semantics":"fail_closed_no_static_or_manual_margin_substitution"}
                if throttle<takeoff_floor-1e-12:
                    requested_collective=float(throttle)
                    throttle=float(takeoff_floor)
                    vertical_inverse={**vertical_inverse,
                        "airborne_takeoff_floor_projection":{
                            "used":True,
                            "requested_collective_norm":requested_collective,
                            "projected_collective_norm":throttle,
                            "floor_source":"source_motor_force_balance_plus_commissioned_envelope",
                            "tilt_aware_floor":takeoff_floor_diag,
                            "manual_margin_used":False,
                            "full_fidelity_gate_remains_authoritative":True}}
                takeoff_ceiling=min(vmax,takeoff_seed+to_corr_max)
                if throttle>takeoff_ceiling+commissioned_takeoff_tolerance+1e-12:
                    return [],{"reason":"vertical_takeoff_trajectory_collective_outside_source_braking_envelope",
                               "phase":phase,"desired_motor_collective_norm":throttle,
                               "source_braking_envelope_norm":[takeoff_floor,takeoff_ceiling],
                               "commissioned_measurement_tolerance_norm":commissioned_takeoff_tolerance,
                               "trajectory_inverse":vertical_inverse,
                               "safety_semantics":"fail_closed_pending_broader_commissioned_evidence"}
                corr=throttle-takeoff_seed
                aileron=0.0; elevator=0.0; pitch_diag={"pitch_stabilization_active":False,"pitch_stabilization_reason":"attitude_rate_state_unavailable",
                    "roll_stabilization_active":False}
                if pitch_plan is not None:
                    pitch_now=float(pitch_plan["pitch_rad"]); q_now=float(pitch_plan["q_rate_rad_s"])
                    p_now=float(pitch_plan["p_rate_rad_s"])
                    attitude_error_now=float(pitch_plan["attitude_error_rad"])
                    roll_error_now=float(pitch_plan["roll_attitude_error_rad"])
                    surface=clip(
                        float(pitch_plan["rate_gain_rad_per_rad_s"])*q_now
                        +float(pitch_plan["attitude_gain_rad_per_rad"])*attitude_error_now,
                        -float(pitch_plan["surface_limit_rad"]),float(pitch_plan["surface_limit_rad"]))
                    elevator=clip(surface/float(pitch_plan["symmetric_gain_rad_per_norm"]),-max_e,max_e)
                    differential_surface=clip(
                        -float(pitch_plan["roll_rate_gain_rad_per_rad_s"])*p_now
                        -float(pitch_plan["roll_attitude_gain_rad_per_rad"])*roll_error_now,
                        -float(pitch_plan["surface_limit_rad"]),float(pitch_plan["surface_limit_rad"]))
                    aileron=clip(differential_surface/float(pitch_plan["differential_gain_rad_per_norm"]),-max_a,max_a)
                    applied_surface=float(pitch_plan["symmetric_gain_rad_per_norm"])*elevator
                    applied_differential_surface=float(pitch_plan["differential_gain_rad_per_norm"])*aileron
                    dt_pitch=float(pitch_plan["prediction_step_s"])
                    q_next=q_now+dt_pitch*float(pitch_plan["pitch_effectiveness_rad_s2_per_rad"])*applied_surface
                    p_next=p_now+dt_pitch*float(pitch_plan["roll_effectiveness_rad_s2_per_rad"])*applied_differential_surface
                    attitude_error_next=self._wrap_pi(attitude_error_now+0.5*dt_pitch*(q_now+q_next))
                    roll_error_next=self._wrap_pi(roll_error_now+0.5*dt_pitch*(p_now+p_next))
                    pitch_next=clip(float(pitch_plan["target_pitch_rad"])+attitude_error_next,-math.pi/2.0,math.pi/2.0)
                    pitch_plan["pitch_rad"]=pitch_next; pitch_plan["q_rate_rad_s"]=q_next
                    pitch_plan["attitude_error_rad"]=attitude_error_next
                    pitch_plan["p_rate_rad_s"]=p_next; pitch_plan["roll_attitude_error_rad"]=roll_error_next
                    pitch_diag={"pitch_stabilization_active":True,"pitch_rad":pitch_now,"pitch_rate_rad_s":q_now,
                        "target_pitch_rad":float(pitch_plan["target_pitch_rad"]),"surface_request_rad":applied_surface,
                        "vertical_attitude_error_rad":attitude_error_now,
                        "vertical_attitude_error_source":pitch_plan["attitude_error_source"],
                        "target_quaternion_wxyz":pitch_plan["target_quaternion_wxyz"],
                        "horizontal_local_guidance":pitch_plan["horizontal_local_guidance"],
                        "elevator_request_norm":elevator,"predicted_pitch_rad":pitch_next,"predicted_pitch_rate_rad_s":q_next,
                        "predicted_vertical_attitude_error_rad":attitude_error_next,
                        "roll_stabilization_active":True,"roll_rate_rad_s":p_now,
                        "vertical_roll_attitude_error_rad":roll_error_now,
                        "differential_surface_request_rad":applied_differential_surface,
                        "aileron_request_norm":aileron,"predicted_roll_rate_rad_s":p_next,
                        "predicted_vertical_roll_attitude_error_rad":roll_error_next,
                        "prediction_model":"source_propwash_pitch_slope_bounded_PD","state_source":pitch_plan["source"],
                        "gain_schedule":pitch_plan["gain_schedule"],"schedule_airspeed_m_s":pitch_plan["schedule_airspeed_m_s"],
                        "full_fidelity_lane":pitch_plan["full_fidelity_lane"],
                        "attitude_gain_rad_per_rad":pitch_plan["attitude_gain_rad_per_rad"],
                        "rate_gain_rad_per_rad_s":pitch_plan["rate_gain_rad_per_rad_s"],
                        "roll_attitude_gain_rad_per_rad":pitch_plan["roll_attitude_gain_rad_per_rad"],
                        "roll_rate_gain_rad_per_rad_s":pitch_plan["roll_rate_gain_rad_per_rad_s"],
                        "forecast_step_s":pitch_plan["forecast_step_s"],"horizon_steps":pitch_plan["horizon_steps"],
                        "transient_steps":pitch_plan["transient_steps"],
                        "terminal_reserve_steps":pitch_plan["terminal_reserve_steps"],
                        "settling_horizon_s":pitch_plan["settling_horizon_s"],
                        "natural_frequency_rad_s":pitch_plan["natural_frequency_rad_s"],
                        "prediction_pole":pitch_plan["prediction_pole"]}
                u=[float(aileron),float(elevator),float(throttle),0.0]
                d={"controller_regime":"vertical_takeoff_force_vertical_rate","base_collective_norm":takeoff_seed,
                   "climb_rate_up_ref_m_s":climb_up,"climb_rate_up_current_m_s":climb_now,
                   "climb_rate_error_m_s":climb_error,"vertical_rate_correction_norm":corr,"desired_motor_collective_norm":throttle,
                   "vertical_collective_synthesis":vertical_inverse,
                   "takeoff_tilt_aware_floor":takeoff_floor_diag,
                   **pitch_diag}
            elif phase=="hover":
                # Hover follows the Layer-4 vertical trajectory; it must not replace
                # a requested braking move with a permanently super-hover bias.  The
                # source motor-law inverse is identical to the takeoff inverse, but
                # hover permits a bounded projection because the commissioned mixer
                # range is deliberately narrower than the abstract qLPV action set.
                # Full-Fidelity still evaluates every projected horizon move.
                try:
                    raw_throttle,vertical_inverse=trajectory_vertical_collective(
                        cur,nxt,allow_bounded_projection=True)
                except (KeyError,TypeError,ValueError,ZeroDivisionError) as exc:
                    return [],{"reason":"hover_trajectory_collective_unavailable",
                               "phase":phase,"error":repr(exc),
                               "safety_semantics":"fail_closed_no_fixed_gain_substitution"}
                local_hold=(pitch_plan or {}).get("horizontal_local_guidance") or {}
                if local_hold.get("applied") is True:
                    try:
                        axis=[float(v) for v in local_hold["target_thrust_axis_ned"]]
                        vertical_fraction=-axis[2]
                        available=float(vertical_inverse["available_total_thrust_N"])
                        vertical_required=float(vertical_inverse["required_total_thrust_N"])
                        if (not all(math.isfinite(v) for v in axis+[vertical_fraction,available,vertical_required])
                                or vertical_fraction<=1e-9 or vertical_fraction>1.0+1e-12
                                or available<=0.0 or vertical_required<=0.0):
                            raise ValueError("local_hold_collective_geometry_invalid")
                        uncorrected=float(raw_throttle)
                        raw_throttle=math.sqrt((vertical_required/vertical_fraction)/available)
                        vertical_inverse={**vertical_inverse,"horizontal_hold_thrust_compensation":{
                            "used":True,"uncorrected_collective_norm":uncorrected,
                            "compensated_collective_norm":raw_throttle,
                            "vertical_thrust_fraction":vertical_fraction,
                            "target_thrust_axis_ned":axis,
                            "method":"source_motor_law_vector_thrust_inverse",
                            "manual_margin_used":False,
                            "full_fidelity_gate_remains_authoritative":True}}
                    except (KeyError,TypeError,ValueError,ZeroDivisionError) as exc:
                        return [],{"reason":"hover_local_hold_collective_unavailable",
                                   "phase":phase,"error":repr(exc),
                                   "safety_semantics":"fail_closed_no_uncompensated_tilt"}
                commissioned_hover=(self.runtime_phase_throttle_affine or {}).get("hover") or {}
                target_bounds=commissioned_hover.get("target_motor_bounds_norm")
                if isinstance(target_bounds,(list,tuple)) and len(target_bounds)==2:
                    commissioned_lo,commissioned_hi=[float(v) for v in target_bounds]
                else:
                    commissioned_lo,commissioned_hi=vmin,vmax
                # WingMPET's airborne hover gate requires every physical replay
                # substep to retain at least the source-derived static
                # force-balance collective.  The abstract Layer-4 trajectory can
                # legitimately ask for a stronger climb-braking acceleration, but
                # Layer5 must project that request into the intersection of the
                # verified physical envelope, commissioned mixer envelope and the
                # existing hover trust region before Full-Fidelity verification.
                # ``hover_equilibrium`` is derived from the Phoenix motor law
                # (sqrt(m*g / available_thrust)); no tuned numeric floor is added.
                hover_lo=max(vmin,hover_seed-h_corr_max,commissioned_lo,hover_equilibrium)
                hover_hi=min(vmax,hover_seed+h_corr_max,commissioned_hi)
                if (not all(math.isfinite(v) for v in (hover_lo,hover_hi,raw_throttle))
                        or hover_lo>hover_hi):
                    return [],{"reason":"hover_commissioned_collective_intersection_empty",
                               "phase":phase,"safety_semantics":"fail_closed_no_envelope_relaxation"}
                throttle=clip(raw_throttle,hover_lo,hover_hi)
                p_d_ref=float(traj[0][2]); alt_err=float(cur[2])-p_d_ref; vd_down=vd_now
                corr=float(throttle-hover_seed)
                aileron=0.0; elevator=0.0
                pitch_diag={"pitch_stabilization_active":False,
                    "pitch_stabilization_reason":"attitude_rate_state_unavailable",
                    "roll_stabilization_active":False}
                if pitch_plan is not None:
                    pitch_now=float(pitch_plan["pitch_rad"]); q_now=float(pitch_plan["q_rate_rad_s"])
                    p_now=float(pitch_plan["p_rate_rad_s"])
                    attitude_error_now=float(pitch_plan["attitude_error_rad"])
                    roll_error_now=float(pitch_plan["roll_attitude_error_rad"])
                    surface=clip(
                        float(pitch_plan["rate_gain_rad_per_rad_s"])*q_now
                        +float(pitch_plan["attitude_gain_rad_per_rad"])*attitude_error_now,
                        -float(pitch_plan["surface_limit_rad"]),float(pitch_plan["surface_limit_rad"]))
                    elevator=clip(surface/float(pitch_plan["symmetric_gain_rad_per_norm"]),-max_e,max_e)
                    differential_surface=clip(
                        -float(pitch_plan["roll_rate_gain_rad_per_rad_s"])*p_now
                        -float(pitch_plan["roll_attitude_gain_rad_per_rad"])*roll_error_now,
                        -float(pitch_plan["surface_limit_rad"]),float(pitch_plan["surface_limit_rad"]))
                    aileron=clip(differential_surface/float(pitch_plan["differential_gain_rad_per_norm"]),-max_a,max_a)
                    applied_surface=float(pitch_plan["symmetric_gain_rad_per_norm"])*elevator
                    applied_differential_surface=float(pitch_plan["differential_gain_rad_per_norm"])*aileron
                    dt_pitch=float(pitch_plan["prediction_step_s"])
                    q_next=q_now+dt_pitch*float(pitch_plan["pitch_effectiveness_rad_s2_per_rad"])*applied_surface
                    p_next=p_now+dt_pitch*float(pitch_plan["roll_effectiveness_rad_s2_per_rad"])*applied_differential_surface
                    attitude_error_next=self._wrap_pi(attitude_error_now+0.5*dt_pitch*(q_now+q_next))
                    roll_error_next=self._wrap_pi(roll_error_now+0.5*dt_pitch*(p_now+p_next))
                    pitch_next=clip(float(pitch_plan["target_pitch_rad"])+attitude_error_next,-math.pi/2.0,math.pi/2.0)
                    pitch_plan["pitch_rad"]=pitch_next; pitch_plan["q_rate_rad_s"]=q_next
                    pitch_plan["attitude_error_rad"]=attitude_error_next
                    pitch_plan["p_rate_rad_s"]=p_next; pitch_plan["roll_attitude_error_rad"]=roll_error_next
                    pitch_diag={"pitch_stabilization_active":True,"pitch_rad":pitch_now,"pitch_rate_rad_s":q_now,
                        "target_pitch_rad":float(pitch_plan["target_pitch_rad"]),"surface_request_rad":applied_surface,
                        "vertical_attitude_error_rad":attitude_error_now,
                        "vertical_attitude_error_source":pitch_plan["attitude_error_source"],
                        "target_quaternion_wxyz":pitch_plan["target_quaternion_wxyz"],
                        "horizontal_local_guidance":pitch_plan["horizontal_local_guidance"],
                        "elevator_request_norm":elevator,"predicted_pitch_rad":pitch_next,
                        "predicted_pitch_rate_rad_s":q_next,
                        "predicted_vertical_attitude_error_rad":attitude_error_next,
                        "roll_stabilization_active":True,"roll_rate_rad_s":p_now,
                        "vertical_roll_attitude_error_rad":roll_error_now,
                        "differential_surface_request_rad":applied_differential_surface,
                        "aileron_request_norm":aileron,"predicted_roll_rate_rad_s":p_next,
                        "predicted_vertical_roll_attitude_error_rad":roll_error_next,
                        "prediction_model":"source_propwash_pitch_slope_bounded_PD",
                        "state_source":pitch_plan["source"],"gain_schedule":pitch_plan["gain_schedule"],
                        "schedule_airspeed_m_s":pitch_plan["schedule_airspeed_m_s"],
                        "attitude_gain_rad_per_rad":pitch_plan["attitude_gain_rad_per_rad"],
                        "rate_gain_rad_per_rad_s":pitch_plan["rate_gain_rad_per_rad_s"],
                        "roll_attitude_gain_rad_per_rad":pitch_plan["roll_attitude_gain_rad_per_rad"],
                        "roll_rate_gain_rad_per_rad_s":pitch_plan["roll_rate_gain_rad_per_rad_s"],
                        "forecast_step_s":pitch_plan["forecast_step_s"],"horizon_steps":pitch_plan["horizon_steps"],
                        "transient_steps":pitch_plan["transient_steps"],
                        "terminal_reserve_steps":pitch_plan["terminal_reserve_steps"],
                        "settling_horizon_s":pitch_plan["settling_horizon_s"],
                        "natural_frequency_rad_s":pitch_plan["natural_frequency_rad_s"]}
                u=[float(aileron),float(elevator),float(throttle),0.0]
                d={"controller_regime":"hover_force_altitude_vertical_rate","base_collective_norm":hover_seed,
                   "hover_altitude_error_m":alt_err,"hover_down_velocity_m_s":vd_down,"hover_feedback_correction_norm":corr,
                   "desired_motor_collective_norm":throttle,
                   "vertical_collective_synthesis":vertical_inverse,
                   "raw_trajectory_collective_norm":raw_throttle,
                   "hover_collective_projection_applied":abs(throttle-raw_throttle)>1e-12,
                   "hover_collective_projection_bounds_norm":[hover_lo,hover_hi],
                   "hover_force_balance_floor_norm":hover_equilibrium,
                   "projection_basis":"source_motor_inverse_intersect_force_balance_commissioned_mixer_and_existing_hover_trust_region",
                   **pitch_diag}
            elif phase in {"transition_forward","transition_back"}:
                lam=lam_from_va(va)
                # Same blend law serves both directions; mission phase defines direction,
                # while airspeed determines the instantaneous aerodynamic authority.
                vertical_u=[0.0,0.0,float(hover_seed),0.0]
                u=[float((1.0-lam)*vertical_u[j]+lam*forward[j]) for j in range(4)]
                d={**fd,"controller_regime":"transition_blended_qLPV_reference","transition_direction":phase,
                   "lambda_wing_borne":lam,"vertical_collective_norm":hover_seed,"forward_throttle_norm":forward[2],
                   "throttle_request_norm":u[2]}
            elif phase=="vertical_landing":
                # Layer4 owns the terminal AGL corridor; Layer5 follows its descent-rate
                # trajectory and never substitutes cruise airspeed for landing thrust.
                desired_down=max(0.0,vd_ref)
                reduction=min(land_red_max,land_ff*desired_down)
                too_fast=max(0.0,vd_now-desired_down)
                boost=min(land_boost_max,land_fb*too_fast)
                throttle=clip(hover_seed-reduction+boost,vmin,vmax)
                u=[0.0,0.0,float(throttle),0.0]
                d={"controller_regime":"vertical_landing_descent_rate","base_collective_norm":hover_seed,
                   "desired_down_velocity_m_s":desired_down,"down_velocity_error_m_s":vd_now-desired_down,
                   "feedforward_reduction_norm":reduction,"braking_boost_norm":boost,"throttle_request_norm":throttle,
                   "terminal_agl_corridor_authority":"Layer4 verified landing corridor"}
            else:
                u=forward; d=fd
            if learned_u is not None:
                # The learned policy supplies the nominal first move.  For vertical
                # vertical takeoff and hover, retain the source-bound same-cycle
                # attitude correction
                # correction produced above; it corrects plant/twin mismatch and is
                # not part of the offline trajectory prior.
                policy_u=list(learned_u)
                if phase in {"vertical_takeoff","hover"}:
                    policy_u[0]=clip(float(policy_u[0])+float(u[0]),-max_a,max_a)
                    policy_u[1]=clip(float(policy_u[1])+float(u[1]),-max_e,max_e)
                u=policy_u
                d={**d,"controller_regime":"compiled_offline_policy_with_bounded_residual_tracker",
                   "compiled_policy":learned_diag,
                   "source_bound_residual_tracker":phase in {"vertical_takeoff","hover"}}
            else:
                d={**d,"compiled_policy":learned_diag,"limited_fallback_used":True,
                   "fallback_authority":"source-bound Phoenix nominal tracker; all downstream gates remain mandatory"}
            # Online twin/plant correction is synthesized from the previous
            # same-transaction vertical-velocity residual and a source-physics
            # input sensitivity.  It is attached by Layer4 *before* FF replay;
            # therefore no unverified post-certificate command can be emitted.
            residual_binding=candidate.get("twin_plant_residual_assimilation") or {}
            residuals=residual_binding.get("state_residual_evidence") or {}
            adaptive_diag={"used":False,"reason":"transaction_matched_residual_unavailable"}
            if i==0 and phase=="vertical_takeoff" and residual_binding.get("applied") is True and vertical_seed is not None:
                try:
                    residual_vd=float(residuals["velocity_d_m_s"])
                    confidence=clip(float(residual_binding["attribution_confidence"]),0.0,1.0)
                    pb=vertical_seed["physics_basis"]; terms=pb["takeoff_propwash_terms"]
                    mass=float(pb["mass_kg"]); kt=float(pb["motor_k_t_N_per_rad_s2"])
                    omega_max=float(self.omega_max); dt=float((operating_context or {})["execution_segment_s"])
                    throttle=max(float(u[2]),1e-9)
                    # total thrust = 2*kT*(omega_max*u)^2.  Positive thrust acts
                    # upward, hence d(v_down)/du is negative.  Cancelling an
                    # observed-minus-predicted residual e requires du=e/S, where
                    # S is the positive magnitude below.
                    sensitivity=4.0*kt*omega_max*omega_max*throttle*dt/mass
                    if not all(math.isfinite(v) for v in (residual_vd,confidence,sensitivity)) or sensitivity<=1e-12:
                        raise ValueError("physical_vertical_input_sensitivity_invalid")
                    raw_delta=confidence*residual_vd/sensitivity
                    # Residual assimilation is a first-move correction of the
                    # already-qualified trajectory inverse, not a second control
                    # law. Keep it inside the same source-derived commissioned
                    # braking envelope; Layer4 still verifies the corrected command
                    # with WingMPET before this path can become FINAL.
                    before=float(u[2])
                    nominal_delta=before-takeoff_seed
                    transient=((vertical_inverse or {}).get("commit_segment_motor_transient") or {})
                    commit_floor=max(takeoff_floor,float(transient.get("minimum_commit_collective_norm",takeoff_floor)))
                    correction_lo=max(-to_corr_max,-to_corr_max-nominal_delta,commit_floor-before)
                    correction_hi=min(to_corr_max,to_corr_max-nominal_delta)
                    bounded_delta=clip(raw_delta,correction_lo,correction_hi)
                    u[2]=clip(before+bounded_delta,commit_floor,vmax)
                    adaptive_diag={"used":True,"reason":"source_physics_residual_first_move",
                        "source_transaction_id":residual_binding.get("source_transaction_id"),
                        "route_hash":residual_binding.get("route_hash"),
                        "velocity_d_residual_m_s":residual_vd,"attribution_confidence":confidence,
                        "input_sensitivity_m_s_per_motor_norm":sensitivity,
                        "raw_motor_delta_norm":raw_delta,"bounded_motor_delta_norm":bounded_delta,
                        "feedback_trust_region_norm":to_corr_max,
                        "source_braking_collective_floor_norm":takeoff_floor,
                        "commit_segment_dynamic_floor_norm":commit_floor,
                        "hover_equilibrium_collective_norm":hover_equilibrium,
                        "source_braking_envelope_preserved":True,
                        "nominal_tracker_delta_norm":nominal_delta,
                        "residual_correction_budget_norm":[correction_lo,correction_hi],
                        "trust_region_saturated":abs(raw_delta-bounded_delta)>1e-12,
                        "applied_motor_delta_norm":float(u[2])-before,
                        "verification_authority":"Layer4/WingMPET Full-Fidelity after this correction",
                        "direct_pwm":False,"direct_jsbsim_actuation":False}
                except (KeyError,TypeError,ValueError,ZeroDivisionError) as exc:
                    adaptive_diag={"used":False,"reason":"residual_first_move_contract_invalid","error":repr(exc)}
            elif i>0 and phase=="vertical_takeoff" and residual_binding.get("applied") is True:
                adaptive_diag={"used":False,"reason":"residual_correction_is_first_move_only",
                    "source_transaction_id":residual_binding.get("source_transaction_id"),
                    "route_hash":residual_binding.get("route_hash")}
            d={**d,"adaptive_first_move":adaptive_diag}
            if phase in {"vertical_takeoff","hover"}:
                desired_motor=float(u[2])
                try:
                    pilot_request,mapping_source=phase_motor_to_pilot_request(phase,desired_motor)
                except (KeyError,TypeError,ValueError,IndexError) as exc:
                    return [],{"reason":"phase_motor_to_pilot_inverse_unavailable","phase":phase,"error":repr(exc)}
                u[2]=float(pilot_request)
                d={**d,"desired_motor_collective_norm":desired_motor,
                   "throttle_request_norm":float(pilot_request),
                   "throttle_coordinate_mapping":mapping_source}
            seq.append(u); diagnostics.append({"step":i,"phase":phase,**d})
        regime=(diagnostics[0].get("controller_regime") if diagnostics else "unknown")
        return seq,{"trim":{"elevator_norm":e0,"throttle_norm":t0},"phase":phase,"controller_regime":regime,
                   "gains":{"vertical_takeoff_climb_rate_gain_per_m_s":k_to_v,"hover_altitude_error_gain_per_m":k_h_alt,
                            "hover_down_velocity_gain_per_m_s":k_h_vd,"vertical_landing_descent_feedforward_gain_per_m_s":land_ff,
                            "vertical_landing_descent_error_gain_per_m_s":land_fb,
                            "vertical_pitch_attitude_gain_rad_per_rad":(diagnostics[0].get("attitude_gain_rad_per_rad") if diagnostics else None),
                            "vertical_pitch_rate_gain_rad_per_rad_s":(diagnostics[0].get("rate_gain_rad_per_rad_s") if diagnostics else None),
                            "vertical_pitch_gain_source":"source_dynamics_plus_L4_timing_contract"},
                   "limits":{"aileron":max_a,"elevator":max_e,"vertical_throttle":[vmin,vmax],
                             "vertical_takeoff_force_balance_floor_norm":takeoff_floor,
                             "hover_force_balance_floor_norm":hover_equilibrium,
                             "vertical_takeoff_max_feedback_correction_norm":to_corr_max,"hover_max_feedback_correction_norm":h_corr_max,
                             "vertical_landing_max_collective_reduction_norm":land_red_max,"vertical_landing_max_braking_boost_norm":land_boost_max},
                   "steps":diagnostics[:3],"horizon":len(seq),"vertical_seed":vertical_seed,
                   "compiled_policy_used":bool(diagnostics and diagnostics[0].get("compiled_policy",{}).get("used")),
                   "adaptive_first_move_used":bool(diagnostics and diagnostics[0].get("adaptive_first_move",{}).get("used")),
                   "knowledge_policy_sha256":((candidate.get("compiled_online_policy") or {}).get("artifact_hash")),
                   "safety_semantics":"compiled evidence is an offline prior only; Layer4 FF + Layer5 FINAL + Layer6 executor + ArduPilot mixer remain mandatory"}

    def _logical_bounds(self, seq:list[list[float]]) -> bool: return all(len(u)==4 and -1<=u[0]<=1 and -1<=u[1]<=1 and 0<=u[2]<=1 and -1<=u[3]<=1 for u in seq)

    def schedule(self, state:dict[str,Any]|None=None, context:dict[str,Any]|None=None, guidance:dict[str,Any]|None=None, network:dict[str,Any]|None=None)->dict[str,Any]:
        state=state or {}; va=float(state.get("airspeed_m_s",state.get("Va",self.data.get("speed",{}).get("cruise_speed_m_s",13.0))))
        rho=float(state.get("air_density_kg_m3",state.get("rho",1.225))); qbar=0.5*rho*va*va
        if self.full_envelope:
            v0=float(self.config.get("phoenix.transition.v_hover_max",3.0)); v1=float(self.config.get("phoenix.transition.v_forward_min",9.0)); lam=clip((va-v0)/max(v1-v0,1e-9)); mode="TRANSITION" if 0<lam<1 else ("HOVER" if lam<=0 else "FORWARD")
        else:
            lam=1.0; mode="FORWARD"; # full-envelope vertices remain unavailable by evidence boundary
        vec=[va,qbar]
        for k in ("vax","vay","vaz","p","q","r","omega_L","omega_R","delta_L","delta_R","SOC"):
            vec.append(float(state.get(k,0.0 if k!="SOC" else 1.0)))
        vec.append(lam)
        return {"mode":mode,"lambda_transition":lam,"vector":vec,"full_envelope_authorized":self.full_envelope,"transition_gate":self.transition_validated,
                "context_digest_fields":sorted((context or {}).keys()),"guidance_present":bool(guidance),"network_read_only":network is not None}

    def predict_motor_step(self, omega:float, omega_cmd:float, dt:float, rotor:str="L")->dict[str,float]:
        target=clip(float(omega_cmd),0.0,self.omega_max); tau=self.tau_up if target>=omega else self.tau_down; alpha=clip(dt/max(tau,1e-9)); w=clip(omega+alpha*(target-omega),0.0,self.omega_max); thrust=min(self.k_t*w*w,self.thrust_max)
        sigma=-1.0 if str(rotor).upper().startswith("L") else 1.0
        return {"omega_rad_s":w,"thrust_N":thrust,"reaction_torque_Nm":sigma*self.k_q*thrust,"tau_s":tau}

    def predict_servo_step(self, delta:float, delta_cmd:float, dt:float, index:int=0)->dict[str,Any]:
        if not self.servo_identified or len(self.servo_tau)<2 or len(self.servo_rate)<2: return {"ok":False,"reason":"servo_dynamics_not_identified"}
        tau=max(self.servo_tau[index],1e-6); rate=max(self.servo_rate[index],1e-6); raw=(float(delta_cmd)-float(delta))/tau; d_dot=max(-rate,min(rate,raw)); nxt=max(-self.elevon_max,min(self.elevon_max,float(delta)+dt*d_dot))
        return {"ok":True,"delta_rad":nxt,"rate_rad_s":d_dot,"tau_s":tau,"rate_limit_rad_s":rate}

    def to_wingmpet_surface_coordinates(self,left_elevon_rad:float,right_elevon_rad:float)->dict[str,Any]:
        """Map commissioned mixer outputs to WingMPET aerodynamic coordinates.

        OFFLINE model certificates already use WingMPET coordinates. ONLINE
        certificates must carry the explicit coupled-plant convention; absence of
        that evidence is fail-closed rather than an assumed identity map.
        """
        vals=np.asarray([left_elevon_rad,right_elevon_rad],dtype=float)
        if vals.shape!=(2,) or not np.isfinite(vals).all():
            return {"ok":False,"reason":"surface_coordinates_non_finite"}
        kind=str((self.runtime_certificate or {}).get("kind") or "")
        if kind=="OFFLINE_MODEL":
            mapped=vals; source="offline_model_already_in_wingmpet_coordinates"
        elif kind=="ONLINE_SITL" and self.runtime_surface_contract:
            mapped=np.asarray([-vals[1],-vals[0]],dtype=float)
            source="versioned_JSBSim_to_WingMPET_surface_contract"
        else:
            return {"ok":False,"reason":"online_plant_surface_contract_missing"}
        return {"ok":True,"physical":[float(mapped[0]),float(mapped[1])],
                "source":source,"contract":dict(self.runtime_surface_contract),
                "direct_pwm":False,"ardupilot_mixer_authoritative":True}

    def mix_physical(self,logical:list[float], *, phase:str|None=None)->dict[str,Any]:
        if len(logical)!=4 or not self._logical_bounds([logical]): return {"ok":False,"reason":"logical_command_bounds"}
        a,e,t,r=[float(x) for x in logical]
        if not self.mixer_validated:
            return {"ok":False,"reason":"ardupilot_mixer_not_locally_validated","delegated_to":"ArduPlane","direct_pwm":False,"logical":list(logical)}
        M=np.asarray(self.runtime_mixer_matrix_rad if self.runtime_mixer_matrix_rad is not None else self.config.get("phoenix.mixer.elevon_matrix"),float)
        b=np.asarray(self.runtime_mixer_trim_rad if self.runtime_mixer_trim_rad is not None else self.config.get("phoenix.mixer.trim_rad",[0.0,0.0]),float)
        phase_name=str(phase or "").strip().lower()
        if phase_name and phase_name in self.runtime_phase_surface_trim_rad:
            b=np.asarray(self.runtime_phase_surface_trim_rad[phase_name],dtype=float)
        if M.shape!=(2,2) or b.shape!=(2,): return {"ok":False,"reason":"mixer_matrix_invalid"}
        u=np.asarray([a,e],dtype=float)
        u0=np.asarray(self.runtime_mixer_operating_input_norm if self.runtime_mixer_matrix_rad is not None else [0.0,0.0],dtype=float)
        elev=np.clip(M@(u-u0)+b,-self.elevon_max,self.elevon_max)
        g=float(self.config.get("phoenix.differential_thrust.gain",self.rudd_dt_gain if self.dt_validated else 0.0)) if self.dt_validated else 0.0
        ta=self.runtime_throttle_affine or {}
        if all(k in ta for k in ("operating_request_norm","operating_left_motor_norm","operating_right_motor_norm",
                                 "left_gain_motor_norm_per_request_norm","right_gain_motor_norm_per_request_norm")):
            dt=float(t)-float(ta["operating_request_norm"])
            base=np.asarray([float(ta["operating_left_motor_norm"])+float(ta["left_gain_motor_norm_per_request_norm"])*dt,
                             float(ta["operating_right_motor_norm"])+float(ta["right_gain_motor_norm_per_request_norm"])*dt],dtype=float)
            throttle_mapping="commissioned_local_affine_ArduPilot_output"
        else:
            base=np.asarray([t,t],dtype=float)
            throttle_mapping="legacy_identity_request_to_model_command"
        motors=np.clip(base+np.asarray([-g*r,g*r]),0.0,1.0)
        return {"ok":True,"physical":[float(elev[0]),float(elev[1]),float(motors[0]),float(motors[1])],"actuators":self.PHYSICAL_ACTUATORS,"direct_pwm":False,
                "differential_thrust_gain":g,"surface_affine_operating_input_norm":[float(x) for x in u0],
                "throttle_mapping":throttle_mapping,"phase":phase_name or None,
                "surface_trim_source":"phase_specific_offline_certificate" if phase_name in self.runtime_phase_surface_trim_rad else "global_mixer_trim"}

    def physical_feasibility(self, logical:list[float], correction_requested:bool=False, state:dict[str,Any]|None=None, dt:float|None=None)->dict[str,Any]:
        reasons=[]
        if len(logical)!=4: reasons.append("logical_command_dimension")
        elif not self._logical_bounds([logical]): reasons.append("logical_command_bounds")
        if correction_requested and not self.mixer_validated: reasons.append("mixer_not_validated_for_qlpv_correction")
        if correction_requested and not self.servo_identified: reasons.append("servo_dynamics_not_identified_for_qlpv_correction")
        if correction_requested and not self.esc_mapping_identified: reasons.append("esc_mapping_not_identified_for_qlpv_correction")
        if self.full_envelope and not self.transition_validated: reasons.append("transition_not_validated")
        details={"ok":not reasons,"reasons":reasons,"direct_pwm":False,"differential_thrust_direct_yaw":False if self.rudd_dt_gain==0 else self.dt_validated,
                 "physical_actuator_tube":{"elevon_rad":[-self.elevon_max,self.elevon_max],"motor_norm":[0.0,1.0],"motor_speed_rad_s":[0.0,self.omega_max],"servo_rate_enforced":self.servo_identified},
                 "transition_gate":self.transition_validated,"full_envelope":self.full_envelope}
        if state is not None: details["scheduling"]=self.schedule(state)
        return details

    def physical_trajectory_feasibility(self, sequence:list[list[float]], correction_requested:bool=False, states:list[dict[str,Any]]|None=None)->dict[str,Any]:
        reasons=[]; steps=[]; dt=float(self.config.get("qlpv.dt_s",0.1))
        # For a non-zero qLPV correction the verification tube is extended to physical
        # elevon/motor states.  Nominal zero-correction commands remain delegated to
        # ArduPlane and do not fabricate a local mixer model.
        dyn={"delta_L":0.0,"delta_R":0.0,"omega_L":0.0,"omega_R":0.0}
        if states and isinstance(states[0],dict):
            for k in dyn:
                try: dyn[k]=float(states[0].get(k,dyn[k]))
                except Exception: pass
        for i,u in enumerate(sequence):
            st=(states[i] if states and i<len(states) and isinstance(states[i],dict) else None)
            r=self.physical_feasibility(u,correction_requested,state=st,dt=dt); step={"logical":list(u),"logical_gate":r}
            reasons.extend(r["reasons"])
            if correction_requested and not r["reasons"]:
                mix=self.mix_physical(u); step["mixer"]=mix
                if not mix.get("ok"):
                    reasons.append(mix.get("reason","mixer_failed"))
                else:
                    dl,dr,ml,mr=[float(x) for x in mix["physical"]]
                    sl=self.predict_servo_step(dyn["delta_L"],dl,dt,0); sr=self.predict_servo_step(dyn["delta_R"],dr,dt,1)
                    if not sl.get("ok") or not sr.get("ok"):
                        reasons.append("servo_physical_tube_unavailable")
                    else:
                        dyn["delta_L"]=sl["delta_rad"]; dyn["delta_R"]=sr["delta_rad"]
                    motL=self.predict_motor_step(dyn["omega_L"],ml*self.omega_max,dt,"L"); motR=self.predict_motor_step(dyn["omega_R"],mr*self.omega_max,dt,"R")
                    dyn["omega_L"]=motL["omega_rad_s"]; dyn["omega_R"]=motR["omega_rad_s"]
                    step["servo"]={"L":sl,"R":sr}; step["motor"]={"L":motL,"R":motR}; step["physical_state_next"]=dict(dyn)
                    if abs(dyn["delta_L"])>self.elevon_max+1e-12 or abs(dyn["delta_R"])>self.elevon_max+1e-12: reasons.append("elevon_dynamic_tube_violation")
                    if dyn["omega_L"]>self.omega_max+1e-12 or dyn["omega_R"]>self.omega_max+1e-12: reasons.append("motor_dynamic_tube_violation")
            steps.append(step)
        return {"ok":not reasons,"reasons":sorted(set(reasons)),"steps":steps,"actuators":self.PHYSICAL_ACTUATORS,"verification_preserving_physical_tube":True,
                "motor_dynamics_used":bool(correction_requested),"servo_dynamics_used":bool(correction_requested),"dt_s":dt,"final_physical_state":dyn if correction_requested else None}
