from __future__ import annotations
import copy
import sys
import importlib.util
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any
from ..storage import CompressedPersistentWorkspace

class Layer4Adapter:
    layer_id=4
    def __init__(self,config,layer3_adapter=None,layer5_adapter=None):
        self.config=config; self.root=config.path_value("TDDT_LAYER4_ROOT"); self.engine=None; self.layer3_adapter=layer3_adapter; self.layer5_adapter=layer5_adapter
        self.native_root=(config.runtime_root/"layer4"/"native").resolve()
        self.native_workspace=CompressedPersistentWorkspace(
            self.native_root,prefix="tdoc-layer4-",working_parent=self.config.runtime_root/".transient")
        self.native_execution_source=None

    def bind_native_execution_source(self,source):
        """TDOC binds a Python candidate/provider source; request JSON cannot."""
        if source is not None and not callable(source):raise TypeError('Python Native candidate source required')
        self.native_execution_source=source

    _FF_OUTCOME_SCHEMA="Layer4-FF-verification-outcome-v1"
    _FF_OUTCOME_STATES={"NOT_ATTEMPTED","ATTEMPTED_AND_REJECTED","ATTEMPTED_AND_VERIFIED","EXCEPTION"}

    @staticmethod
    def _ff_error_id(candidate_id:str,reason:str,error:str)->str:
        return "fferr-"+hashlib.sha256(f"{candidate_id}|{reason}|{error}".encode("utf-8",errors="replace")).hexdigest()[:16]

    @classmethod
    def _ff_outcome(cls,status:str,reason:str,*,reason_codes=None,verified=False,attempted=False,executed=False,**extra):
        if status not in cls._FF_OUTCOME_STATES:
            raise ValueError(f"invalid FF audit status: {status}")
        codes=[]
        for item in list(reason_codes or [reason]):
            text=str(item)
            if text and text not in codes: codes.append(text)
        out={
            "schema":cls._FF_OUTCOME_SCHEMA,
            "outcome_status":status,
            "verified":bool(verified),
            "candidate_verified":bool(verified),
            "attempted":bool(attempted),
            "executed":bool(executed),
            "reason":str(reason),
            "reason_codes":codes,
            "verification_reasons":list(codes),
        }
        out.update(extra)
        return out

    _TRANSITION_FF_PHASES={"transition_forward","transition_back"}

    @staticmethod
    def _audit_sha256(obj:Any)->str:
        payload=json.dumps(obj,sort_keys=True,separators=(",",":"),default=str,allow_nan=False).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @classmethod
    def _transition_ff_evidence(cls,phase:str,*,bridge=None,rows=None,outcome_status:str,reason_codes=None,certificate=None)->dict[str,Any]:
        """Build a compact, reproducible audit chain for transition Full-Fidelity evidence.

        This is evidence only; it never upgrades a verification result.  Current FF
        attempts must prove bridge readiness, exact physical-action consumption,
        WingMPET/op27 provenance and workspace identity.  Certificate reuse must carry
        the original physical issuance evidence.
        """
        phase=str(phase or "")
        required=phase in cls._TRANSITION_FF_PHASES
        bridge=dict(bridge or {})
        rows=list(rows or [])
        cert=dict(certificate or {})
        actions=list(bridge.get("physical_actions") or [])
        workspace=[]
        for r in rows:
            wid=dict(r.get("verification_workspace_identity") or {})
            if wid and wid not in workspace: workspace.append(wid)
        sources=sorted({str((r.get("physical_replay") or {}).get("source") or "") for r in rows if isinstance(r,dict)})
        consumed=sum(1 for r in rows if bool(r.get("physical_action_consumed")))
        replay_digests=[str(r.get("physical_action_sha256") or "") for r in rows if r.get("physical_action_sha256")]
        cert_phys=dict(cert.get("physical_issuance_evidence") or {})
        current_attempt=outcome_status in {"ATTEMPTED_AND_REJECTED","ATTEMPTED_AND_VERIFIED","EXCEPTION"} and bool(rows)
        certificate_reuse=bool(cert)
        if certificate_reuse:
            chain_ok=bool(
                cert.get("verified")
                and cert.get("certificate_id")
                and cert_phys.get("bridge_ready")
                and cert_phys.get("physical_actions_consumed")
                and cert_phys.get("verdict_authoritative")
                and cert_phys.get("verdict_non_bypassable")
                and cert_phys.get("all_rows_vtol_feasible")
                and cert_phys.get("workspace_count",0)>0
                and cert_phys.get("physical_action_count",0)>0
            )
        elif current_attempt:
            chain_ok=bool(
                bridge.get("ok")
                and actions
                and consumed==len(rows)
                and len(rows)>0
                and len(workspace)>0
                and all(src.startswith("WingMPET/op27") for src in sources)
                and all(bool(r.get("physical_action_sha256")) for r in rows)
            )
        elif outcome_status=="NOT_ATTEMPTED":
            # Audit completeness means the reason why FF was not attempted is explicit;
            # it does not imply bridge readiness or physical validation.
            chain_ok=bool(reason_codes)
        else:
            chain_ok=False
        return {
            "schema":"TDOC-Transition-FF-evidence-v1",
            "required":required,
            "phase":phase or None,
            "outcome_status":outcome_status,
            "audit_complete":bool((not required) or chain_ok),
            "bridge_ready":bool(bridge.get("ok")) if bridge else bool(cert_phys.get("bridge_ready")),
            "bridge_reason":bridge.get("reason") if bridge else cert_phys.get("bridge_reason"),
            "bridge_owner":bridge.get("owner") if bridge else cert_phys.get("bridge_owner"),
            "bridge_binding_source":bridge.get("binding_source") if bridge else cert_phys.get("bridge_binding_source"),
            "bridge_sha256":cls._audit_sha256(bridge) if bridge else cert_phys.get("bridge_sha256"),
            "physical_action_count":len(actions) if actions else int(cert_phys.get("physical_action_count") or 0),
            "physical_actions_sha256":cls._audit_sha256(actions) if actions else cert_phys.get("physical_actions_sha256"),
            "replay_row_count":len(rows),
            "physical_action_consumed_count":consumed,
            "physical_actions_consumed_all":bool(rows) and consumed==len(rows) if rows else bool(cert_phys.get("physical_actions_consumed")),
            "physical_action_step_sha256":replay_digests,
            "replay_sources":sources or list(cert_phys.get("verdict_sources") or []),
            "workspace_count":len(workspace) if workspace else int(cert_phys.get("workspace_count") or 0),
            "workspace_identities":workspace,
            "workspace_identity_sha256":cls._audit_sha256(workspace) if workspace else cert_phys.get("workspace_identity_sha256"),
            "certificate_reuse":certificate_reuse,
            "certificate_id":cert.get("certificate_id") if cert else None,
            "reason_codes":list(reason_codes or []),
            "simulation_only":True,
            "operational_actuation":False,
        }

    def preflight(self):
        req=["src/layer4/__init__.py","configs/configuration_boulder.yaml"]
        miss=req if self.root is None else [x for x in req if not (self.root/x).exists()]
        modules={name: bool(importlib.util.find_spec(name)) for name in ("numpy","yaml")}
        missing_modules=[name for name,ok in modules.items() if not ok]
        return {"ok":bool(self.root and self.root.is_dir() and not miss and not missing_modules),"root":str(self.root) if self.root else None,"missing":miss,"python_modules":modules,"missing_python_modules":missing_modules,
                "full_fidelity_owner":"TDOC/RuntimeScheduler","internal_layer4_reverse_invocation":False}
    def _ensure(self):
        if self.engine is not None:return
        if not self.preflight()["ok"]: raise RuntimeError("Layer4 preflight failed")
        if str(self.root/"src") not in sys.path: sys.path.insert(0,str(self.root/"src"))
        from layer4 import Layer4Config, Layer4Engine
        cfg=Layer4Config.from_yaml(self.root/str(self.config.get("layers.layer4.config","configs/configuration_boulder.yaml")))
        if (self.config.get('layers.layer6.online.transport')=='Layer6Client_GUI_MAVLink'
                and self.config.get('layers.layer6.online.execution_profile')=='closed_loop_sitl'):
            from layer4.native_controller_contract import ModelPredictionScope
            cfg.model_prediction_scope=ModelPredictionScope()
        rr=self.native_workspace.working_root; rr.mkdir(parents=True,exist_ok=True)
        cfg.data.setdefault("registry",{})["path"]=str(rr/"candidates.jsonl"); cfg.data["registry"]["provenance_path"]=str(rr/"provenance.jsonl")
        # Disable Layer4's internal L4->L3->L2 verifier. TDOC performs the same verification gate externally.
        cfg.data.setdefault("verification",{})["enabled"]=False; cfg.data["verification"]["mode"]="tdoc_owned_full_fidelity_transaction"
        self.engine=Layer4Engine(cfg)
        from ..model_compatibility import integrated_native,capture_model_dependencies
        if integrated_native(self.config):
            self.engine.mf.model_dependencies_provider=lambda:capture_model_dependencies(self.config)
        self.native_workspace.sync()

    def verify_native_reference(self,request,service)->dict[str,Any]:
        """TDOC explicitly binds a provider; JSON cannot select this dependency.

        The separate reference schema never enters _tdoc_full_fidelity or the
        operational verified-candidate/FINAL assembly path.
        """
        self._ensure()
        previous=self.engine.verifier.native_reference_service
        self.engine.verifier.native_reference_service=service
        try:
            from layer4.native_provider_contract import REQUEST_SCHEMA
            if isinstance(request,dict) and request.get('schema')==REQUEST_SCHEMA:
                return self.engine.run(request).to_dict()
            return self.engine.verify_native_reference(request).to_dict()
        finally:
            self.engine.verifier.native_reference_service=previous

    def verify_native_execution(self,request,service)->dict[str,Any]:
        """Bind a trusted Python service for bounded future prediction only."""
        self._ensure()
        previous=self.engine.native_execution_service
        self.engine.native_execution_service=service
        try:return self.engine.run(request).to_dict()
        finally:self.engine.native_execution_service=previous

    def prepare_critical_path(self)->dict[str,Any]:
        """Build immutable runtime structures before the first cycle clock starts."""
        t0=time.monotonic(); self._ensure(); entries=len(self.engine.mf._load())
        return {"ok":True,"certificate_bank_entries":entries,
                "positive_index_keys":len(self.engine.mf._positive_index or {}),
                "negative_index_keys":len(self.engine.mf._negative_index or {}),
                "elapsed_s":time.monotonic()-t0,"outside_cycle_deadline":True}

    def _verification_template(self)->dict[str,Any]:
        rel=str(self.engine.config.get("verification.request_template","examples/layer3_runtime_request_template.json"))
        p=Path(self.engine.config.package_root)/rel
        if not p.is_file():
            raise RuntimeError(f"Layer4 verification request template missing: {p}")
        return json.loads(p.read_text(encoding="utf-8"))


    @staticmethod
    def _wingmpet_physical_verdict(rows:list[dict[str,Any]])->dict[str,Any]:
        """Authoritative non-bypassable verdict for WingMPET op27 replay.

        Mapping certificates authorize a command to be replayed; they never authorize
        physical feasibility.  Every replay row, final op27 output and recorded substep
        must report VTOL-feasible, and the physical action must actually be consumed.
        """
        details=[]
        from ..native_ground_policy import retired_fixture_replay
        for r in rows or []:
            pr=r.get("physical_replay") or {}
            out=pr.get("outputs") or {}
            trace=pr.get("substep_trace") or []
            src=str(pr.get("source") or "")
            retired_fixture=retired_fixture_replay(pr)
            trace_ok=bool(trace) and all(bool(step.get("vtol_feasible")) for step in trace)
            try:
                output_ok=float(out.get("vtol_feasible",0.0))>=0.5
            except Exception:
                output_ok=False
            row_ok=bool(
                r.get("physical_mission_safe")
                and r.get("physical_action_consumed")
                and pr.get("action_consumed")
                and pr.get("vtol_feasible")
                and output_ok
                and trace_ok
                and not retired_fixture
                and (src.startswith("WingMPET/op27") or src.startswith("WingMPET/op35-ground-supported"))
            )
            details.append({
                "ok":row_ok,
                "source":src,
                "action_consumed":bool(pr.get("action_consumed")),
                "replay_vtol_feasible":bool(pr.get("vtol_feasible")),
                "output_vtol_feasible":bool(output_ok),
                "substeps_all_vtol_feasible":bool(trace_ok),
                "substep_count":len(trace),
                "retired_launch_fixture_present":retired_fixture,
            })
        return {
            "authoritative":True,
            "non_bypassable":True,
            "override_allowed":False,
            "all_rows_vtol_feasible":bool(details) and all(x["ok"] for x in details),
            "rows":details,
        }

    @staticmethod
    def _merge_verification_request(template:dict[str,Any], live_request:dict[str,Any])->dict[str,Any]:
        """Overlay the authoritative same-cycle TDOC request onto Layer3 FF defaults.

        The template supplies only fields absent from the live request.  Runtime state,
        climate, mission target, OFFLINE plant state and provenance must come from the
        same atomic cycle being verified; otherwise FF silently verifies a different
        initial condition.
        """
        import copy
        def merge(base, overlay):
            out=copy.deepcopy(base) if isinstance(base,dict) else {}
            for k,v in (overlay or {}).items():
                if isinstance(v,dict) and isinstance(out.get(k),dict): out[k]=merge(out[k],v)
                else: out[k]=copy.deepcopy(v)
            return out
        out=merge(template,live_request or {})
        out.setdefault("context",{})["tdoc_full_fidelity_request_source"]="same_cycle_live_request_over_verification_template"
        return out

    # Problem #7 (v0910): full-fidelity stage coverage must follow the mission
    # taxonomy, not the old forward-flight-only gate.  The value is the canonical
    # SARG *reference/prior* stage; realized Twin state and the runtime mission
    # phase remain the original requested phase.  These aliases mirror Layer3's
    # KnowledgeResolver.MISSION_STAGE_ALIASES.
    _FF_STAGE_MAP={
        "vertical_takeoff":"takeoff_transition",
        "hover":"takeoff_transition",
        "transition_forward":"takeoff_transition",
        "climb":"climb",
        "cruise":"cruise",
        "turn":"turn",
        "approach":"approach",
        "transition_back":"approach",
        "vertical_landing":"approach",
    }
    _FF_PASSIVE_BOUNDARY_PHASES={"on_ground","touchdown"}

    @classmethod
    def _full_fidelity_stage_contract(cls,request:dict[str,Any])->dict[str,Any]:
        """Return the SARG-compatible FF stage for the current mission phase.

        Controlled VTOL mission phases are eligible for real full-fidelity replay.
        SARG is reference/prior only, so the returned ``sarg_stage`` selects the
        nearest canonical prior while ``requested_stage`` remains authoritative for
        Twin/runtime mode semantics.  Passive boundary phases remain fail-closed
        here; their special readiness semantics are handled separately (problem #10).
        """
        stage=str((request or {}).get("stage") or "").strip()
        mapped=cls._FF_STAGE_MAP.get(stage)
        passive=stage in cls._FF_PASSIVE_BOUNDARY_PHASES
        return {
            "applicable": mapped is not None,
            "requested_stage": stage or None,
            "sarg_stage": mapped,
            "supported_requested_stages": sorted(cls._FF_STAGE_MAP),
            "passive_boundary_phase": passive,
            "reason": (
                "vtol_or_forward_phase_supported" if mapped is not None
                else "passive_boundary_phase_deferred" if passive
                else "full_fidelity_not_applicable_phase"
            ),
        }

    def _ff_timing_contract(self, request:dict[str,Any], horizon_steps:int)->dict[str,Any]:
        online=str((request or {}).get("learning_mode") or "").upper()=="ONLINE_JSBSIM"
        forecast=float(self.engine.config.dt)
        segment=(float(self.config.get("tdoc.rtmo.l4_artifacts.commit_segment_s",0.1))
                 if online else forecast)
        executor_period=(float(self.config.get("tdoc.rtmo.realtime.max_control_period_s",0.1))
                         if online else forecast)
        if online and abs(segment-executor_period)>1e-12:
            raise RuntimeError(
                "l4_commit_segment_executor_period_mismatch:"
                f"segment={segment}:executor={executor_period}")
        return {"schema":"TDOC-L4-FF-multirate-v3","forecast_step_s":forecast,
                "execution_segment_s":segment,"executor_period_s":executor_period,
                "plant_transaction_steps":1,"horizon_steps":int(horizon_steps),
                "commit_lane_steps":1,"forecast_lane_steps":int(horizon_steps),
                "commit_lane_all_gates":True,"forecast_lane_all_gates":True,
                "first_move_only_committable":True,
                "forecast_policy_replanned_each_step":online}

    @staticmethod
    def _ff_certificate_timing_matches(cert:dict[str,Any], expected:dict[str,Any])->bool:
        actual=dict((cert or {}).get("timing_contract") or {})
        return bool(actual==expected)

    @staticmethod
    def _phoenix_hover_operating_context(snapshot_dict:dict[str,Any])->dict[str,Any]:
        """Extract same-cycle hover environment/propulsion context without simulator-truth promotion."""
        reps=(((snapshot_dict.get("std") or {}).get("representations") or {}) if isinstance(snapshot_dict,dict) else {})
        climate=((reps.get("MicroMetTwin") or {}).get("canonical_state") or {}) if isinstance(reps,dict) else {}
        ctx=(((snapshot_dict.get("std") or {}).get("context") or {}) if isinstance(snapshot_dict,dict) else {})
        ops=(ctx.get("online_execution_state") or ctx.get("online_physical_state")
             or ctx.get("offline_plant_state") or ctx.get("offline_physical_state") or {}) if isinstance(ctx,dict) else {}
        def _scalar(v,default):
            if isinstance(v,dict): v=v.get("value",default)
            try: return float(v)
            except Exception: return float(default)
        out = {
            "air_density_kg_m3":_scalar(climate.get("air_density_kg_m3",1.225),1.225),
            "battery_soc":_scalar(ops.get("soc",1.0),1.0),
            "airspeed_m_s":_scalar(ops.get("airspeed_m_s",0.0),0.0),
            "altitude_agl_m":_scalar(ctx.get("altitude_agl_m",0.0),0.0),
            "environment_fingerprint":ctx.get("effective_climate_fingerprint"),
            "source":"same_cycle_L3_STD+physical_replay_initial_state",
            "simulation_only":True,"not_field_evidence":True,
        }
        # latest_review/MESA local-guidance contract: vertical flight must not
        # reduce navigation to a fixed tailsitter attitude.  Carry the current
        # MESA state and the TDOC mission-phase reference to Layer5 so it can
        # synthesize a bounded position/velocity hold.  This is navigation-plane
        # evidence only: it contains no actuator/PWM command and never promotes
        # JSBSim truth into MESA.
        mesa=((reps.get("MESA_NAV") or {}).get("canonical_state") or {}) if isinstance(reps,dict) else {}
        phase=str(ctx.get("stage") or ctx.get("flight_phase") or "").strip().lower()
        phase_targets=ctx.get("online_phase_reference_targets") or {}
        target=phase_targets.get(phase) if isinstance(phase_targets,dict) else None
        try:
            position=[float(v) for v in mesa["position_ned_m"][:3]]
            velocity=[float(v) for v in mesa["ground_velocity_ned_m_s"][:3]]
            target_enu=[float(v) for v in target[:3]]
            target_ned=[target_enu[1],target_enu[0],-target_enu[2]]
            values=position+velocity+target_ned
            if (len(position)==3 and len(velocity)==3 and len(target_ned)==3
                    and all(math.isfinite(v) for v in values)
                    and phase in {"vertical_takeoff","hover"}):
                out["mesa_local_guidance"]={
                    "schema":"TDOC-MESA-local-guidance-v1",
                    "phase":phase,
                    "frame":"PROJECTED_NED",
                    "position_ned_m":position,
                    "ground_velocity_ned_m_s":velocity,
                    "target_position_ned_m":target_ned,
                    "horizontal_velocity_target_ned_m_s":[0.0,0.0],
                    "controller_objective":"phase_reference_position_and_zero_horizontal_velocity",
                    "navigation_authority":"MESA_STD",
                    "mission_reference_source":"Offline_reference/OnlineMissionPrior",
                    "truth_injection_to_mesa":False,
                    "actuation_authority":False,
                }
        except (KeyError,TypeError,ValueError):
            pass
        attitude_reference=ctx.get("vertical_attitude_reference")
        if isinstance(attitude_reference,dict):
            values=attitude_reference.get("quaternion_wxyz")
            try:
                qref=[float(v) for v in values]
                qnorm=math.sqrt(sum(v*v for v in qref))
                if len(qref)==4 and math.isfinite(qnorm) and qnorm>1e-9:
                    out["vertical_attitude_reference"]={
                        **attitude_reference,
                        "quaternion_wxyz":[v/qnorm for v in qref],
                    }
            except (TypeError,ValueError):
                pass
        # Layer5 owns the nominal attitude/rate decision, while Layer4 Full-Fidelity
        # owns acceptance.  Carry the complete same-cycle simulation state so the
        # bounded vertical tracker does not silently plan a throttle-only horizon.
        # This packet never enters MESA and cannot authorize execution by itself.
        physical_keys=("qw","qx","qy","qz","p_rate","q_rate","r_rate")
        try:
            attitude_rate={k:float(ops[k]) for k in physical_keys}
            if all(math.isfinite(v) for v in attitude_rate.values()):
                out["attitude_rate_state"]={
                    **attitude_rate,
                    "source":("Layer6/GUI/MAVLink current simulation state" if str(ops.get("kinematics_source") or "").startswith("Layer6Client.GUI.") else "PT_LIB/FMI current simulation state") if ctx.get("online_execution_state") or ctx.get("online_physical_state") else "offline physical replay state",
                    "simulation_only":True,"not_field_evidence":True,
                    "mixer_authority":"ArduPilot",
                }
        except (KeyError,TypeError,ValueError):
            pass
        # Preserve the same-cycle source motor state for the Layer5 first-move
        # inverse.  Layer5 may use it only to compensate the identified motor
        # transient inside the already verified commit segment; Layer4 remains the
        # Full-Fidelity acceptance authority and this context has no actuation right.
        try:
            motor_speed={
                "omega_left_rad_s":float(ops["omega_left"]),
                "omega_right_rad_s":float(ops["omega_right"]),
            }
            if all(math.isfinite(v) and v>=0.0 for v in motor_speed.values()):
                out["motor_speed_state"]={
                    **motor_speed,
                    "source":("Layer6/GUI/MAVLink current simulation state" if str(ops.get("kinematics_source") or "").startswith("Layer6Client.GUI.") else "PT_LIB/FMI current simulation state") if ctx.get("online_execution_state") or ctx.get("online_physical_state") else "offline physical replay state",
                    "simulation_only":True,"not_field_evidence":True,
                    "actuation_authority":False,
                }
        except (KeyError,TypeError,ValueError):
            pass
        return out

    def _scenario_map(self,snap):
        # Recreate exactly the reduced Layer-4 scenarios used to build candidates so
        # full-fidelity feasibility/risk use the same scenario contract.
        raw=self.engine.scenarios.generate(snap)
        return {str(x.scenario_id):x for x in self.engine.scenarios.reduce(raw)}

    def _full_objectives_consistent(self,rows,cand,snap,scenario):
        """Evaluate full-fidelity states with the *same* Layer4 ObjectiveEngine.

        The upstream verifier historically used a second objective definition (for
        example time=H*dt instead of H*dt+time-to-go), which makes surrogate/full
        comparison structurally biased. TDOC owns verification and therefore encodes
        replay snapshots back through the authoritative StateCodec/Objectives path.
        """
        import numpy as np
        states=[self.engine.codec.encode(snap)]
        covs=[self.engine.uq.initial_covariance(snap)]
        net=[{"J_C":float(snap.mismatch or 0.0),"mu":float(snap.consistency_margin if snap.consistency_margin is not None else self.engine.config.get("consistency.mu_target",0.25))}]
        for r in rows:
            rs=self.engine.adapter.adapt(r.get("snapshot") or {})
            states.append(self.engine.codec.encode(rs))
            covs.append(self.engine.uq.initial_covariance(rs))
            net.append({"J_C":float(r.get("mismatch") or 0.0),"mu":float(r.get("margin") if r.get("margin") is not None else self.engine.config.get("consistency.mu_target",0.25))})
        X=np.asarray(states,dtype=float)
        return self.engine.objectives.compute(X,covs,net,cand.actions,snap,scenario)

    def _verification_model_identity(self,request):
        base=self.engine.model_hash
        from ..native_ground_policy import require_native_contacts,POLICY_SHA256
        policy=require_native_contacts(self.config,request)
        from ..model_compatibility import integrated_native,capture_model_dependencies
        self._verification_model_dependencies=None
        if not integrated_native(self.config):
            return base
        self._verification_model_dependencies=capture_model_dependencies(self.config)
        from ..utils import sha256_obj
        return sha256_obj({'base_model':base,'ground_contact_policy':policy['revision'],
                           'ground_contact_policy_sha256':POLICY_SHA256,
                           'model_dependencies':self._verification_model_dependencies})

    def _tdoc_full_fidelity(self,result_dict:dict[str,Any],snapshot_dict:dict[str,Any],request:dict[str,Any])->dict[str,Any]:
        """Run strict TDOC-owned full-fidelity verification for the selected candidates.

        Verification remains fail-closed.  This method does not relax any Layer-4
        threshold; it adds explicit physical-action consumption and reason tracing so
        Offline and Online share exactly the same verification boundary.
        """
        if self.layer3_adapter is None: return result_dict
        import numpy as np
        from layer4.models import Candidate
        t_ff0=time.monotonic()
        snap=self.engine.adapter.adapt(snapshot_dict)
        from layer4.native_controller_contract import model_prediction_scope
        prediction_scope=model_prediction_scope(self.engine.config)
        scenario_map=self._scenario_map(snap)
        verification_template=self._verification_template()
        verification_request=self._merge_verification_request(verification_template,request)
        verification_model_hash=self._verification_model_identity(verification_request)
        verification_model_dependencies=getattr(self,'_verification_model_dependencies',None)
        phase_contract=self._full_fidelity_stage_contract(request)
        policy_binding=dict(((request.get("context") or {}).get("knowledge_policy") or {}))
        phase_policy=dict(policy_binding.get("phase_policy") or {})
        policy_hash=(str(policy_binding.get("artifact_hash") or "") or None)
        if policy_binding:
            policy_valid=bool(
                policy_binding.get("ok") is True
                and policy_hash
                and str(phase_policy.get("phase") or "")==str(phase_contract.get("requested_stage") or "")
                and bool((policy_binding.get("policy_contract") or {}).get("offline_prior_only"))
                and (policy_binding.get("policy_contract") or {}).get("actuation_authority") is False
            )
            if not policy_valid:
                raise RuntimeError("knowledge_policy_phase_or_authority_contract_invalid")
        fields=set(Candidate.__dataclass_fields__)
        candidates=list(result_dict.get("candidates") or [])
        if not phase_contract["applicable"]:
            # Not an error: SARG has no canonical reference for hover/VTOL/back-
            # transition phases, and Layer5 nominal control is intentionally not
            # authorized there. Preserve fail-closed semantics without throwing a
            # runtime exception or fabricating a phase mapping.
            records=[]
            for cdict in candidates:
                vr=self._ff_outcome(
                    "NOT_ATTEMPTED","full_fidelity_not_applicable_phase",
                    reason_codes=["full_fidelity_not_applicable_phase"],phase_contract=phase_contract,
                    invocation_owner="TDOC/RuntimeScheduler",
                    safety_semantics="fail_closed_passive_boundary_or_unsupported_phase",
                    selection={"selected":False,"phase_applicable":False},
                )
                cdict["verified"]=False; cdict["candidate_verified"]=False; cdict["verification"]=vr
                records.append({"candidate_id":str(cdict.get("candidate_id")),**vr})
            result_dict["verified_candidates"]=[]
            conf=result_dict.setdefault("conformance",{})
            conf.update({"C_FF":True,"C_FF_phase_applicable":False,"C_FF_runtime_clean":True,
                         "C_FF_bridge_ready":True,"C_FF_physical_actions_consumed":True,"C_FF_verified_count":0,
                         "C_FF_audit_complete":all(bool((c.get("verification") or {}).get("reason_codes")) for c in candidates)})
            result_dict.setdefault("provenance",{})["full_fidelity_verification"]={
                "owner":"TDOC/RuntimeScheduler","internal_reverse_invocation":False,
                "phase_contract":phase_contract,"records":records,"runtime_error_count":0,
                "outcome_counts":{"NOT_ATTEMPTED":len(records),"ATTEMPTED_AND_REJECTED":0,"ATTEMPTED_AND_VERIFIED":0,"EXCEPTION":0},
                "elapsed_s":time.monotonic()-t_ff0,
            }
            return result_dict
        # Keep the real mission phase authoritative for Twin/FMUs and qLPV/VTOL
        # mode semantics.  Layer3's KnowledgeResolver already aliases that mission
        # phase to the canonical SARG prior.  Record the mapping explicitly rather
        # than replacing request.stage with the less-granular SARG vocabulary.
        verification_request["stage"]=phase_contract["requested_stage"]
        verification_request.setdefault("context",{})["tdoc_requested_stage"]=phase_contract["requested_stage"]
        verification_request["context"]["tdoc_sarg_stage_mapping"]=phase_contract["sarg_stage"]
        verification_request["context"]["tdoc_full_fidelity_phase_contract"]="mission_phase_authoritative_sarg_prior_alias_v1"
        # Full-fidelity verification is selective *after* surrogate feasibility.  An
        # infeasible candidate must never consume FF budget and FF must never be able
        # to transform a surrogate-infeasible candidate into an authorized one.
        eligible=[c for c in candidates if c.get("feasible") is True]
        ranked=sorted(eligible,key=lambda c:(float(c.get("robust_score") if c.get("robust_score") is not None else c.get("scalar_cost") if c.get("scalar_cost") is not None else 1e30),str(c.get("candidate_id"))))
        # ``top_k_ff`` limits expensive Full-Fidelity work, not inexpensive action-
        # bridge eligibility checks.  Previously the first K ranked candidates were
        # selected before the Layer5 bridge ran.  A bridge-ineligible candidate then
        # consumed a Full-Fidelity slot despite WingMPET never being invoked, and a
        # lower-ranked executable candidate was never considered.  Scan the finite
        # surrogate-feasible pool in rank order and consume a slot only for a
        # certificate decision or a real FF attempt.  No feasibility, bridge, or FF
        # gate is relaxed by this scheduling correction.
        selected:set[str]=set()
        considered:set[str]=set()
        verified=[]; ff_records=[]; runtime_errors=0
        for cdict in ranked:
            if len(selected)>=self.engine.config.top_k_ff:
                break
            cid=str(cdict.get("candidate_id"))
            considered.add(cid)
            if policy_binding:
                # The same immutable phase policy is consumed by both the FF action
                # bridge and later Layer5 selection.  It is a prior, never a verifier.
                cdict["compiled_online_policy"]={
                    "schema":policy_binding.get("schema"),"artifact_hash":policy_hash,
                    "handoff_hash":policy_binding.get("handoff_hash"),
                    "setd_package_id":policy_binding.get("setd_package_id"),
                    "policy_contract":dict(policy_binding.get("policy_contract") or {}),
                    "phase_policy":phase_policy,
                }
            cand=Candidate(**{k:cdict[k] for k in fields if k in cdict})
            t0=time.monotonic(); bridge=None
            try:
                scenario=scenario_map.get(str(cand.scenario_id))
                # Formalism_layer4_v2: reuse is permitted only when the certificate
                # originated from a real successful FF run and still matches the
                # current phase/model/parameter/context/envelope. The manager itself
                # is fail-closed and cannot mint a certificate from ROM/qLPV alone.
                cert=self.engine.mf.find(cand,snap,scenario,verification_model_hash,self.engine.param_hash)
                phase_now=self.engine.mf.phase(snap)
                expected_timing=self._ff_timing_contract(verification_request,len(cand.actions))
                # The phase-aware Phoenix binding is part of the verified candidate
                # contract, regardless of whether the FF authority is current or
                # reused.  Applying it only in the current-FF branch made Layer5 map
                # an otherwise valid certificate through the generic flight mixer.
                cdict["tdoc_phase"]=phase_contract["requested_stage"]
                if policy_binding or phase_contract["requested_stage"] in {"vertical_takeoff","hover","transition_forward","transition_back","vertical_landing"}:
                    cdict["phoenix_operating_context"]=self._phoenix_hover_operating_context(snapshot_dict)
                    cdict["phoenix_operating_context"]["execution_segment_s"]=float(
                        self.config.get("tdoc.rtmo.l4_artifacts.commit_segment_s",0.1))
                    cdict["phoenix_operating_context"]["forecast_step_s"]=float(expected_timing["forecast_step_s"])
                    cdict["phoenix_operating_context"]["horizon_steps"]=int(expected_timing["horizon_steps"])
                # This mode comes from TDOC's current transaction, never a stale
                # candidate/SARG context. It is inherited by both replay lanes.
                cdict.setdefault('phoenix_operating_context',{})['tdoc_learning_mode']=str(
                    verification_request.get('learning_mode') or 'OFFLINE_SE').upper()
                residual_context=dict(((snapshot_dict.get("guidance") or {}).get(
                    "twin_plant_residual_assimilation") or {}))
                if residual_context.get("applied") is True:
                    # Layer5 may synthesize a bounded first-move correction from
                    # this evidence, but only here, before WingMPET FF.  The same
                    # immutable context remains on the returned candidate.
                    cdict["twin_plant_residual_assimilation"]=residual_context
                cert_reuse_rejected_reason=None
                if cert is not None and str(verification_request.get('learning_mode') or '').upper()=='ONLINE_JSBSIM':
                    # A feasibility certificate is a prior, not the physical
                    # first-move prediction for this Online transaction. Produce
                    # that prediction through the existing current-state FF lane.
                    cert_reuse_rejected_reason='online_current_first_move_prediction_required'
                    cert=None
                if cert is not None and not self._ff_certificate_timing_matches(cert,expected_timing):
                    cert_reuse_rejected_reason="ff_certificate_timing_contract_mismatch"
                    cert=None
                if cert is not None and policy_hash and cert.get("knowledge_policy_sha256")!=policy_hash:
                    cert_reuse_rejected_reason="ff_certificate_knowledge_policy_mismatch"
                    cert=None
                if cert is not None and phase_now in self._TRANSITION_FF_PHASES:
                    cert_phys=dict(cert.get("physical_issuance_evidence") or {})
                    if not self.engine.mf._physical_certificate_evidence_ok(cert_phys,require_transition_chain=True):
                        cert_reuse_rejected_reason="transition_certificate_missing_p1_3_evidence_chain"
                        cert=None
                if cert is not None:
                    selected.add(cid)
                    vr=self._ff_outcome(
                        "ATTEMPTED_AND_VERIFIED","ff_certificate_reuse",reason_codes=["ff_certificate_reuse"],
                        verified=True,attempted=False,executed=False,verification_origin="FF_CERT_REUSE",
                        certificate_id=cert.get("certificate_id"),certificate_persisted=True,ff_origin=True,
                        phase=phase_now,attempt_kind="CERTIFICATE_REUSE",current_cycle_ff_executed=False,
                        model_dependencies=cert.get('model_dependencies'),
                        invocation_owner="TDOC/RuntimeScheduler",selection={"selected":True,"top_k_ff":self.engine.config.top_k_ff},
                        elapsed_s=time.monotonic()-t0,
                    )
                    vr.update(prediction_scope)
                    if phase_now in self._TRANSITION_FF_PHASES:
                        vr["transition_ff_evidence"]=self._transition_ff_evidence(
                            phase_now,outcome_status="ATTEMPTED_AND_VERIFIED",reason_codes=vr.get("reason_codes"),certificate=cert
                        )
                        vr["certificate_physical_issuance_evidence"]=dict(cert.get("physical_issuance_evidence") or {})
                    cdict["verification"]=vr; cdict["verified"]=True; cdict["candidate_verified"]=True
                    verified.append(cdict); ff_records.append({"candidate_id":cid,**vr}); continue
                rejected_cert=self.engine.mf.find_rejection(
                    cand,snap,scenario,verification_model_hash,self.engine.param_hash
                )
                if rejected_cert is not None and not self._ff_certificate_timing_matches(rejected_cert,expected_timing):
                    rejected_cert=None
                if rejected_cert is not None and policy_hash and rejected_cert.get("knowledge_policy_sha256")!=policy_hash:
                    rejected_cert=None
                if rejected_cert is not None:
                    selected.add(cid)
                    vr=self._ff_outcome(
                        "NOT_ATTEMPTED","ff_exact_rejection_certificate_reuse",
                        reason_codes=["ff_exact_rejection_certificate_reuse"]+list(rejected_cert.get("reason_codes") or []),
                        verified=False,attempted=False,executed=False,verification_origin="FF_REJECTION_CERT_REUSE",
                        certificate_id=rejected_cert.get("certificate_id"),certificate_persisted=True,
                        rejection_authority="reject_only_never_verified",phase=phase_now,
                        attempt_kind="EXACT_REJECTION_CERTIFICATE_REUSE",current_cycle_ff_executed=False,
                        invocation_owner="TDOC/RuntimeScheduler",selection={"selected":True,"top_k_ff":self.engine.config.top_k_ff},
                        elapsed_s=time.monotonic()-t0,
                    )
                    if phase_now in self._TRANSITION_FF_PHASES:
                        vr["transition_ff_evidence"]=self._transition_ff_evidence(
                            phase_now,outcome_status="NOT_ATTEMPTED",reason_codes=vr.get("reason_codes")
                        )
                    cdict["verification"]=vr; cdict["verified"]=False; cdict["candidate_verified"]=False
                    ff_records.append({"candidate_id":cid,**vr}); continue
                # Solution 2: phase is part of the candidate contract, not only a
                # temporary FF-bridge hint.  Layer5 must bind the same phase-aware
                # nominal control that Layer4 verified, otherwise joint proof is
                # broken between FF verification and the later L5/L2 control effect.
                cdict["tdoc_phase"]=phase_contract["requested_stage"]
                if policy_binding or phase_contract["requested_stage"] in {"vertical_takeoff","hover","transition_forward","transition_back","vertical_landing"}:
                    cdict["phoenix_operating_context"]=self._phoenix_hover_operating_context(snapshot_dict)
                    cdict["phoenix_operating_context"]["execution_segment_s"]=float(
                        self.config.get("tdoc.rtmo.l4_artifacts.commit_segment_s",0.1))
                    cdict["phoenix_operating_context"]["forecast_step_s"]=float(expected_timing["forecast_step_s"])
                    cdict["phoenix_operating_context"]["horizon_steps"]=int(expected_timing["horizon_steps"])
                residual_context=dict(((snapshot_dict.get("guidance") or {}).get(
                    "twin_plant_residual_assimilation") or {}))
                if residual_context.get("applied") is True:
                    cdict["twin_plant_residual_assimilation"]=residual_context
                bridge_candidate=dict(cdict)
                # The current-FF lane rebuilt phase context after certificate
                # lookup. Preserve TDOC's mode at that final producer boundary.
                bridge_candidate.setdefault('phoenix_operating_context',{})['tdoc_learning_mode']=str(
                    verification_request.get('learning_mode') or 'OFFLINE_SE').upper()
                bridge=self.layer5_adapter.full_fidelity_action_bridge(bridge_candidate) if self.layer5_adapter is not None else {"ok":False,"reason":"layer5_phoenix_bridge_unavailable"}
                if not bridge.get("ok"):
                    reasons=["phoenix_action_bridge_unvalidated",str(bridge.get("reason") or "bridge_not_ready")]
                    vr=self._ff_outcome(
                        "NOT_ATTEMPTED","phoenix_action_bridge_unvalidated",reason_codes=reasons,
                        bridge=bridge,diagnostic_replay_executed=False,invocation_owner="TDOC/RuntimeScheduler",
                        safety_semantics="fail_closed_no_mapping_guess",phase=self.engine.mf.phase(snap),
                        selection={"selected":False,"considered":True,"bridge_eligible":False,
                                   "ff_budget_consumed":False,"top_k_ff":self.engine.config.top_k_ff},elapsed_s=time.monotonic()-t0,
                    )
                    if phase_contract["requested_stage"] in self._TRANSITION_FF_PHASES:
                        vr["transition_ff_evidence"]=self._transition_ff_evidence(
                            phase_contract["requested_stage"],bridge=bridge,outcome_status="NOT_ATTEMPTED",reason_codes=vr.get("reason_codes")
                        )
                        if cert_reuse_rejected_reason:
                            vr["certificate_reuse_rejected_reason"]=cert_reuse_rejected_reason
                    cdict["verification"]=vr; cdict["verified"]=False; cdict["candidate_verified"]=False; ff_records.append({"candidate_id":cid,**vr})
                    continue
                selected.add(cid)
                # The MPC forecast grid and the receding-horizon execution segment
                # are distinct clocks.  Verify them as two independent FF lanes:
                # (1) the only committable first move at the executor/plant period,
                # and (2) the complete conditional forecast at Layer4's forecast
                # interval.  Sequentially applying every one-second trajectory move
                # for only 0.1 s mixes the two clocks and can reject a physically
                # valid horizon (or accept an invalid one).  Both lanes remain
                # fail-closed and WingMPET-authoritative.
                online_mode=str(verification_request.get("learning_mode") or "").upper()=="ONLINE_JSBSIM"
                commit_segment_s=(float(self.config.get("tdoc.rtmo.l4_artifacts.commit_segment_s",0.1))
                                  if online_mode else float(self.engine.config.dt))
                commit_cdict=copy.deepcopy(cdict)
                commit_cdict["trajectory"]=list(cdict.get("trajectory") or [])[:2]
                commit_cdict["actions"]=list(cdict.get("actions") or [])[:1]
                commit_bridge=copy.deepcopy(bridge)
                commit_bridge["logical_actions"]=list(commit_bridge.get("logical_actions") or [])[:1]
                commit_bridge["physical_actions"]=list(commit_bridge.get("physical_actions") or [])[:1]
                horizon_cdict=copy.deepcopy(cdict)
                horizon_cdict.setdefault("phoenix_operating_context",{})["execution_segment_s"]=float(self.engine.config.dt)
                horizon_cdict["phoenix_operating_context"]["full_fidelity_lane"]="FORECAST_HORIZON"
                horizon_bridge=(self.layer5_adapter.full_fidelity_action_bridge(horizon_cdict)
                                if self.layer5_adapter is not None
                                else {"ok":False,"reason":"layer5_phoenix_bridge_unavailable"})
                if not horizon_bridge.get("ok"):
                    reasons=["phoenix_forecast_horizon_bridge_unvalidated",
                             str(horizon_bridge.get("reason") or "forecast_horizon_bridge_not_ready")]
                    vr=self._ff_outcome(
                        "NOT_ATTEMPTED","phoenix_forecast_horizon_bridge_unvalidated",
                        reason_codes=reasons,bridge=commit_bridge,
                        forecast_horizon_bridge=horizon_bridge,
                        diagnostic_replay_executed=False,
                        invocation_owner="TDOC/RuntimeScheduler",
                        safety_semantics="fail_closed_no_cross_clock_action_reuse",
                        phase=self.engine.mf.phase(snap),
                        selection={"selected":False,"considered":True,"bridge_eligible":False,
                                   "ff_budget_consumed":False,"top_k_ff":self.engine.config.top_k_ff},
                        elapsed_s=time.monotonic()-t0,
                    )
                    cdict["verification"]=vr; cdict["verified"]=False
                    cdict["candidate_verified"]=False
                    ff_records.append({"candidate_id":cid,**vr})
                    continue
                horizon_action_provider=None
                if online_mode:
                    # A real MPC executor commits one short first move and replans
                    # from the next observed state.  Certifying a six-second list of
                    # controls computed only from the entry state instead certifies
                    # an open-loop controller that Online never executes.  During
                    # the non-actuating WingMPET forecast, regenerate Layer5's first
                    # move from each physically propagated twin state.  Layer5 still
                    # owns the decision, Layer4 still owns acceptance, and Layer3
                    # remains the sole L2/twin replay owner.
                    base_horizon_candidate=copy.deepcopy(horizon_cdict)
                    base_trajectory=copy.deepcopy(list(horizon_cdict.get("trajectory") or []))
                    base_actions=copy.deepcopy(list(horizon_cdict.get("actions") or []))
                    codec=self.engine.codec
                    def horizon_action_provider(*,step_index,physical_state,mapped_state,
                                                trajectory_state,wind_ned):
                        remaining=max(1,len(base_actions)-int(step_index))
                        policy_candidate=copy.deepcopy(base_horizon_candidate)
                        policy_candidate["trajectory"]=copy.deepcopy(base_trajectory[int(step_index):])
                        policy_candidate["actions"]=copy.deepcopy(base_actions[int(step_index):])
                        if len(policy_candidate["trajectory"])<2 or not policy_candidate["actions"]:
                            return {"ok":False,"reason":"forecast_policy_remaining_horizon_empty"}
                        current=policy_candidate["trajectory"][0]
                        try:
                            current[codec.index["mesa_position_ned_m_0"]]=float(mapped_state["pn"])
                            current[codec.index["mesa_position_ned_m_1"]]=float(mapped_state["pe"])
                            current[codec.index["mesa_position_ned_m_2"]]=float(mapped_state["pd"])
                            current[codec.index["Vg_0"]]=float(physical_state["vn"])
                            current[codec.index["Vg_1"]]=float(physical_state["ve"])
                            current[codec.index["Vg_2"]]=float(physical_state["vd"])
                            va=[float(physical_state[k])-float(wind_ned[i])
                                for i,k in enumerate(("vn","ve","vd"))]
                            airspeed=math.sqrt(sum(v*v for v in va))
                            current[codec.index["airspeed_m_s"]]=airspeed
                            oc=policy_candidate.setdefault("phoenix_operating_context",{})
                            oc.update({
                                "execution_segment_s":float(self.engine.config.dt),
                                "forecast_step_s":float(self.engine.config.dt),
                                "horizon_steps":remaining,
                                "full_fidelity_lane":"FORECAST_HORIZON",
                                "airspeed_m_s":airspeed,
                            })
                            oc["attitude_rate_state"]={
                                **{k:float(physical_state[k]) for k in
                                   ("qw","qx","qy","qz","p_rate","q_rate","r_rate")},
                                "source":("Layer6/GUI/MAVLink current simulation state + WingMPET forecast propagation" if self.config.get("layers.layer6.online.transport")=="Layer6Client_GUI_MAVLink" else "PT_LIB/FMI current simulation state + WingMPET forecast propagation"),
                                "simulation_only":True,"not_field_evidence":True,
                                "mixer_authority":"ArduPilot",
                            }
                            oc["motor_speed_state"]={
                                "omega_left_rad_s":float(physical_state["omega_left"]),
                                "omega_right_rad_s":float(physical_state["omega_right"]),
                                "source":"WingMPET Full-Fidelity forecast propagation",
                                "simulation_only":True,"not_field_evidence":True,
                                "actuation_authority":False,
                            }
                            local=oc.get("mesa_local_guidance")
                            if isinstance(local,dict):
                                local["position_ned_m"]=[float(mapped_state[k]) for k in ("pn","pe","pd")]
                                local["ground_velocity_ned_m_s"]=[float(physical_state[k]) for k in ("vn","ve","vd")]
                                local["state_source"]="MESA-aligned WingMPET Full-Fidelity forecast"
                                local["actuation_authority"]=False
                        except (KeyError,TypeError,ValueError,IndexError) as exc:
                            return {"ok":False,"reason":"forecast_policy_state_rebind_failed",
                                    "error":repr(exc)}
                        step_bridge=(self.layer5_adapter.full_fidelity_action_bridge(policy_candidate)
                                     if self.layer5_adapter is not None else
                                     {"ok":False,"reason":"layer5_phoenix_bridge_unavailable"})
                        if not step_bridge.get("ok") or not step_bridge.get("physical_actions"):
                            return {"ok":False,
                                    "reason":str(step_bridge.get("reason") or
                                                 "forecast_policy_layer5_bridge_failed"),
                                    "bridge_record":step_bridge}
                        return {"ok":True,
                                "physical_action":dict(step_bridge["physical_actions"][0]),
                                "remaining_horizon_steps":remaining,
                                "bridge_record":step_bridge}
                commit_request=copy.deepcopy(verification_request)
                commit_request["transaction_id"]=f"{verification_request.get('transaction_id','online')}-ff-commit"
                commit_request.setdefault("context",{})["tdoc_full_fidelity_lane"]="COMMIT_SEGMENT"
                commit_rows=self.layer3_adapter.full_fidelity_replay(commit_cdict,snapshot_dict,commit_request,self.engine.codec,self.engine.config.dt,
                    scenario=scenario,physical_actions=commit_bridge.get("physical_actions"),bridge_record=commit_bridge,
                    physical_step_s=commit_segment_s)
                horizon_request=copy.deepcopy(verification_request)
                horizon_request["transaction_id"]=f"{verification_request.get('transaction_id','online')}-ff-horizon"
                horizon_request.setdefault("context",{})["tdoc_full_fidelity_lane"]="FORECAST_HORIZON"
                rows=self.layer3_adapter.full_fidelity_replay(cdict,snapshot_dict,horizon_request,self.engine.codec,self.engine.config.dt,
                    scenario=scenario,physical_actions=horizon_bridge.get("physical_actions"),bridge_record=horizon_bridge,
                    physical_step_s=float(self.engine.config.dt),
                    physical_action_provider=horizon_action_provider)
                jmax=float((snap.slow_policy.get("consistency_bounds") or {}).get("maximum_mismatch",self.engine.config.get("consistency.j_c_max",25)))
                mumin=float((snap.slow_policy.get("consistency_bounds") or {}).get("minimum_margin",self.engine.config.get("consistency.mu_min",-1)))
                # Problem #5 (v0910): numeric mismatch/margin alone do not prove that
                # Layer-3 consistency was actually assessed.  A replay step is complete
                # only when CoC has no missing expected observations and no active edge
                # remains unassessed.  This keeps turn/approach fail-closed while allowing
                # them to verify once the mission-time fixes preserve MESA velocity.
                def _coc_complete(r):
                    return (
                        r.get("mismatch") is not None
                        and r.get("margin") is not None
                        and not (r.get("coc_missing_expected_observations") or [])
                        and not (r.get("coc_unassessed_active_edges") or [])
                        and str(r.get("coc_status") or "").lower() not in {"unassessed", "incomplete", "failed"}
                    )
                both_lanes=list(commit_rows)+list(rows)
                all_assessed=bool(commit_rows) and bool(rows) and all(_coc_complete(r) for r in both_lanes)
                consistency_safe=all_assessed and all(float(r["mismatch"])<=jmax and float(r["margin"])>=mumin for r in both_lanes)
                twin_safe=bool(commit_rows) and bool(rows) and all(bool(r.get("validity_ok")) for r in both_lanes)
                stability_safe=bool(commit_rows) and bool(rows) and all(((r.get("stability") or {}).get("stable") is not False) for r in both_lanes)
                physical_actions_consumed=bool(commit_rows) and bool(rows) and all(bool(r.get("physical_action_consumed")) for r in both_lanes)
                # Solution 8: the WingMPET op27 physical verdict is authoritative and
                # non-bypassable.  A mapping/certificate may authorize a command to be
                # replayed, but it may never turn a failed physical replay into a
                # verified candidate.  Require every replay row, every reported output,
                # and every recorded op27 substep to remain VTOL-feasible.
                commit_wingmpet_verdict=self._wingmpet_physical_verdict(commit_rows)
                horizon_wingmpet_verdict=self._wingmpet_physical_verdict(rows)
                physical_runtime_safe=bool(commit_wingmpet_verdict.get("all_rows_vtol_feasible") and horizon_wingmpet_verdict.get("all_rows_vtol_feasible"))
                wingmpet_verdict={
                    "schema":"TDOC-L4-FF-dual-lane-WingMPET-v1",
                    "authoritative":True,
                    "non_bypassable":True,
                    "override_allowed":False,
                    "all_rows_vtol_feasible":physical_runtime_safe,
                    "rows":list(commit_wingmpet_verdict.get("rows") or [])+list(horizon_wingmpet_verdict.get("rows") or []),
                    "commit_segment":commit_wingmpet_verdict,
                    "forecast_horizon":horizon_wingmpet_verdict,
                }
                full_obj=self._full_objectives_consistent(rows,cand,snap,scenario)
                full_norm=self.engine.verifier._normalize(full_obj); sur_norm=cand.normalized_objectives or self.engine.verifier._normalize(cand.objective_vector)
                keys=sorted(set(full_norm)&set(sur_norm)); delta=float(np.linalg.norm([full_norm[k]-sur_norm[k] for k in keys])) if keys else float("inf")
                eps=float(self.engine.config.get("verification.objective_tolerance_normalized",2.0)); objective_safe=delta<=eps
                commit_ff_feas=self.engine.verifier._full_feasibility(
                    commit_rows,cand,snap,scenario,progress_step_count=len(cand.actions))
                horizon_ff_feas=self.engine.verifier._full_feasibility(rows,cand,snap,scenario)
                ff_feas={
                    "safe":bool(commit_ff_feas["safe"] and horizon_ff_feas["safe"]),
                    "physical_mission_safe":bool(commit_ff_feas["safe"] and horizon_ff_feas["safe"]),
                    "action_safe":bool(commit_ff_feas["action_safe"] and horizon_ff_feas["action_safe"]),
                    "reasons":sorted(set(list(commit_ff_feas.get("reasons") or [])+list(horizon_ff_feas.get("reasons") or []))),
                    "P_viol_full":max(float(commit_ff_feas["P_viol_full"]),float(horizon_ff_feas["P_viol_full"])),
                    "steps":list(horizon_ff_feas.get("steps") or []),
                    "commit_segment_steps":list(commit_ff_feas.get("steps") or []),
                    "commit_segment":commit_ff_feas,"forecast_horizon":horizon_ff_feas,
                }
                chance_safe=float(ff_feas["P_viol_full"])<=float(self.engine.config.get("feasibility.alpha_risk",0.1))
                physical_mission_safe=bool(ff_feas["safe"] and physical_runtime_safe and physical_actions_consumed)
                F_full=bool(consistency_safe and twin_safe and stability_safe and physical_mission_safe)
                safe=bool(F_full and chance_safe and objective_safe)
                reasons=[]
                if not all_assessed: reasons.append("unassessed_layer3_consistency")
                elif not consistency_safe: reasons.append("layer3_consistency_gate_failed")
                if not twin_safe: reasons.append("twin_validity_gate_failed")
                if not stability_safe: reasons.append("stability_gate_failed")
                if not physical_actions_consumed: reasons.append("physical_actions_not_consumed")
                if not physical_runtime_safe: reasons.append("wingmpet_physical_runtime_gate_failed")
                if not ff_feas["safe"]: reasons.append("full_physical_feasibility_gate_failed")
                if not chance_safe: reasons.append("chance_constraint_gate_failed")
                if not objective_safe: reasons.append("objective_consistency_gate_failed")
                # Preserve the detailed Full-Fidelity physical reason codes rather
                # than collapsing them into one generic rejection label.
                for _reason in list(ff_feas.get("reasons") or []):
                    if str(_reason) not in reasons: reasons.append(str(_reason))
                if safe: reasons=["verified"]
                vr=self._ff_outcome(
                    "ATTEMPTED_AND_VERIFIED" if safe else "ATTEMPTED_AND_REJECTED",
                    "verified" if safe else (reasons[0] if reasons else "full_fidelity_verification_gate_failed"),
                    reason_codes=reasons,verified=safe,attempted=True,executed=bool(commit_rows and rows),
                    verification_origin="FF_CURRENT" if safe else "UNVERIFIED",phase=self.engine.mf.phase(snap),
                    bridge=commit_bridge,forecast_horizon_bridge=horizon_bridge,
                    F_full=F_full,chance_safe=chance_safe,objective_safe=objective_safe,
                    objective_delta_normalized=delta,objective_tolerance_normalized=eps,full_objectives=full_obj,surrogate_objectives=cand.objective_vector,
                    consistency_safe=consistency_safe,twin_validity_safe=twin_safe,stability_safe=stability_safe,
                    physical_actions_consumed=physical_actions_consumed,physical_runtime_safe=physical_runtime_safe,physical_mission_safe=physical_mission_safe,
                    wingmpet_physical_verdict=wingmpet_verdict,
                    P_viol_full=ff_feas["P_viol_full"],full_feasibility=ff_feas,surrogate_chance_risk=float(cand.violation_probability),
                    commit_segment_steps=[{k:v for k,v in r.items() if k!="snapshot"} for r in commit_rows],
                    steps=[{k:v for k,v in r.items() if k!="snapshot"} for r in rows],
                    adapter="TDOC-owned dual-lane commit-segment + forecast-horizon Layer2/WingMPET/Layer3 FF replay + authoritative Layer4 ObjectiveEngine",
                    invocation_owner="TDOC/RuntimeScheduler",selection={"selected":True,"top_k_ff":self.engine.config.top_k_ff},
                    elapsed_s=time.monotonic()-t0,
                    timing_contract=self._ff_timing_contract(verification_request,len(rows)),
                )
                vr["knowledge_policy_sha256"]=policy_hash
                vr["knowledge_policy_role"]="offline_prior_only" if policy_hash else "not_present"
                if phase_contract["requested_stage"] in self._TRANSITION_FF_PHASES:
                    vr["transition_ff_evidence"]=self._transition_ff_evidence(
                        phase_contract["requested_stage"],bridge=commit_bridge,rows=commit_rows,
                        outcome_status=vr.get("outcome_status"),reason_codes=vr.get("reason_codes")
                    )
                    if cert_reuse_rejected_reason:
                        vr["certificate_reuse_rejected_reason"]=cert_reuse_rejected_reason
                    if not bool((vr.get("transition_ff_evidence") or {}).get("audit_complete")):
                        safe=False
                        vr=self._ff_outcome(
                            "ATTEMPTED_AND_REJECTED","transition_ff_evidence_incomplete",
                            reason_codes=list(reasons)+["transition_ff_evidence_incomplete"],verified=False,attempted=True,executed=bool(commit_rows and rows),
                            verification_origin="UNVERIFIED",phase=phase_contract["requested_stage"],bridge=commit_bridge,
                            forecast_horizon_bridge=horizon_bridge,
                            transition_ff_evidence=vr.get("transition_ff_evidence"),physical_actions_consumed=physical_actions_consumed,
                            physical_runtime_safe=physical_runtime_safe,physical_mission_safe=physical_mission_safe,wingmpet_physical_verdict=wingmpet_verdict,
                            steps=[{k:v for k,v in r.items() if k!="snapshot"} for r in rows],invocation_owner="TDOC/RuntimeScheduler",
                            selection={"selected":True,"top_k_ff":self.engine.config.top_k_ff},elapsed_s=time.monotonic()-t0,
                        )
                if verification_model_dependencies is not None:
                    vr['model_dependencies']=verification_model_dependencies
                vr.update(prediction_scope)
                if safe:
                    phase_name=str(vr.get("phase") or "")
                    # P0-8: transition phases are safety-critical Full-Fidelity proof,
                    # just like vertical boundary phases. A verified claim must be
                    # durably bound to the authoritative WingMPET physical verdict.
                    # Preserve the existing vertical-certificate invariant and extend
                    # the same authoritative physical-verdict requirement to both
                    # transition directions for P0-8 auditability.
                    require_vertical_certificate=phase_name in {"vertical_takeoff","hover","vertical_landing"}
                    require_transition_certificate=phase_name in {"transition_forward","transition_back"}
                    require_physical_certificate=bool(require_vertical_certificate or require_transition_certificate)
                    cert=self.engine.mf.build(
                        cand,snap,scenario,verification_model_hash,self.engine.param_hash,vr,
                        require_physical_verdict=require_physical_certificate,
                    )
                    if cert is not None:
                        vr["certificate_id"]=cert.get("certificate_id")
                        vr["certificate_persisted"]=bool(cert.get("bank_persisted"))
                        vr["certificate_issuance_contract"]=cert.get("issuance_contract")
                    else:
                        # No current FF success is auditable as VERIFIED without a
                        # durable certificate. Fail closed for all phases.
                        prior=dict(vr)
                        vr=self._ff_outcome(
                            "ATTEMPTED_AND_REJECTED","ff_certificate_issuance_failed",
                            reason_codes=list(prior.get("reason_codes") or [])+["ff_certificate_issuance_failed"],
                            verified=False,attempted=True,executed=bool(commit_rows and rows),verification_origin="UNVERIFIED",
                            phase=phase_name,bridge=commit_bridge,forecast_horizon_bridge=horizon_bridge,
                            prior_verification=prior,certificate_persisted=False,
                            physical_actions_consumed=physical_actions_consumed,physical_runtime_safe=physical_runtime_safe,
                            physical_mission_safe=physical_mission_safe,wingmpet_physical_verdict=wingmpet_verdict,
                            invocation_owner="TDOC/RuntimeScheduler",selection={"selected":True,"top_k_ff":self.engine.config.top_k_ff},
                            elapsed_s=time.monotonic()-t0,
                        )
                        # Backward-compatible explicit fail-closed assignments retained
                        # for existing source-level safety regression guards.
                        vr["verified"]=False
                        vr["candidate_verified"]=False
                elif vr.get("outcome_status")=="ATTEMPTED_AND_REJECTED" and vr.get("executed"):
                    rejection_cert=self.engine.mf.build_rejection(
                        cand,snap,scenario,verification_model_hash,self.engine.param_hash,vr
                    )
                    if rejection_cert is not None:
                        vr["rejection_certificate_id"]=rejection_cert.get("certificate_id")
                        vr["rejection_certificate_persisted"]=True
                        vr["rejection_certificate_authority"]="reject_only_never_verified"
            except Exception as exc:
                runtime_errors+=1
                selected.add(cid)
                err=repr(exc)
                vr=self._ff_outcome(
                    "EXCEPTION","tdoc_full_fidelity_runtime_failed",reason_codes=["tdoc_full_fidelity_runtime_failed"],
                    attempted=True,executed=False,error=err,error_id=self._ff_error_id(cid,"tdoc_full_fidelity_runtime_failed",err),
                    bridge=bridge,phase=self.engine.mf.phase(snap),invocation_owner="TDOC/RuntimeScheduler",
                    selection={"selected":True,"top_k_ff":self.engine.config.top_k_ff},elapsed_s=time.monotonic()-t0,
                )
                policy_failure=getattr(exc,"policy_failure",None)
                if isinstance(policy_failure,dict):
                    vr["action_policy_failure"]=policy_failure
                    from ..diagnostics import event
                    failed_bridge=(policy_failure.get("provider_result") or {}).get("bridge_record") or {}
                    event("layer4.full_fidelity.policy_rejected",candidate_id=cid,
                          step_index=policy_failure.get("step_index"),
                          reason=policy_failure.get("reason"),binding=failed_bridge.get("binding"),
                          replay_evidence=policy_failure.get("replay_evidence"),
                          actuation_authority=False)
                if phase_contract["requested_stage"] in self._TRANSITION_FF_PHASES:
                    vr["transition_ff_evidence"]=self._transition_ff_evidence(
                        phase_contract["requested_stage"],bridge=bridge,outcome_status="EXCEPTION",reason_codes=vr.get("reason_codes")
                    )
            cdict["verification"]=vr; cdict["verified"]=bool(vr.get("verified")); cdict["candidate_verified"]=bool(vr.get("verified")); ff_records.append({"candidate_id":cid,**vr})
            if cdict["verified"]: verified.append(cdict)
        # P0-8: every candidate still receives exactly one auditable outcome.  Pool
        # members skipped after the actual FF budget is consumed, plus surrogate-
        # infeasible candidates, are explicitly NOT_ATTEMPTED.
        recorded={str(r.get("candidate_id")) for r in ff_records}
        for cdict in candidates:
            cid=str(cdict.get("candidate_id"))
            if cid in recorded:
                continue
            if cdict.get("feasible") is not True:
                reason="surrogate_infeasible_not_full_fidelity_attempted"
            else:
                reason="top_k_full_fidelity_budget_not_selected"
            vr=self._ff_outcome(
                "NOT_ATTEMPTED",reason,reason_codes=[reason],phase=phase_contract["requested_stage"],
                verification_origin="UNVERIFIED",invocation_owner="TDOC/RuntimeScheduler",
                selection={"selected":False,"considered":cid in considered,
                           "ff_budget_consumed":False,"top_k_ff":self.engine.config.top_k_ff},
                safety_semantics="selective_full_fidelity_fail_closed",
            )
            if phase_contract["requested_stage"] in self._TRANSITION_FF_PHASES:
                vr["transition_ff_evidence"]=self._transition_ff_evidence(
                    phase_contract["requested_stage"],outcome_status="NOT_ATTEMPTED",reason_codes=vr.get("reason_codes")
                )
            cdict["verification"]=vr; cdict["verified"]=False; cdict["candidate_verified"]=False
            ff_records.append({"candidate_id":cid,**vr})
        result_dict["verified_candidates"]=verified
        if prediction_scope:
            for candidate in candidates:
                candidate.setdefault('provenance',{}).update(prediction_scope)
                candidate.setdefault('verification',{}).update(prediction_scope)
            for record in ff_records:
                record.update(prediction_scope)
        conf=result_dict.setdefault("conformance",{})
        selected_records=[r for r in ff_records if bool((r.get("selection") or {}).get("selected"))]
        attempted_records=[r for r in selected_records if r.get("outcome_status") in {"ATTEMPTED_AND_REJECTED","ATTEMPTED_AND_VERIFIED","EXCEPTION"}]
        ff_states=self._FF_OUTCOME_STATES
        transition_records=[r for r in ff_records if str(r.get("phase") or "") in self._TRANSITION_FF_PHASES]
        transition_evidence_complete=all(
            isinstance(r.get("transition_ff_evidence"),dict)
            and bool((r.get("transition_ff_evidence") or {}).get("audit_complete"))
            for r in transition_records
        ) if transition_records else True
        audit_complete=(
            len(ff_records)==len(candidates)
            and all(r.get("schema")==self._FF_OUTCOME_SCHEMA and r.get("outcome_status") in ff_states and bool(r.get("reason_codes")) for r in ff_records)
            and all((not bool(r.get("verified"))) or (r.get("outcome_status")=="ATTEMPTED_AND_VERIFIED" and bool(r.get("certificate_id"))) for r in ff_records)
            and transition_evidence_complete
        )
        # C_FF means every VERIFIED claim has a traceable FF origin/certificate; it
        # does not require every candidate to consume FF budget.
        conf["C_FF"]=all((not bool(r.get("verified"))) or (r.get("outcome_status")=="ATTEMPTED_AND_VERIFIED" and bool(r.get("certificate_id"))) for r in ff_records)
        conf["C_FF_audit_complete"]=audit_complete
        conf["C_FF_transition_evidence_complete"]=transition_evidence_complete
        conf["C_FF_runtime_clean"]=runtime_errors==0
        bridge_records=[r for r in selected_records if r.get("reason") not in {
            "ff_certificate_reuse","ff_exact_rejection_certificate_reuse"
        }]
        # P1-3: bridge readiness is a real readiness assertion, not a vacuous
        # acknowledgement that an explicit bridge failure was recorded.
        conf["C_FF_bridge_ready"]=all(bool((r.get("bridge") or {}).get("ok")) for r in bridge_records) if bridge_records else True
        executed_records=[r for r in attempted_records if bool(r.get("executed"))]
        conf["C_FF_physical_actions_consumed"]=all(bool(r.get("physical_actions_consumed")) for r in executed_records) if executed_records else True
        conf["C_FF_physical_action_evidence_count"]={
            "selected":len(selected_records),
            "bridge_checked":len(bridge_records),
            "executed":len(executed_records),
            "consumed":sum(1 for r in executed_records if bool(r.get("physical_actions_consumed"))),
            "transition_records":len(transition_records),
            "transition_audit_complete":sum(1 for r in transition_records if bool((r.get("transition_ff_evidence") or {}).get("audit_complete"))),
        }
        conf["C_FF_wingmpet_physical_verdict_enforced"]=all(
            bool((r.get("wingmpet_physical_verdict") or {}).get("authoritative")) for r in executed_records
        ) if executed_records else True
        conf["C_FF_verified_count"]=len(verified)
        conf["C_FF_certificate_issuance_integrity"]=all(
            (not bool(r.get("verified"))) or bool(r.get("certificate_persisted"))
            for r in ff_records
        )
        conf["C_FF_phase_applicable"]=True
        outcome_counts={state:sum(1 for r in ff_records if r.get("outcome_status")==state) for state in sorted(ff_states)}
        result_dict.setdefault("provenance",{})["full_fidelity_verification"]={"owner":"TDOC/RuntimeScheduler","internal_reverse_invocation":False,
            "phase_contract":phase_contract,"records":ff_records,"runtime_error_count":runtime_errors,"outcome_counts":outcome_counts,
            "selected_candidate_ids":sorted(selected),"audit_complete":audit_complete,"elapsed_s":time.monotonic()-t_ff0}
        return result_dict

    def invoke(self,payload:dict[str,Any])->dict[str,Any]:
        self._ensure()
        request=payload.get("request") or {}
        phase=str(request.get("stage") or ((payload.get("snapshot") or {}).get("context") or {}).get("stage") or "").strip().lower()
        online=str(request.get("learning_mode") or "").upper()=="ONLINE_JSBSIM"
        envelope=None
        native_online=online and self.config.get("layers.layer6.online.transport")=="Layer6Client_GUI_MAVLink"
        native_source=getattr(self,'native_execution_source',None)
        if native_online and native_source is not None:
            from layer4.native_operational import require,REQUEST_SCHEMA
            candidate,service=native_source(copy.deepcopy(payload['snapshot']),copy.deepcopy(request))
            tx=payload.get('transaction_id') or request.get('transaction_id')
            require(candidate['schema']==REQUEST_SCHEMA and candidate['snapshot']==payload['snapshot'],
                    'Native candidate source changed the current Layer3 snapshot')
            require(bool(tx) and candidate['transaction_id']==tx,'Native candidate transaction mismatch')
            result=self.verify_native_execution(candidate,service)
            return {'ok':True,'result':result,'provenance':{'invocation_owner':'TDOC/RuntimeScheduler',
                    'scope':'CURRENT_NATIVE_OPERATIONAL_VERIFICATION','actuation_executed':False}}
        if native_online:
            prior=(self.layer5_adapter.ensure_native_control_binding() if self.layer5_adapter is not None
                   else {"ok":False,"reason":"layer5_adapter_unavailable"})
            if not prior.get("ok"):
                raise RuntimeError(f"native Layer4 requires accepted Offline model prior: {prior}")
            # The retired FMI commissioning envelope cannot constrain the native GUI
            # interface. Keep the core source-domain bounds; current FF and FINAL
            # still authorize each decision. An Offline prior grants no actuation.
        if online and not native_online and phase in {"vertical_takeoff","hover"}:
            context=self._phoenix_hover_operating_context(payload["snapshot"])
            timing=self._ff_timing_contract(request,self.engine.config.horizon)
            context["execution_segment_s"]=float(timing["execution_segment_s"])
            context["forecast_step_s"]=float(timing["forecast_step_s"])
            context["horizon_steps"]=int(timing["horizon_steps"])
            envelope=(self.layer5_adapter.layer4_action_envelope(
                phase,context,self.engine.config.horizon) if self.layer5_adapter is not None
                else {"ok":False,"reason":"layer5_action_envelope_adapter_unavailable"})
        self.engine.set_runtime_action_envelope(envelope)
        result=self.engine.run(payload["snapshot"]); d=result.to_dict()
        from contextlib import nullcontext
        scope=getattr(self.layer5_adapter,'isolated_verification_model_scope',None)
        with scope() if callable(scope) else nullcontext():
            d=self._tdoc_full_fidelity(d,payload["snapshot"],request)
        # Full candidate JSONL remains in the transient append-only workspace and is
        # flushed by close()/the terminal checkpoint.  Per-invocation gzip of this
        # multi-megabyte evidence is archival work, not part of the L4 control path.
        sync={"deferred":True,"checkpoint":"Layer4Adapter.close","persistent_compressed":True,
              "working_root":str(self.native_workspace.working_root),"persistent_root":str(self.native_root)}
        d.setdefault("provenance",{})["invocation_owner"]="TDOC/RuntimeScheduler"
        d["provenance"]["layer5_action_envelope_contract"]=envelope
        d["provenance"]["native_storage"]={"root":str(self.native_root),"compressed_at_rest":True,"sync":sync}
        return {"ok":True,"result":d}
    def close(self):
        try:self.native_workspace.close()
        finally:self.engine=None
