from __future__ import annotations
import copy, json, uuid, time, math
from typing import Any
from .adapters import Layer2Adapter,Layer3Adapter,Layer4Adapter,Layer5Adapter,Layer6OnlineAdapter,Layer7Adapter
from .dependency import DependencyChecker
from .health import HealthManager
from .twin_registry import TwinRegistry
from .network_state import NetworkStateMirror
from .feedback import FeedbackRouter
from .invocation import InvocationGate
from .models import CycleResult,InvocationEvent
from .provenance import ProvenanceLog
from .registry import LayerRegistry
from .scheduler import TDOCScheduler
from .utils import sha256_obj,utc_now,jsonable
from .versioning import VersionManager
from .unit_adapter import UnitAdapterBinding
from .knowledge import KnowledgeLedger
from .knowledge_handoff import OfflineKnowledgeHandoff
from .knowledge_policy import KnowledgePolicyCompiler
from .knowledge_startup import optional_policy_admission
from .participation import cycle_mpc_participation, cycle_decision_gate_trace
from .rtmo.artifact_gate import RTMOL4ArtifactGate
from .rtmo.realtime_metrics import RTMOControlLatencyTracker
from .rtmo.persistence_lane import OnlinePersistenceLane
from .storage import LayerRuntimeArchive, write_json_gz
from .vehicle_state import phoenix_state_from_ptlib_snapshot

def _layer7_trigger_eligibility(*, layer6_output, execution_evidence, layer7_preflight, request):
    """Classify Layer7 trigger authority without turning an upstream hold into a Layer7 fault.

    CONTROL_TRAINING requires realized Layer6 execution evidence. The explicit
    observation-only validation mode may route telemetry evidence to Layer7, but
    that route never claims mission progress or actuation.
    """
    o6=layer6_output if isinstance(layer6_output,dict) else {}
    ev=execution_evidence if isinstance(execution_evidence,dict) else {}
    pf7=layer7_preflight if isinstance(layer7_preflight,dict) else {}
    req=request if isinstance(request,dict) else {}
    status=str(o6.get("status") or "").upper()
    source=str(ev.get("source") or "")
    destination=str(ev.get("destination") or "")
    schema=str(ev.get("schema") or "")
    evidence_bound=bool(ev and destination=="Layer7" and schema.startswith("TDOC-L6-online-"))
    explicit_observation_only=bool(req.get("online_observation_only"))
    realized_execution=bool(status=="EXECUTED" and o6.get("actuation_executed") is True and evidence_bound and source in {"Layer6/PT_LIB","Layer6/GUI/MAVLink"})
    validation_observation=bool(explicit_observation_only and status=="OBSERVATION_ONLY" and evidence_bound and ev.get("validation_only") is True)
    upstream_eligible=bool(realized_execution or validation_observation)
    layer7_ready=bool(pf7.get("ok",False))
    eligible=bool(upstream_eligible and layer7_ready)
    if not upstream_eligible:
        reason="upstream_execution_evidence_unavailable"
        status_label="NOT_INVOKED_UPSTREAM_EXECUTION"
        consequence_only=True
        root_cause=False
    elif not layer7_ready:
        reason="layer7_preflight_not_ready"
        status_label="HELD_LAYER7_NOT_READY"
        consequence_only=False
        root_cause=True
    else:
        reason="layer7_trigger_ready"
        status_label="READY_TO_INVOKE"
        consequence_only=False
        root_cause=False
    return {
        "schema":"TDOC-L7-trigger-eligibility-v1",
        "eligible":eligible,
        "upstream_eligible":upstream_eligible,
        "realized_execution":realized_execution,
        "validation_observation":validation_observation,
        "layer7_ready":layer7_ready,
        "layer6_status":status,
        "evidence_bound":evidence_bound,
        "reason":reason,
        "status":status_label,
        "consequence_only":consequence_only,
        "root_cause":root_cause,
    }

def _finite_plant_time(obj:Any)->float|None:
    """Extract FMI/JSBSim simulation time without treating scheduler time as plant time."""
    stack=[obj]; seen=0
    wanted={"sim_time_s","simulation_time_s","plant_time_s"}
    while stack and seen<4000:
        cur=stack.pop(); seen+=1
        if isinstance(cur,dict):
            for k,v in cur.items():
                if str(k).lower() in wanted:
                    try:
                        x=float(v)
                        if math.isfinite(x): return x
                    except Exception:
                        pass
                if isinstance(v,(dict,list,tuple)): stack.append(v)
        elif isinstance(cur,(list,tuple)):
            stack.extend(v for v in cur if isinstance(v,(dict,list,tuple)))
    return None

def _current_online_observation_contract(*, online_state:dict[str,Any]|None, transaction_id:str, episode_id:Any=None,
                                         cycle_in_episode:Any=None, previous_current_plant_time_s:float|None=None,
                                         previous_post_execution:dict[str,Any]|None=None)->dict[str,Any]:
    """Classify a public PT_LIB/FMI state sample without granting navigation authority.

    A current simulator state is always useful diagnostic/execution-state evidence, but
    it becomes execution-correlated freshness only for the cycle immediately following
    a successful Layer6 execution in the same episode and only when FMI plant time is
    present and non-regressing.  This prevents commissioning/free-running simulator
    truth from silently becoming GNSS-denied MESA navigation evidence.
    """
    state=dict(online_state or {})
    ingress=dict(state.get("ptlib_ingress") or {})
    raw=dict(ingress.get("state") or {})
    plant_time=_finite_plant_time(raw if raw else ingress)
    prev_current=None
    try:
        if previous_current_plant_time_s is not None: prev_current=float(previous_current_plant_time_s)
    except Exception: prev_current=None
    monotonic=bool(plant_time is not None and (prev_current is None or plant_time+1e-9>=prev_current))
    prev=dict(previous_post_execution or {})
    prev_pt=_finite_plant_time(prev)
    same_episode=bool(prev and str(prev.get("episode_id"))==str(episode_id))
    adjacent=False
    try:
        adjacent=bool(prev.get("cycle_in_episode") is not None and cycle_in_episode is not None and int(prev.get("cycle_in_episode"))+1==int(cycle_in_episode))
    except Exception:
        adjacent=False
    post_not_newer=bool(plant_time is not None and prev_pt is not None and plant_time+1e-9>=prev_pt)
    post_valid=bool(prev.get("valid_for_next_cycle_correlation") is True and prev.get("actuation_executed") is True)
    execution_correlated=bool(monotonic and post_valid and same_episode and adjacent and post_not_newer)
    if plant_time is None:
        reason="plant_time_missing"
    elif not monotonic:
        reason="plant_time_regression"
    elif not prev:
        reason="no_prior_realized_execution"
    elif not same_episode:
        reason="prior_execution_episode_mismatch"
    elif not adjacent:
        reason="prior_execution_not_immediately_preceding_cycle"
    elif not post_valid:
        reason="prior_execution_not_valid_for_correlation"
    elif not post_not_newer:
        reason="current_observation_older_than_post_execution"
    else:
        reason="correlated_to_previous_realized_execution"
    state_hash=sha256_obj(raw) if raw else None
    return {
        "schema":"TDOC-current-online-observation-v1",
        "transaction_id":str(transaction_id),"episode_id":episode_id,"cycle_in_episode":cycle_in_episode,
        "source":str(ingress.get("source") or "Layer6 runtime ingress"),"source_schema":ingress.get("schema"),
        "plant_time_s":plant_time,"plant_time_present":plant_time is not None,
        "previous_current_plant_time_s":prev_current,"plant_time_monotonic":monotonic,
        "previous_post_execution_transaction_id":prev.get("transaction_id"),
        "previous_post_execution_plant_time_s":prev_pt,"previous_execution_same_episode":same_episode,
        "previous_execution_adjacent_cycle":adjacent,"execution_correlated":execution_correlated,
        "fresh_for_control_state":execution_correlated,
        "classification":"EXECUTION_CORRELATED_CURRENT" if execution_correlated else "CURRENT_DIAGNOSTIC_ONLY",
        "correlation_reason":reason,"state_hash":state_hash,"released":bool(state.get("released")),
        "simulation_only":True,"not_field_evidence":True,"authority_class":"DIAGNOSTIC_EXECUTION_STATE",
        "navigation_authority":"MESA_STD","truth_injection_to_mesa":False,
        "wingmpet_alignment_allowed":True,"mesa_observation_allowed":False,
    }

def _post_execution_observation_contract(*, layer6_output:dict[str,Any]|None, transaction_id:str, episode_id:Any=None,
                                          cycle_in_episode:Any=None)->dict[str,Any]|None:
    """Return a next-cycle correlation anchor only for realized Layer6 execution."""
    out=dict(layer6_output or {})
    if str(out.get("status") or "").upper()!="EXECUTED" or out.get("actuation_executed") is not True:
        return None
    ev=dict(out.get("execution_evidence") or {})
    snap=ev.get("io_snapshot") if isinstance(ev.get("io_snapshot"),dict) else None
    if not snap: return None
    plant_time=_finite_plant_time(snap)
    return {
        "schema":"TDOC-post-execution-observation-v1",
        "transaction_id":str(transaction_id),"episode_id":episode_id,"cycle_in_episode":cycle_in_episode,
        "source":str(ev.get("source") or "Layer6")+" execution_evidence.io_snapshot",
        "plant_time_s":plant_time,"plant_time_present":plant_time is not None,
        "actuation_executed":True,"layer6_status":"EXECUTED",
        "valid_for_next_cycle_correlation":plant_time is not None,
        "state_hash":sha256_obj(snap),"simulation_only":True,"not_field_evidence":True,
        "navigation_authority":"MESA_STD","truth_injection_to_mesa":False,
    }

def vendored_external_binaries(package_root,active_roots=()):
    """Audit archives are not installed backends; configured active roots still count."""
    import os
    from pathlib import Path
    root=Path(package_root).resolve();active=[Path(p).resolve() for p in active_roots if p is not None]
    found=[]
    for base,dirs,files in os.walk(root):
        current=Path(base)
        if current==root:
            dirs[:]=[d for d in dirs if d not in {'reports','logs'} or any(p.is_relative_to(root/d) for p in active)]
        for name in files:
            p=current/name
            if p.is_file() and (p.suffix.lower() in ('.fmu','.tif','.tiff') or p.name.endswith(('.tar.gz','.tgz'))):found.append(str(p))
    return found


class TDOCOrchestrator:
    """Normative owner of Ready -> Trigger -> Invoke -> VersionedRoute for TDDT runtime."""
    OWNER="TDOC/RuntimeScheduler"
    def bind_native_prediction(self,model,binding_path,proposal):
        """Composition-time Python binding; JSON/runtime flags cannot install a model.

        Installation grants no qualification or actuation. Every transaction
        still passes the current issuer, safety gates and independent verifier.
        """
        from .native_production import NativeProductionLane
        if getattr(self,'native_prediction_lane',None) is not None:
            raise ValueError('Native prediction already composed; no silent model replacement')
        lane=NativeProductionLane(self.config,self.adapters,model,binding_path,proposal)
        result=lane.install()
        self.native_prediction_lane=lane
        return result

    def __init__(self,config):
        self.config=config; self.registry=LayerRegistry(config); self.registry.build(); self.dep=DependencyChecker(config); self.gate=InvocationGate()
        self.scheduler=TDOCScheduler(config.get("scheduler.fast_dt_s",1.0),config.get("scheduler.meso_fast_steps",3),config.get("scheduler.slow_meso_windows",2))
        self.provenance=ProvenanceLog(config.runtime_root/str(config.get("runtime.provenance","events/invocations.jsonl")))
        self.versions=VersionManager(config.runtime_root/"versions",config.orchestration_version); self.health=HealthManager()
        self.twin_registry=TwinRegistry(config.runtime_root/"registry/twins.json"); self.network_state=NetworkStateMirror(config.runtime_root/"network/network_state.json")
        self.feedback_router=FeedbackRouter(config.runtime_root/"feedback/layer7")
        chunk_records=int(config.get("runtime.storage.chunk_records",64))
        chunk_bytes=int(config.get("runtime.storage.chunk_uncompressed_bytes",1048576))
        self.knowledge=KnowledgeLedger(config.runtime_root/"knowledge/ledger",chunk_records=chunk_records,chunk_bytes=chunk_bytes)
        self.offline_handoff=OfflineKnowledgeHandoff(config.runtime_root)
        self.knowledge_policy=KnowledgePolicyCompiler(config)
        self.layer_archive=LayerRuntimeArchive(config.runtime_root,max_records=chunk_records,max_uncompressed_bytes=max(chunk_bytes,2097152))
        self.unit_adapter=UnitAdapterBinding(config)
        self.adapters={2:Layer2Adapter(config,self.unit_adapter)}
        self.adapters[3]=Layer3Adapter(config,self.unit_adapter,self.adapters[2]); self.adapters[5]=Layer5Adapter(config)
        self.adapters[4]=Layer4Adapter(config,self.adapters[3],self.adapters[5])
        # Certificate parsing/index construction is deterministic startup work, not
        # part of the first L4 control transaction.  Keeping it outside the cycle
        # monotonic clock prevents a cold-cache deadline miss from changing the
        # closed-loop trajectory before certificate reuse can become effective.
        self.layer4_startup_preparation=self.adapters[4].prepare_critical_path()
        self.adapters[6]=Layer6OnlineAdapter(config,self.adapters[5])
        if config.get('layers.layer6.online.execution_profile')=='closed_loop_sitl':
            # MicroMet preparation precedes inertial propagation. Refresh the
            # measured observation after that work using the existing Layer2
            # callback; the original climate timestamp remains unchanged.
            self.adapters[2].native_observation_source=self.adapters[6].online_runtime_ingress
            self.adapters[2].native_history_observation_source=self.adapters[6].online_history_ingress
            self.adapters[5].bind_native_mission(status_source=self.adapters[6].current_online_status,
                environment_validator=self.adapters[6].validated_environment_status)
        from .model_compatibility import integrated_native,capture_model_dependencies
        self.rtmo_l4_artifacts=RTMOL4ArtifactGate(
            model_dependencies_provider=(lambda:capture_model_dependencies(config)) if integrated_native(config) else None,
            max_age_s=float(config.get("tdoc.rtmo.l4_artifacts.max_age_s",30.0)),
            position_tolerance_m=float(config.get("tdoc.rtmo.l4_artifacts.position_tolerance_m",20.0)),
            velocity_tolerance_m_s=float(config.get("tdoc.rtmo.l4_artifacts.velocity_tolerance_m_s",4.0)),
        )
        self.rtmo_control_latency=RTMOControlLatencyTracker(
            deadline_s=float(config.get("tdoc.rtmo.realtime.max_control_period_s",0.1)),
            max_samples=int(config.get("tdoc.rtmo.realtime.latency_window_samples",20000)),
            min_samples_for_claim=int(config.get("tdoc.rtmo.realtime.min_executed_samples_for_p99_claim",1000)),
        )
        self.online_persistence=None
        if not bool(config.get("tdoc.rtmo.persistence.hot_path_sync_heavy_writes",False)):
            self.online_persistence=OnlinePersistenceLane(
                knowledge=self.knowledge, layer_archive=self.layer_archive, runtime_root=config.runtime_root,
                max_queue=int(config.get("tdoc.rtmo.persistence.max_queue_records",256)),
            )
        self._layer7_context={}
        self._online_prior_cache:dict[str,Any]|None=None
        self._last_current_online_plant_time_s=None
        self._last_post_execution_observation=None
        # A successful Layer7 evaluation may legitimately produce NO_CHANGE.  Keep
        # that completion acknowledgement in memory for exactly the next L2 cycle so
        # the closed-loop edge L7->L2 is explicit without inventing or persisting a
        # knowledge update.
        self._pending_l7_l2_ack:dict[str,Any]|None=None
        self.adapters[7]=Layer7Adapter(config,validation_callback=self._layer7_validate_candidate,
                                       commit_callback=self._layer7_commit_candidate,rollback_callback=self._layer7_rollback)
        self._seq=0
    def _event(self,tx,source,dest,etype,version,boundary,reason,status,details=None):
        self._seq+=1; e=InvocationEvent(f"{tx}:{self._seq:04d}",tx,self.OWNER,source,dest,etype,version,boundary,reason,status,utc_now(),details=details or {}); return self.provenance.append(e)
    def online_control_readiness(self)->dict[str,Any]:
        l5=self.adapters.get(5)
        if l5 is None or not callable(getattr(l5,"online_control_readiness",None)):
            return {"ok":False,"status":"NOT_READY","legacy_status":"NOT_READY","infrastructure_status":"INFRASTRUCTURE_NOT_READY",
                    "readiness_scope":"CONTROL_INFRASTRUCTURE_MAPPING_COMMISSIONING_ONLY","mission_executability_authority":False,
                    "reason":"layer5_online_control_readiness_unavailable","blockers":["layer5_online_control_readiness_unavailable"]}
        return l5.online_control_readiness()

    def prepare_online_transport_session(self)->dict[str,Any]:
        l6=self.adapters.get(6)
        if l6 is None or not callable(getattr(l6,"prepare_online_transport_session",None)):
            return {"ok":False,"reason":"layer6_online_transport_prepare_unavailable"}
        return l6.prepare_online_transport_session()

    def prepare_online_control_session(self)->dict[str,Any]:
        l6=self.adapters.get(6)
        if l6 is None or not callable(getattr(l6,"prepare_online_control_session",None)):
            return {"ok":False,"reason":"layer6_online_control_prepare_unavailable"}
        return l6.prepare_online_control_session()

    def abort_online_control_session(self,reason:str="tdoc_online_control_abort")->dict[str,Any]:
        l6=self.adapters.get(6)
        if l6 is None or not callable(getattr(l6,"abort_online_control_session",None)):
            return {"ok":False,"reason":"layer6_online_control_abort_unavailable"}
        return l6.abort_online_control_session(reason)

    def commission_online_control(self)->dict[str,Any]:
        """Commission PT_LIB/ArduPilot nominal control path and bind evidence to Layer5."""
        l6=self.adapters.get(6)
        if l6 is None or not callable(getattr(l6,"commission_online_control",None)):
            return {"ok":False,"reason":"layer6_online_control_commission_unavailable"}
        out=l6.commission_online_control()
        out["readiness_after"]=self.online_control_readiness()
        return out

    def preflight(self)->dict[str,Any]:
        deps=self.dep.check(); adapters={str(n):a.preflight() for n,a in self.adapters.items()}; reg=self.registry.snapshot(); h=self.health.evaluate(reg,deps); unit_adapter=self.unit_adapter.preflight()
        active_roots=[self.config.path_value(k) for k in ('TDDT_TWINS_ROOT','TDDT_CLIMATE_ROOT','TDDT_BOULDER_ROOT','TDDT_LAYER3_ROOT','TDDT_LAYER4_ROOT','TDDT_LAYER5_ROOT','TDDT_LAYER6_ROOT','TDDT_LAYER7_ROOT')]
        vendored=vendored_external_binaries(self.config.package_root,active_roots);no_vendor=not vendored
        base_ok=bool(deps["required_ok"] and unit_adapter.get("ok") and all(adapters[str(n)].get("ok",False) for n in (2,3,4,5,7)) and no_vendor)
        offline_ready=bool(base_ok)
        online_ready=bool(base_ok and adapters["6"].get("online_ready",False) and adapters["7"].get("ok",False))
        control_readiness=self.online_control_readiness() if base_ok else {"ok":False,"status":"NOT_READY","legacy_status":"NOT_READY","infrastructure_status":"INFRASTRUCTURE_NOT_READY",
            "readiness_scope":"CONTROL_INFRASTRUCTURE_MAPPING_COMMISSIONING_ONLY","mission_executability_authority":False,
            "reason":"base_preflight_not_ready","blockers":["base_preflight_not_ready"]}
        return {"ok":base_ok,"required_ok":deps["required_ok"],"dependencies":deps["dependencies"],"unit_adapter":unit_adapter,"adapters":adapters,"registry":reg,"health":h,
                "no_vendored_external_binaries":no_vendor,"vendored_external_binaries":vendored,"binary_audit_scope":"installed package; non-active reports/logs excluded","invocation_owner":self.OWNER,"execution_modes":{"OFFLINE_SE":offline_ready,"ONLINE_JSBSIM":online_ready,"ONLINE_JSBSIM_CONTROL_TRAINING":bool(online_ready and control_readiness.get("ok")),"ONLINE_JSBSIM_OBSERVATION":online_ready},
                "online_control_readiness":control_readiness,
                "layer6_scope":"ONLINE_JSBSIM_ONLY","layer7_scope":"TDOC_MANAGED_ONLINE_FEEDBACK_SIDE_MODULE",
                "offline_route":"Layer5 -> TDOC -> Layer2",
                "online_loop":"Layer2 -> Layer3 -> Layer4 -> Layer5 -> Layer6 -> Layer7 -> TDOC VersionedRoute -> {Layer2,Layer3.1,Layer3.2,Layer3.3}"}
    def _invoke(self,layer,tx,source,payload,version,boundary,readiness):
        if not readiness.ready:
            self._event(tx,source,f"Layer{layer}","Hold",version,boundary,"readiness_failed","HELD",readiness.to_dict()); return {"ok":False,"status":"HELD","readiness":readiness.to_dict()}
        self._event(tx,source,f"Layer{layer}","Invoke",version,boundary,"ready_gate_passed","STARTED",readiness.to_dict())
        started=time.perf_counter()
        try:
            out=self.adapters[layer].invoke(payload); elapsed=time.perf_counter()-started
            layer_request=(payload.get("request") or {}) if isinstance(payload,dict) else {}
            pre_release_segment=bool(
                layer==4 and isinstance(layer_request,dict)
                and layer_request.get("rtmo_pre_release_verify") is True
            )
            offline_full_horizon=bool(
                layer==4 and isinstance(layer_request,dict)
                and str(layer_request.get("learning_mode") or "").upper()=="OFFLINE_SE"
            )
            # OFFLINE_SE runs the same non-actuating, full-horizon L2/L3/WingMPET
            # verification lane as Online pre-release.  It must not be classified as
            # the <2 s committable control segment: doing so discards valid verified
            # candidates before Layer5 and prevents the documented offline loop from
            # ever closing.  The existing full-horizon deadline remains fail-closed;
            # this changes only which already-defined timing contract applies.
            non_actuating_full_horizon=bool(pre_release_segment or offline_full_horizon)
            deadline_key=("deadlines.layer4_pre_release_s" if non_actuating_full_horizon
                          else f"deadlines.layer{layer}_s")
            deadline=self.config.get(deadline_key)
            deadline_ok=bool(deadline is not None and float(deadline)>0.0 and elapsed<=float(deadline))
            timing_segment=(
                "NON_ACTUATING_PRE_RELEASE_FULL_HORIZON" if pre_release_segment else
                "NON_ACTUATING_OFFLINE_FULL_HORIZON" if offline_full_horizon else
                "CONTROL_TRANSACTION"
            )
            self._event(tx,f"Layer{layer}","TDOC","Route",version,boundary,"output_ready","OK" if deadline_ok else "DEADLINE_EXCEEDED",{"output_hash":sha256_obj(out),"elapsed_s":elapsed,"deadline_s":deadline,"deadline_key":deadline_key,"deadline_ok":deadline_ok,"timing_segment":timing_segment})
            out["orchestration_timing"]={"elapsed_s":elapsed,"deadline_s":deadline,"deadline_key":deadline_key,"deadline_ok":deadline_ok,"clock":"monotonic","timing_segment":timing_segment,"actuation_forbidden":non_actuating_full_horizon}
            if not deadline_ok:
                out["original_status"]=out.get("status")
                out["ok"]=False; out["status"]="DEADLINE_EXCEEDED"; out["reason"]=f"layer{layer}_deadline_exceeded"
                out["late_output_discarded"]=True
            return out
        except Exception as exc:
            elapsed=time.perf_counter()-started; self._event(tx,f"Layer{layer}","TDOC","Alarm",version,boundary,"invoke_exception","FAILED",{"error":repr(exc),"elapsed_s":elapsed}); return {"ok":False,"status":"FAILED","error":repr(exc),"orchestration_timing":{"elapsed_s":elapsed}}
    def _route_offline_feedback_to_layer2(self,tx,feedback,request,version,boundary,statuses):
        replay=copy.deepcopy(request); replay.pop("transaction_id",None)
        ctx=dict(replay.get("context") or {}); ctx["layer5_training_feedback"]=feedback; ctx["training_mode"]="OFFLINE_SE"; replay["context"]=ctx; replay["training_feedback"]=feedback
        pf=self.adapters[2].preflight(); r=self.gate.evaluate(2,valid=bool(feedback),version_ok=True,boundary_ok=True,health_ok=pf.get("ok",False),policy_route=self.config.layer_enabled(2),evidence={"route":"Layer5->TDOC->Layer2","feedback_hash":feedback.get("feedback_hash")})
        statuses["2_offline_feedback"]=r.to_dict(); return self._invoke(2,tx,"Layer5-offline-training-feedback",replay,version,boundary,r)

    def _layer7_validate_candidate(self, req:dict[str,Any])->dict[str,Any]:
        """TDOC-owned candidate-specific shadow/stability handshake; fail closed without bound evidence."""
        ctx=self._layer7_context or {}; user=dict((ctx.get("request") or {}).get("layer7_candidate_validation") or {})
        base=str(req.get("base_version","")); ch=str(req.get("candidate_hash",""))
        if user and str(user.get("base_version",""))==base and str(user.get("candidate_hash",""))==ch:
            out=dict(user); out["base_version"]=base; out["candidate_hash"]=ch; out["validation_owner"]="TDOC/RuntimeScheduler"; return out
        snap=ctx.get("snapshot") or {}; margin=snap.get("consistency_margin",0.0)
        return {"base_version":base,"candidate_hash":ch,"stability_ok":False,"coverage_ok":False,
                "active_metrics":{},"candidate_metrics":{},"violation_count":1,"sample_count":0,"margin":float(margin or 0.0),
                "validation_owner":"TDOC/RuntimeScheduler","reason":"candidate_bound_external_shadow_validation_not_supplied_fail_closed"}

    @staticmethod
    def _numeric_map(obj):
        out={}
        if not isinstance(obj,dict): return out
        for k,v in obj.items():
            try: out[str(k)]=float(v); continue
            except Exception: pass
            if isinstance(v,dict):
                for q in ("value","weight","final","K","coupling"):
                    try: out[str(k)]=float(v[q]); break
                    except Exception: pass
        return out

    def _translate_layer7_candidate(self, payload:dict[str,Any])->dict[str,Any]:
        cand=dict(payload.get("candidate") or {}); ctx=self._layer7_context or {}; snap=dict(ctx.get("snapshot") or {})
        adaptation={"version":str(payload.get("candidate_version")),"layer7_candidate_hash":payload.get("candidate_hash"),"layer7_candidate":cand}
        # W/K deltas are converted into enforceable Layer-3 policy bounds for the next version.
        eng=self.adapters[3].engine
        policy=copy.deepcopy(getattr(eng,"current_slow_policy",{}) or {})
        wcur=self._numeric_map(snap.get("weights") or {}); wb=copy.deepcopy(policy.get("weight_bounds") or {})
        for k,d in dict(cand.get("delta_w") or {}).items():
            target=max(0.0,min(1.0,float(wcur.get(str(k),0.0))+float(d))); wb[str(k)]=[target,target]
        if cand.get("delta_w"): adaptation["weight_bounds"]=wb
        kcur=self._numeric_map(snap.get("couplings") or {}); kb=copy.deepcopy(policy.get("coupling_bounds") or {})
        for k,d in dict(cand.get("delta_k") or {}).items():
            target=max(0.0,min(1.0,float(kcur.get(str(k),0.0))+float(d))); kb[str(k)]=[target,target]
        if cand.get("delta_k"): adaptation["coupling_bounds"]=kb
        desired={tuple(map(str,x[:2])) for x in (cand.get("graph_candidate") or []) if isinstance(x,(list,tuple)) and len(x)>=2}
        if desired and eng is not None:
            changes={}
            for key,e in eng.topology.edges.items(): changes[str(key)]=(str(e.source),str(e.target)) in desired
            adaptation["topology_changes"]=changes
        for src,dst in (("delta_omega","layer7_delta_omega"),("delta_epsilon","layer7_delta_epsilon"),("delta_lambda_c","layer7_delta_lambda_c"),("delta_policy","layer7_delta_policy"),("delta_constraints","layer7_delta_constraints")):
            if cand.get(src): adaptation[dst]=copy.deepcopy(cand[src])
        return adaptation

    def _layer7_commit_candidate(self, payload:dict[str,Any])->dict[str,Any]:
        validation=dict(payload.get("validation") or {}); required=("evidence_ok","ess_ok","route_ok","bounds_ok","stability_ok","shadow_ok","governance_ok","boundary_ok","coverage_ok")
        if not all(bool(validation.get(k)) for k in required): return {"accepted":False,"reason":"TDOC safe-commit gate not satisfied"}
        if str(payload.get("base_version"))!=str((self._layer7_context or {}).get("snapshot_version")): return {"accepted":False,"reason":"base_version_mismatch"}
        adaptation=self._translate_layer7_candidate(payload)
        authorization=f"TDOC-L7-governance:{payload.get('candidate_hash')}" if validation.get("governance_ok") else None
        result=self.adapters[3].commit_layer7(adaptation,authorization=authorization)
        if not result.get("committed"): return {"accepted":False,"reason":result.get("reason","Layer3 safe commit rejected")}
        version=str((result.get("policy") or {}).get("version") or payload.get("candidate_version"))
        self.versions.activate_external(version,mark_safe=False)
        return {"accepted":True,"version":version,"committed_version":version,"tdoc_owner":self.OWNER}

    def _layer7_rollback(self, payload:dict[str,Any])->dict[str,Any]:
        target=str(payload.get("target_version","")); result=self.adapters[3].rollback_layer7(reason=str(payload.get("reason","Layer7 rollback")))
        restored=str((result.get("policy") or {}).get("version") or result.get("version") or target)
        if target and restored!=target: return {"accepted":False,"reason":f"rollback_target_mismatch:{restored}!={target}"}
        self.versions.restore_external(restored); return {"accepted":True,"version":restored,"restored_version":restored,"tdoc_owner":self.OWNER}

    @staticmethod
    def _euler_quaternion(roll:float,pitch:float,yaw:float)->tuple[float,float,float,float]:
        cr,sr=math.cos(roll/2.0),math.sin(roll/2.0); cp,sp=math.cos(pitch/2.0),math.sin(pitch/2.0); cy,sy=math.cos(yaw/2.0),math.sin(yaw/2.0)
        return (cr*cp*cy+sr*sp*sy, sr*cp*cy-cr*sp*sy, cr*sp*cy+sr*cp*sy, cr*cp*sy-sr*sp*cy)

    def _online_simulation_state_ingress(self, phase:Any=None)->dict[str,Any]:
        """Canonicalize fresh native Layer6 GUI telemetry before Layer2.

        This diagnostic simulation state never becomes MESA navigation authority.
        """
        ingress=self.adapters[6].online_runtime_ingress()
        if not isinstance(ingress,dict) or not ingress.get("ok"):
            return {"ok":False,"reason":"layer6_online_runtime_ingress_not_ready","ingress":ingress,"simulation_only":True,"not_field_evidence":True}
        raw=dict(ingress.get("state") or {}); init=dict(ingress.get("initialization_contract") or {}); env=dict(ingress.get("environment_contract") or {})
        required_live=("latitude_deg","longitude_deg","altitude_msl_m","airspeed_m_s",
                       "velocity_n_m_s","velocity_e_m_s","velocity_d_m_s","roll_rad","pitch_rad","yaw_rad")
        if any(not self.adapters[6]._finite(raw.get(k)) for k in required_live):
            return {"ok":False,"reason":"online_live_telemetry_incomplete","ingress":ingress}
        if any(init.get(k) is None for k in ("latitude_deg","longitude_deg","altitude_msl_m","heading_deg","initial_airspeed_m_s")):
            return {"ok":False,"reason":"online_observed_origin_incomplete","ingress":ingress}
        def finite(name,default=None):
            try:
                v=float(raw.get(name)); return v if math.isfinite(v) else default
            except Exception:return default
        # Position is always taken from the FMU façade; while held it equals the exact
        # initialized geospatial state. Convert WGS84 delta through the validated TDOC
        # ISO adapter, not a local hand-written frame transform.
        lat=finite("latitude_deg",float(init["latitude_deg"])); lon=finite("longitude_deg",float(init["longitude_deg"])); alt=finite("altitude_msl_m",float(init["altitude_msl_m"]))
        try:
            self.unit_adapter.activate()
            from tdoc_unit_converter import adapters as iso
            o=iso.wgs84_to_ecef(math.radians(float(init["latitude_deg"])),math.radians(float(init["longitude_deg"])),float(init["altitude_msl_m"]))
            c=iso.wgs84_to_ecef(math.radians(lat),math.radians(lon),alt)
            enu=iso.ecef_delta_to_enu([float(c[i])-float(o[i]) for i in range(3)],math.radians(float(init["latitude_deg"])),math.radians(float(init["longitude_deg"])))
            ned=iso.enu_to_ned(enu); pn,pe,pd=[float(x) for x in ned]
            if init.get("map_crs"):
                # MESA/Layer3 mission positions use the map CRS, with N/E order
                # and absolute down-height. Local zero-NED cannot be mixed with it.
                e,n,h=iso.transform_crs(lon,lat,alt,source_crs="EPSG:4979",target_crs=init["map_crs"])
                pn,pe,pd=float(n),float(e),-float(h)
        except Exception as exc:
            return {"ok":False,"reason":"online_WGS84_to_NED_conversion_failed","error":repr(exc),"ingress":ingress,"simulation_only":True,"not_field_evidence":True}
        released=bool(ingress.get("released")); raw_va=finite("airspeed_m_s",0.0) or 0.0
        raw_v=(finite("velocity_n_m_s",0.0) or 0.0,finite("velocity_e_m_s",0.0) or 0.0,finite("velocity_d_m_s",0.0) or 0.0)
        use_live=bool(released and raw_va>=0.0 and all(math.isfinite(x) for x in raw_v))
        if use_live:
            vn,ve,vd=raw_v; va=raw_va; state_source="Layer6Client.GUI.telemetry"
            roll=finite("roll_rad",0.0) or 0.0; pitch=finite("pitch_rad",0.0) or 0.0; yaw=finite("yaw_rad",math.radians(float(init["heading_deg"])))
        else:
            # Exact JSBSim RunIC contract: gamma=0 and psi=initial_heading.  Wind is a
            # configured physics input, therefore ground velocity is Va-track + wind.
            psi=math.radians(float(init["heading_deg"])); va=float(init["initial_airspeed_m_s"])
            wind=list(env.get("wind_ned_m_s") or [0.0,0.0,0.0]); wind=(wind+[0.0,0.0,0.0])[:3]
            vn=va*math.cos(psi)+float(wind[0]); ve=va*math.sin(psi)+float(wind[1]); vd=float(wind[2])
            from tddt_tdoc.phoenix_frames import fallback_attitude
            att=fallback_attitude(phase,yaw_rad=psi,kinematic_pitch_rad=0.0)
            roll=float(att["roll_rad"]); pitch=float(att["pitch_rad"]); yaw=float(att["yaw_rad"])
            state_source="TDOC_to_PT_LIB_RunIC_initialization_contract+Phoenix_phase_attitude"
        qw,qx,qy,qz=self._euler_quaternion(float(roll),float(pitch),float(yaw))
        act=dict(ingress.get("actuator_feedback") or {}); limits=self.adapters[5].phoenix_runtime_physical_limits(); omega_max=float(limits.get("motor_speed_rad_s",0.0) or 0.0)
        lm=float(act.get("left_motor_norm",0.0) or 0.0); rm=float(act.get("right_motor_norm",0.0) or 0.0)
        # Rotor speeds are explicitly command-based model-state estimates, never sensor
        # measurements.  They prevent a fictitious zero-RPM spool transient in L4 FF.
        omega_left=max(0.0,min(1.0,lm))*omega_max; omega_right=max(0.0,min(1.0,rm))*omega_max
        if not use_live:
            air_velocity_ned=[va*math.cos(float(yaw)),va*math.sin(float(yaw)),0.0]
            air_velocity_source="RunIC heading + initial true airspeed"
        else:
            # JSBSim/PT_LIB exposes ground velocity and simulator-truth wind separately.
            # Preserve the realized air-relative state explicitly so hypothetical L4
            # full-fidelity scenarios can substitute their own wind without creating
            # an artificial airspeed jump from mixing live Vg with scenario W.
            tw=(finite("truth_wind_n_m_s",None),finite("truth_wind_e_m_s",None),finite("truth_wind_d_m_s",None))
            if all(x is not None and math.isfinite(float(x)) for x in tw):
                air_velocity_ned=[vn-float(tw[0]),ve-float(tw[1]),vd-float(tw[2])]
                air_velocity_source="PT_LIB live ground velocity - JSBSim truth wind"
            else:
                air_velocity_ned=None
                air_velocity_source="unavailable_without_truth_wind"
        phys={"pn":pn,"pe":pe,"pd":pd,"vn":vn,"ve":ve,"vd":vd,"qw":qw,"qx":qx,"qy":qy,"qz":qz,
              "p_rate":finite("gyro_x_rad_s",0.0) or 0.0,"q_rate":finite("gyro_y_rad_s",0.0) or 0.0,"r_rate":finite("gyro_z_rad_s",0.0) or 0.0,
              "omega_left":omega_left,"omega_right":omega_right,"soc":finite("battery_soc",1.0),
              "released":released,"kinematics_source":state_source,"airspeed_m_s":va,"air_velocity_ned_m_s":air_velocity_ned,"air_velocity_source":air_velocity_source}
        if ingress.get("source")=="Layer6/GUI/MAVLink" and finite("battery_soc",None) is None:
            return {"ok":False,"reason":"online_native_battery_soc_unavailable","ingress":ingress}
        numeric_keys=("pn","pe","pd","vn","ve","vd","qw","qx","qy","qz","p_rate","q_rate","r_rate","omega_left","omega_right","soc","airspeed_m_s")
        if not all(math.isfinite(float(phys[k])) for k in numeric_keys):
            return {"ok":False,"reason":"online_physical_state_non_finite","simulation_only":True,"not_field_evidence":True}
        return {"ok":True,"schema":"TDOC-online-canonical-simulation-state-v1","state_source":state_source,"released":released,"raw_airspeed_m_s":raw_va,
                "online_physical_state":phys,"ptlib_ingress":ingress,"simulation_only":True,"not_field_evidence":True,
                "rotor_speed_semantics":"command-based estimate from ArduPilot actuator feedback and source-bound Phoenix omega_max; not measured RPM",
                "conversion_authority":"TDOC ISO adapter / Layer3.1 contract"}

    def run_cycle(self,request:dict[str,Any])->CycleResult:
        cycle_started_monotonic_s=time.perf_counter()
        request=copy.deepcopy(request)
        tx=str(request.get("transaction_id") or f"tx-{uuid.uuid4().hex[:12]}")
        request["transaction_id"]=tx
        current_online_observation=None
        post_execution_observation=None
        mode_ingress=str(request.get("learning_mode") or self.config.get("layers.layer5.default_learning_mode","OFFLINE_SE")).upper()
        if mode_ingress=="ONLINE_JSBSIM" and not bool(request.get("online_observation_only")):
            ctx0=dict(request.get("context") or {})
            rc0=(ctx0.get("rtmo_run_context") or ctx0.get("tdoc_run_context") or {}) if isinstance(ctx0,dict) else {}
            rc0=rc0 if isinstance(rc0,dict) else {}
            phase0=rc0.get("phase_id") or request.get("stage") or ctx0.get("flight_phase")
            online_state=self._online_simulation_state_ingress(phase=phase0)
            current_online_observation=_current_online_observation_contract(
                online_state=online_state,transaction_id=tx,episode_id=request.get("online_episode_id"),
                cycle_in_episode=request.get("online_cycle_in_episode"),
                previous_current_plant_time_s=self._last_current_online_plant_time_s,
                previous_post_execution=self._last_post_execution_observation,
            )
            if current_online_observation.get("plant_time_present") and current_online_observation.get("plant_time_monotonic"):
                self._last_current_online_plant_time_s=float(current_online_observation["plant_time_s"])
            ctx=dict(request.get("context") or {}); ctx["online_state_ingress"]=online_state; ctx["current_online_observation"]=copy.deepcopy(current_online_observation)
            rtmo_enabled=bool(self.config.get("tdoc.rtmo.enabled",False))
            native_transport=self.config.get("layers.layer6.online.transport")=="Layer6Client_GUI_MAVLink"
            if online_state.get("ok"):
                if rtmo_enabled or native_transport:
                    # RTMO-I4: JSBSim/PT_LIB truth remains execution/shadow evidence and
                    # must not be injected as an independent GNSS-denied MESA observation.
                    # WingMPET may still consume this simulation state for plant/model
                    # alignment under an explicitly simulation-only channel.
                    ctx["online_execution_state"]=dict(online_state["online_physical_state"])
                    ctx["rtmo_data_authority"]={"navigation":"MESA_STD","simulator_truth":"DIAGNOSTIC_EXECUTION_ONLY","truth_injection_to_mesa":False}
                else:
                    ctx["online_physical_state"]=dict(online_state["online_physical_state"])
            else:
                ingress=dict(online_state.get("ingress") or {}) if isinstance(online_state,dict) else {}
                if not ingress.get("compatibility_test_double"):
                    # Control-training must fail closed when the realized/initialized SITL
                    # state cannot be coherently routed into L2/L3 before optimization.
                    raise RuntimeError(f"online simulation-state ingress required but failed: {online_state}")
                ctx["online_state_ingress_compatibility"]="pytest validation double only; production remains fail-closed"
            request["context"]=ctx
        current_run_context=(dict((request.get("context") or {}).get("rtmo_run_context") or
                                  (request.get("context") or {}).get("tdoc_run_context") or {})
                             if isinstance(request.get("context"),dict) else {})
        # Persistent storage is an audit trail; Online adaptation is scoped to the
        # current run/episode so a newly started flight cannot consume stale L7 evidence.
        pending_l7=self.feedback_router.pending_context(
            run_context=current_run_context if mode_ingress=="ONLINE_JSBSIM" else None)
        if mode_ingress=="ONLINE_JSBSIM" and isinstance(self._pending_l7_l2_ack,dict):
            ack=self._pending_l7_l2_ack
            ack_scope=FeedbackRouter._run_scope(ack.get("run_context"))
            same_episode=bool(
                ack_scope.get("run_id") and ack_scope.get("episode_id")
                and ack_scope.get("run_id") == current_run_context.get("run_id")
                and ack_scope.get("episode_id") == current_run_context.get("episode_id")
            )
            if same_episode and "Layer2" not in pending_l7:
                pending_l7=dict(pending_l7); pending_l7["Layer2"]=copy.deepcopy(ack)
            # One-shot synchronization evidence: it is neither retained by the
            # FeedbackRouter nor replayed as adaptation knowledge.
            self._pending_l7_l2_ack=None
        if pending_l7:
            ctx=dict(request.get("context") or {}); ctx["layer7_feedback"]=pending_l7; request["context"]=ctx
        ref=float(request.get("reference_time_s",0.0)); b=self.scheduler.boundary(ref); boundary=b.label(); ver=self.versions.active; statuses={}; self._event(tx,"DeviceInterface","TDOC","Data",ver,boundary,"qualified_ingress","OK")
        r2=self.gate.evaluate(2,valid=True,version_ok=True,boundary_ok=True,health_ok=self.adapters[2].preflight().get("ok",False),policy_route=self.config.layer_enabled(2)); statuses["2"]=r2.to_dict()
        o2=self._invoke(2,tx,"TDOC",request,ver,boundary,r2); packets=o2.get("packets") or {}
        # L2's registered provider consumes local typed sensor receipts. Other
        # layers receive their JSON evidence, never an installable local receipt.
        request['context']=jsonable(request.get('context') or {})
        statuses['2']['invocation']={k:o2[k] for k in ('ok','status','error','late_output_discarded','orchestration_timing') if k in o2}
        if getattr(self.adapters[2],'native_observation_source',None) is not None:
            observed_ref=float(request.get('reference_time_s',ref))
            if observed_ref!=ref:
                previous_ref=ref;ref=observed_ref;b=self.scheduler.boundary(ref);boundary=b.label()
                self._event(tx,'Layer2','TDOC','Route',ver,boundary,'native_current_observation_refresh','OK',
                    {'previous_source_time_s':previous_ref,'current_source_time_s':ref,
                     'climate_source_timestamp_preserved':True,'clock_rebased':False})
        twin_reg=self.twin_registry.observe(packets) if packets else {"ok":False,"count":0,"details":{}}; valid_packets=bool(o2.get("ok") and twin_reg.get("ok") and packets and all(isinstance(v,dict) for v in packets.values()))
        l3_pf=self.adapters[3].preflight()
        if pending_l7:
            try:self.adapters[3].apply_layer7_feedback(pending_l7)
            except Exception:pass
        r3=self.gate.evaluate(3,valid=valid_packets,version_ok=True,boundary_ok=True,health_ok=l3_pf.get("ok",False),policy_route=self.config.layer_enabled(3),evidence={"packet_count":len(packets),"preflight":l3_pf}); statuses["3"]=r3.to_dict()
        o3=self._invoke(3,tx,"Layer2-output",{"packets":packets,"request":request,"layer3_runtime_meta":o2.get("layer3_runtime_meta") or {}},ver,boundary,r3)
        statuses['3']['invocation']={k:o3[k] for k in ('ok','status','error','late_output_discarded','orchestration_timing') if k in o3}
        ua_binding=dict(o3.get("unit_adapter_binding") or {})
        if ua_binding:self._event(tx,"Layer3.1","TDOC","Route",ver,boundary,"iso_adapter_alignment_health","OK" if ua_binding.get("health_ok",True) else "FAILED",ua_binding)
        snap=o3.get("snapshot") or {}; snapshot_ready_monotonic_s=time.perf_counter(); prov=snap.get("provenance") or {}; atomic=prov.get("snapshot_kind") in (None,"atomic_layer3_network_snapshot") and bool(snap)
        network_mirror=self.network_state.update(snap) if snap else {}; snap_ver=str(snap.get("policy_version",ver)) if snap else ver
        mode=str(request.get("learning_mode") or self.config.get("layers.layer5.default_learning_mode","OFFLINE_SE")).upper()
        online_training_pre=bool(mode=="ONLINE_JSBSIM" and (request.get("online_training") or request.get("require_offline_knowledge")))
        knowledge_policy_gate={"ok":True,"status":"NOT_REQUIRED","reason":"offline_or_online_without_knowledge_prior"}
        l4_prior_admission={"ok":True,"status":"NOT_REQUIRED","actuation_authority":False}
        if online_training_pre:
            # Only the guide verified for this cycle/phase may reach prediction.
            # A request reused after a phase change must not retain an old guide.
            ctx=dict(request.get("context") or {}); ctx.pop("knowledge_policy",None)
            request["context"]=ctx
            early_handoff=self.offline_handoff.validate_online_start(request)
            if early_handoff.get("ok"):
                hm_early=dict(early_handoff.get("manifest") or {})
                policy_phase=str(request.get("stage") or "").strip().lower()
                if policy_phase in {"on_ground","touchdown"}:
                    knowledge_policy_gate={"ok":True,"status":"NOT_REQUIRED","reason":"passive_terminal_phase","phase":policy_phase}
                else:
                    knowledge_policy_gate=self.knowledge_policy.phase_policy(
                        policy_phase,handoff_hash=hm_early.get("handoff_hash"))
                    l4_prior_admission=optional_policy_admission(knowledge_policy_gate)
                    l4_prior_admission["readiness_scope"]="L4_PRIOR_INTEGRITY_ONLY; not prediction verification/flight readiness"
                    if l4_prior_admission.get("ok") and l4_prior_admission.get("policy_available"):
                        ctx=dict(request.get("context") or {})
                        ctx["knowledge_policy"]=copy.deepcopy(knowledge_policy_gate)
                        request["context"]=ctx
            else:
                knowledge_policy_gate={"ok":False,"status":"NOT_READY","reason":"offline_handoff_not_ready_for_policy",
                                       "handoff_gate":early_handoff}
                l4_prior_admission={"ok":False,"status":"NOT_READY","reason":"offline_handoff_not_ready_for_prediction",
                                    "actuation_authority":False}
        offline_model_evidence=getattr(self,"_offline_model_control_evidence",None)
        if mode=="OFFLINE_SE" and not (isinstance(offline_model_evidence,dict) and offline_model_evidence.get("ok")):
            derive=getattr(self.adapters.get(2),"derive_offline_phoenix_model_certificate",None)
            profile_sha=getattr(self.adapters.get(5),"phoenix_profile_sha256",None)
            airframe_sha=getattr(self.adapters.get(5),"phoenix_airframe_binding_sha256",None)
            apply_cert=getattr(self.adapters.get(5),"apply_offline_model_certificate",None)
            bind_model=getattr(self.adapters.get(5),"set_verification_model_certificate",None)
            seed_fn=getattr(self.adapters.get(5),"offline_vtol_seed_controls",None)
            if callable(derive) and callable(profile_sha) and callable(apply_cert) and callable(bind_model):
                ctx_fn=getattr(self.adapters.get(4),"_phoenix_hover_operating_context",None)
                cert_context=ctx_fn(snap) if callable(ctx_fn) and isinstance(snap,dict) else None
                phase_seeds=seed_fn(cert_context) if callable(seed_fn) else {"ok":False,"reason":"layer5_vertical_seed_api_unavailable"}
                cert=derive(profile_sha(),phase_seed_controls=phase_seeds,airframe_binding_sha256=(airframe_sha() if callable(airframe_sha) else None))
                applied=apply_cert(cert) if isinstance(cert,dict) and cert.get("ok") else {"ok":False,"reason":"offline_model_certificate_not_qualified"}
                # Layer4 FF uses the TDOC-side simulation-only action bridge, which
                # must be bound to the same qualified model certificate in OFFLINE_SE.
                # Applying the certificate only to external Layer5 does not populate
                # this bridge and previously caused phoenix_action_bridge_unvalidated.
                bound=bind_model(cert) if isinstance(cert,dict) and cert.get("ok") else {"ok":False,"reason":"verification_model_certificate_not_qualified"}
                offline_model_evidence={"ok":bool(isinstance(cert,dict) and cert.get("ok") and isinstance(applied,dict) and applied.get("ok") and isinstance(bound,dict) and bound.get("ok")),"certificate":cert,"layer5_apply":applied,"verification_bind":bound}
            else:
                offline_model_evidence={"ok":False,"certificate":None,"layer5_apply":{"ok":False,"reason":"offline_model_certificate_api_unavailable"},"verification_bind":{"ok":False,"reason":"verification_model_certificate_api_unavailable"},"reason":"offline_model_certificate_api_unavailable"}
            self._offline_model_control_evidence=offline_model_evidence
        if mode=="ONLINE_JSBSIM":
            # L4 full-fidelity uses a WingMPET-qualified *model* action bridge.  Keep
            # this evidence distinct from the PT_LIB commissioning certificate that
            # authorizes the real ArduPilot execution path.
            derive=getattr(self.adapters.get(2),"derive_offline_phoenix_model_certificate",None)
            bind_model=getattr(self.adapters.get(5),"set_verification_model_certificate",None)
            profile_sha=getattr(self.adapters.get(5),"phoenix_profile_sha256",None)
            airframe_sha=getattr(self.adapters.get(5),"phoenix_airframe_binding_sha256",None)
            seed_fn=getattr(self.adapters.get(5),"offline_vtol_seed_controls",None)
            if callable(derive) and callable(bind_model) and callable(profile_sha):
                if not isinstance(offline_model_evidence,dict) or not offline_model_evidence.get("ok"):
                    ctx_fn=getattr(self.adapters.get(4),"_phoenix_hover_operating_context",None)
                    cert_context=ctx_fn(snap) if callable(ctx_fn) and isinstance(snap,dict) else None
                    phase_seeds=seed_fn(cert_context) if callable(seed_fn) else {"ok":False,"reason":"layer5_vertical_seed_api_unavailable"}
                    cert=derive(profile_sha(),phase_seed_controls=phase_seeds,airframe_binding_sha256=(airframe_sha() if callable(airframe_sha) else None)); bound=bind_model(cert) if isinstance(cert,dict) and cert.get("ok") else {"ok":False,"reason":"verification_model_certificate_not_qualified"}
                    offline_model_evidence={"ok":bool(isinstance(cert,dict) and cert.get("ok") and isinstance(bound,dict) and bound.get("ok")),"certificate":cert,"verification_bind":bound}
                    self._offline_model_control_evidence=offline_model_evidence
                else:
                    bind_model((offline_model_evidence or {}).get("certificate"))
            ctx=dict(request.get("context") or {}); ctx["online_ff_model_control_evidence"]={"ok":bool((offline_model_evidence or {}).get("ok")),
                "certificate_schema":(((offline_model_evidence or {}).get("certificate") or {}).get("schema")),
                "scope":"L4_WINGMPET_FULL_FIDELITY_ONLY","simulation_only":True,"not_field_evidence":True,
                "execution_certificate_separate":True}; request["context"]=ctx
        if mode=="OFFLINE_SE":
            ctx=dict(request.get("context") or {}); ctx["offline_model_control_evidence"]={"ok":bool((offline_model_evidence or {}).get("ok")),
                "certificate_schema":(((offline_model_evidence or {}).get("certificate") or {}).get("schema")),
                "safety_semantics":"simulation/model-derived nominal zero-correction binding; no PT_LIB; no ONLINE readiness"}; request["context"]=ctx
        # Missing phase guidance cannot veto fresh current-state prediction.
        # Handoff/policy integrity, snapshot, health and route checks still apply;
        # candidate verification and Layer5/Layer6 release remain separate gates.
        r4=self.gate.evaluate(4,valid=bool(o3.get("ok") and atomic and l4_prior_admission.get("ok",False)),version_ok=bool(snap),boundary_ok=True,health_ok=self.adapters[4].preflight().get("ok",False),policy_route=self.config.layer_enabled(4),evidence={"snapshot_kind":prov.get("snapshot_kind"),"snapshot_version":snap_ver,"offline_model_control":offline_model_evidence if mode=="OFFLINE_SE" else None,"knowledge_policy_gate":knowledge_policy_gate,"prior_admission":l4_prior_admission}); statuses["4"]=r4.to_dict()
        pre_release_verify=bool(mode=="ONLINE_JSBSIM" and request.get("rtmo_pre_release_verify"))
        l4_artifact_use_metadata=None
        l4_prediction_snapshot=None
        # PRE_RELEASE_VERIFY must produce fresh current-state L4 evidence.  It therefore
        # bypasses the fast artifact-reuse lane, but remains non-actuating below.
        rtmo_fast_l4=bool(mode=="ONLINE_JSBSIM" and not pre_release_verify and self.config.get("tdoc.rtmo.enabled",False) and self.config.get("tdoc.rtmo.l4_artifacts.fast_path_reuse",True))
        passive_contact_boundary=bool(rtmo_fast_l4 and request.get("stage") in ("on_ground","touchdown"))
        if passive_contact_boundary:
            # These phases observe contact and plant time; they never actuate.
            # The verified artifact belongs to the imminent controlled phase.
            # Acquiring it as an on-ground artifact would reject that valid seed.
            o4={"ok":False,"status":"PASSIVE_BOUNDARY","result":None,
                "reason":"passive_contact_boundary_non_actuating"}
            self._event(tx,"RTMO-L4-ArtifactGate","Layer4","Hold",snap_ver,boundary,
                        "passive_contact_boundary_non_actuating","HELD",{})
        elif rtmo_fast_l4 and r4.ready:
            gate_t0=time.perf_counter()
            decision=self.rtmo_l4_artifacts.acquire(snapshot=snap,request=request,orchestration_version=self.versions.active,reference_time_s=ref)
            if decision.ok:
                l4_artifact_use_metadata=copy.deepcopy(decision.metadata or {})
                l4_prediction_snapshot=decision.prediction_snapshot
                elapsed=time.perf_counter()-gate_t0
                o4={"ok":True,"status":"RTMO_ARTIFACT_REUSE","result":decision.result,
                    "orchestration_timing":{"elapsed_s":elapsed,"deadline_s":float(self.config.get("tdoc.rtmo.realtime.max_control_period_s",0.1)),"deadline_ok":elapsed<=float(self.config.get("tdoc.rtmo.realtime.max_control_period_s",0.1)),"rtmo_fast_path":True,"layer4_compute_in_fast_path":False},
                    "rtmo_l4_artifact":decision.metadata}
                self._event(tx,"RTMO-L4-ArtifactGate","Layer5","Route",snap_ver,boundary,"verified_l4_artifact_reuse","OK",decision.metadata or {})
                # Refresh is intentionally decoupled from the fast transaction.  A
                # running refresh never blocks reuse of the last still-valid artifact.
                age=float((decision.metadata or {}).get("artifact_age_s",0.0) or 0.0)
                refresh_after=float(self.config.get("tdoc.rtmo.l4_artifacts.refresh_after_s",5.0))
                artifact_status=self.rtmo_l4_artifacts.status()
                bootstrap_first_reuse=bool(
                    (decision.metadata or {}).get("publication_mode")=="SYNCHRONOUS_PRE_RELEASE"
                    and int(artifact_status.get("executed_transaction_count_for_artifact",0) or 0)==0
                )
                # The first reuse immediately follows synchronous phase bootstrap.
                # Do not launch duplicate CPU-bound verification while the mission
                # driver may still perform its one-time plant-clock rebase.
                if age>=refresh_after and not bootstrap_first_reuse:
                    payload={"snapshot":copy.deepcopy(snap),"request":copy.deepcopy(request)}
                    self.rtmo_l4_artifacts.schedule_refresh(
                        invoke=lambda p=payload:self.adapters[4].invoke(p), snapshot=snap, request=request,
                        orchestration_version=self.versions.active, reference_time_s=ref, trigger_reason="artifact_refresh_age")
            else:
                # Cold start and phase entry are owned by the synchronous,
                # non-actuating bootstrap in the mission driver.  Starting a second
                # CPU-bound FF worker here duplicates that work and lets the stale
                # worker contend with the newly seeded fast lane.  Runtime envelope,
                # freshness and version failures still request an async refresh.
                if decision.reason in {"no_verified_l4_artifact","artifact_phase_mismatch"}:
                    scheduled={"scheduled":False,"reason":"phase_bootstrap_owner",
                               "gate_reason":decision.reason}
                else:
                    payload={"snapshot":copy.deepcopy(snap),"request":copy.deepcopy(request)}
                    scheduled=self.rtmo_l4_artifacts.schedule_refresh(
                        invoke=lambda p=payload:self.adapters[4].invoke(p), snapshot=snap, request=request,
                        orchestration_version=self.versions.active, reference_time_s=ref, trigger_reason=decision.reason)
                elapsed=time.perf_counter()-gate_t0
                o4={"ok":False,"status":"HELD","reason":"RTMO_L4_ARTIFACT_PENDING","result":None,
                    "orchestration_timing":{"elapsed_s":elapsed,"deadline_s":float(self.config.get("tdoc.rtmo.realtime.max_control_period_s",0.1)),"deadline_ok":elapsed<=float(self.config.get("tdoc.rtmo.realtime.max_control_period_s",0.1)),"rtmo_fast_path":True,"layer4_compute_in_fast_path":False},
                    "rtmo_l4_artifact":{"gate_reason":decision.reason,"gate":decision.metadata,"refresh":scheduled}}
                self._event(tx,"RTMO-L4-ArtifactGate","Layer4","Hold",snap_ver,boundary,"no_valid_l4_artifact","HELD",o4["rtmo_l4_artifact"])
        else:
            o4=self._invoke(4,tx,"Layer3.3-snapshot",{"snapshot":snap,"request":request},snap_ver,boundary,r4)
        downstream={}; outputs={}
        online_training=bool(mode=="ONLINE_JSBSIM" and (request.get("online_training") or request.get("require_offline_knowledge")))
        handoff={"ok":True,"status":"NOT_REQUIRED","reason":"online_training_not_requested"}
        setd_precheck={"ok":True,"reason":"not_required"}
        if online_training:
            if self._online_prior_cache is None:
                checked_handoff=self.offline_handoff.validate_online_start(request)
                checked_setd=(self.adapters[5].load_setd() if checked_handoff.get("ok")
                              else {"ok":False,"reason":"offline_handoff_not_ready"})
                self._online_prior_cache={"handoff":checked_handoff,"setd_precheck":checked_setd}
            handoff=dict(self._online_prior_cache.get("handoff") or {})
            setd_precheck=dict(self._online_prior_cache.get("setd_precheck") or {})
            if handoff.get("ok"):
                hm=handoff.get("manifest") or {}; expected=(hm.get("setd") or {}).get("package_id")
                actual=((setd_precheck.get("manifest") or {}).get("package_id") if isinstance(setd_precheck,dict) else None)
                quality=((setd_precheck.get("manifest") or {}).get("knowledge_quality") or {}) if isinstance(setd_precheck,dict) else {}
                control_valid=bool(quality.get("control_knowledge_valid") is True)
                phase_cov=dict(quality.get("phase_control_coverage") or {})
                phase_complete=bool(phase_cov.get("all_required_control_phases") is True)
                missing_phases=list(phase_cov.get("missing_control_phases") or [])
                phase_consistent=bool(phase_complete and not missing_phases)
                # Solution-4-v2: SETD identity/loadability is the Online prior gate.
                # Incomplete phase coverage is forwarded as MissionCertification
                # metadata and enforced later by RTMO current/next-phase release.
                if not setd_precheck.get("ok") or not expected or actual!=expected:
                    reason="offline_SETD_identity_or_verification_mismatch"
                    handoff={**handoff,"ok":False,"status":"NOT_READY","reason":reason,"expected_package_id":expected,"actual_package_id":actual,"setd_precheck":setd_precheck}
                else:
                    setd_cycle_evidence={
                        "ok":True,"reason":setd_precheck.get("reason"),
                        "manifest":{k:(setd_precheck.get("manifest") or {}).get(k) for k in (
                            "schema","package_id","episode_id","committed","experience_count",
                            "logical_content_sha256","knowledge_quality",
                        ) if k in (setd_precheck.get("manifest") or {})},
                        "recovery":setd_precheck.get("recovery"),
                        "cached_read_only_prior":True,"actuation_authority":False,
                    }
                    handoff={**handoff,"setd_precheck":setd_cycle_evidence,"control_knowledge_valid":control_valid,"phase_coverage_consistent":phase_consistent,"missing_phases":missing_phases,"mission_certification_status":"FULLY_CERTIFIED" if phase_consistent else "NOT_FULLY_CERTIFIED"}
                    # Forward only the Layer-5-defined CRP/SARG/TEP reference bundle; SETD itself
                    # is loaded by the external Layer-5 KStore from the same TDOC-owned compressed workspace.
                    refs=dict(((hm.get("setd") or {}).get("reference_bundle") or {}))
                    if refs:
                        merged=dict(request.get("knowledge_refs") or {}); merged.update(refs); request["knowledge_refs"]=merged
                    ctx=dict(request.get("context") or {}); ctx["offline_knowledge_handoff"]={"knowledge_id":hm.get("knowledge_id"),"knowledge_version":hm.get("knowledge_version"),"handoff_hash":hm.get("handoff_hash"),"setd_package_id":expected}; request["context"]=ctx
        pf5=self.adapters[5].preflight(); layer5_valid=bool(o4.get("ok") and (handoff.get("ok",False) if online_training else True))
        r5=self.gate.evaluate(5,valid=layer5_valid,version_ok=True,boundary_ok=True,health_ok=bool(pf5.get("ok",False)),policy_route=self.config.layer_enabled(5),evidence={"adapter":pf5,"learning_mode":mode,"online_training":online_training,"offline_knowledge_handoff":handoff}); statuses["5"]=r5.to_dict()
        o5=self._invoke(5,tx,"Layer4-output",{"layer4_result":o4.get("result"),"network_snapshot":snap,"request":request,"version":snap_ver,"learning_mode":mode},snap_ver,boundary,r5)
        downstream["5"]={"status":o5.get("status","OK" if o5.get("ok") else "FAILED"),"enabled":self.config.layer_enabled(5),"output":o5}; outputs["5"]=o5
        if isinstance(o5.get('decision_layer4_input'),dict):o4['result']=o5.pop('decision_layer4_input')
        current_prediction=None
        offline_closed=False; offline_timing_contract=None; execution_evidence=None; o6=None; o7=None; l7_routes={}
        if mode=="OFFLINE_SE":
            self._event(tx,"TDOC","Layer6","Skip",snap_ver,boundary,"layer6_not_in_offline_training_path","SKIPPED",{"route":"Layer5->TDOC->Layer2"}); statuses["6"]={"layer_id":6,"ready":False,"policy_route":False,"reasons":["NOT_IN_OFFLINE_PATH"],"evidence":{"learning_mode":mode}}; downstream["6"]={"status":"NOT_IN_OFFLINE_PATH","enabled":False}
            self._event(tx,"TDOC","Layer7","Skip",snap_ver,boundary,"layer7_not_in_offline_training_path","SKIPPED",{"route":"Layer5->TDOC->Layer2"}); statuses["7"]={"layer_id":7,"ready":False,"policy_route":False,"reasons":["NOT_IN_OFFLINE_PATH"]}; downstream["7"]={"status":"NOT_IN_OFFLINE_PATH","enabled":False}
            fb=self.adapters[5].offline_training_feedback(o5,version=snap_ver)
            loop_deadline=float(self.config.get("deadlines.offline_loop_s",0.0) or 0.0)
            elapsed_before_feedback=time.perf_counter()-cycle_started_monotonic_s
            loop_budget_valid=bool(loop_deadline>0.0)
            if not loop_budget_valid or elapsed_before_feedback>loop_deadline:
                downstream["2_offline_feedback"]={"status":"HELD","reason":"offline_loop_deadline_exceeded_before_feedback"}
            elif fb.get("ok") and isinstance(fb.get("training_feedback"),dict):
                feedback=fb["training_feedback"]; rr=self._route_offline_feedback_to_layer2(tx,feedback,request,snap_ver,boundary,statuses); offline_closed=bool(rr.get("ok") and rr.get("control_effect_applied",False)); downstream["2_offline_feedback"]={"status":"OK" if offline_closed else "FAILED","output":rr}
                elapsed_after_feedback=time.perf_counter()-cycle_started_monotonic_s
                if elapsed_after_feedback>loop_deadline:
                    offline_closed=False; downstream["2_offline_feedback"]["status"]="FAILED"; downstream["2_offline_feedback"]["reason"]="offline_loop_deadline_exceeded"
                self._event(tx,"Layer2-training-output","TDOC","Route",snap_ver,boundary,"offline_training_loop_closed" if offline_closed else "offline_training_feedback_failed","OK" if offline_closed else "FAILED",{"feedback_hash":feedback.get("feedback_hash")})
            else: downstream["2_offline_feedback"]={"status":"HELD","reason":fb.get("reason")}
            loop_elapsed=time.perf_counter()-cycle_started_monotonic_s
            offline_timing_contract={"schema":"TDOC-offline-timing-contract-v1","clock":"monotonic","deadline_s":loop_deadline,
                "elapsed_s":loop_elapsed,"deadline_ok":bool(loop_budget_valid and loop_elapsed<=loop_deadline),"loop_closed":offline_closed,
                "ok":bool(loop_budget_valid and loop_elapsed<=loop_deadline and offline_closed),"layer6_applicable":False,"layer7_applicable":False}
        elif mode=="ONLINE_JSBSIM" and pre_release_verify:
            # Solution2-v3: capability bootstrap is a verification-only transaction.
            # L2→L3→fresh L4→L5 executes, but Layer6/Layer7 are explicitly not invoked.
            # The resulting evidence may qualify only the immediately-following
            # RuntimeRelease; it is not a durable joint-control proof.
            pf6=self.adapters[6].preflight()
            statuses["6"]={"layer_id":6,"ready":False,"policy_route":False,"reasons":["PRE_RELEASE_VERIFY_NON_ACTUATING"],"evidence":{"learning_mode":mode,"rtmo_pre_release_verify":True}}
            statuses["7"]={"layer_id":7,"ready":False,"policy_route":False,"reasons":["PRE_RELEASE_VERIFY_NON_ACTUATING"],"evidence":{"learning_mode":mode,"rtmo_pre_release_verify":True}}
            downstream["6"]={"status":"NOT_INVOKED_PRE_RELEASE_VERIFY","enabled":True,"non_actuating":True}
            downstream["7"]={"status":"NOT_INVOKED_PRE_RELEASE_VERIFY","enabled":True,"non_actuating":True}
            self._event(tx,"TDOC","Layer6","Skip",snap_ver,boundary,"pre_release_verify_non_actuating","SKIPPED",{"phase":request.get("stage")})
            self._event(tx,"TDOC","Layer7","Skip",snap_ver,boundary,"pre_release_verify_non_actuating","SKIPPED",{"phase":request.get("stage")})
        elif mode=="ONLINE_JSBSIM":
            current_prediction_gate={'ok':True,'status':'NOT_REQUIRED'}
            if (o5.get('ok') and not passive_contact_boundary
                and (o4.get('result') or {}).get('schema')!='Layer4-native-execution-result-v1'
                and (o5.get('result',{}).get('gates') or {}).get('FINAL') is True):
                try:
                    current_prediction=self.adapters[4].verify_committed_first_move(o4['result'],snap,o5,request)
                    o5['current_first_move_prediction']=current_prediction
                    current_prediction_gate={'ok':True,'status':'VERIFIED'}
                except Exception as exc:
                    current_prediction_gate={'ok':False,'status':'REJECTED','reason':'current_first_move_prediction_rejected','error':repr(exc)}
                    if isinstance(getattr(exc,'evidence',None),dict):
                        current_prediction_gate['evidence']=exc.evidence
                    self._event(tx,'TDOC','Layer6','Reject',snap_ver,boundary,'current_first_move_prediction_rejected','FAILED',{'error':repr(exc)})
            pf6=self.adapters[6].preflight(); r6=self.gate.evaluate(6,valid=bool(o5.get("ok") and current_prediction_gate['ok']),version_ok=True,boundary_ok=True,health_ok=bool(pf6.get("online_ready",False)),policy_route=self.config.layer_enabled(6),evidence={"adapter":pf6,"selected_mode":mode,"current_command_prediction_gate":current_prediction_gate}); statuses["6"]=r6.to_dict()
            o6=self._invoke(6,tx,"Layer5-output",{"layer5_output":o5,"layer4_result":o4.get("result"),"network_snapshot":snap,"request":request,"version":snap_ver,"learning_mode":mode,"transaction_id":tx},snap_ver,boundary,r6); execution_ack_monotonic_s=time.perf_counter(); downstream["6"]={"status":o6.get("status","FAILED"),"enabled":True,"output":o6}; outputs["6"]=o6; execution_evidence=o6.get("execution_evidence")
            if not current_prediction_gate['ok']:
                o6.update(reason=current_prediction_gate['reason'],prediction_gate=current_prediction_gate)
            post_execution_observation=_post_execution_observation_contract(
                layer6_output=o6,transaction_id=tx,episode_id=request.get("online_episode_id"),
                cycle_in_episode=request.get("online_cycle_in_episode"),
            )
            if isinstance(post_execution_observation,dict):
                self._last_post_execution_observation=copy.deepcopy(post_execution_observation)
            if str(o6.get("status") or "") == "EXECUTED" and o6.get("actuation_executed") is True and isinstance(execution_evidence,dict):
                if rtmo_fast_l4 and o4.get("status")=="RTMO_ARTIFACT_REUSE" and isinstance(l4_artifact_use_metadata,dict):
                    o6["l4_first_move_consumption"]=self.rtmo_l4_artifacts.mark_first_move_executed(
                        metadata=l4_artifact_use_metadata,
                        transaction_id=tx,
                        execution_status=str(o6.get("status") or ""),
                        actuation_executed=bool(o6.get("actuation_executed")),
                    )
                self.rtmo_control_latency.record(
                    transaction_id=tx,
                    latency_s=execution_ack_monotonic_s-snapshot_ready_monotonic_s,
                    phase=request.get("stage"),
                    execution_status=str(o6.get("status") or ""),
                    actuation_executed=True,
                )
            if isinstance(execution_evidence,dict) and isinstance(execution_evidence.get('io_snapshot'),dict):
                # TDOC augments the Layer-5 decision envelope with post-decision live
                # vehicle/actuator feedback. This is reporting evidence, not a new
                # Layer-5 command and never bypasses FINAL/governance gates.
                o5['decision_vehicle_state']=phoenix_state_from_ptlib_snapshot(execution_evidence.get('io_snapshot'),o5.get('result') if isinstance(o5.get('result'),dict) else o5,o6)
                o5['decision_vehicle_state_semantics']='post-decision Layer6 GUI/ArduPilot feedback; elevon angle and rotor speed are estimates unless a direct sensor exists'
            pf7=self.adapters[7].preflight()
            l7_trigger=_layer7_trigger_eligibility(layer6_output=o6,execution_evidence=execution_evidence,layer7_preflight=pf7,request=request)
            r7=self.gate.evaluate(7,valid=bool(l7_trigger.get("upstream_eligible")),version_ok=True,boundary_ok=True,health_ok=bool(pf7.get("ok",False)),policy_route=self.config.layer_enabled(7),evidence={"adapter":pf7,"execution_evidence_present":bool(execution_evidence),"trigger_eligibility":l7_trigger}); statuses["7"]=r7.to_dict()
            if l7_trigger.get("eligible") and r7.ready:
                feedback_layer4=dict(o4.get('result') or {})
                if current_prediction is not None:feedback_layer4['committed_first_move']=current_prediction
                self._layer7_context={"transaction_id":tx,"snapshot":snap,"snapshot_version":snap_ver,"layer4":feedback_layer4,"layer5":o5,"request":request}
                prepare_diagnostic=getattr(self.adapters[7],'prepare_native_diagnostic',None)
                diagnostic_preparation=None
                try:
                    payload=Layer7Adapter.build_input(transaction_id=tx,version=snap_ver,reference_time_s=ref,execution_evidence=execution_evidence,layer4_result=feedback_layer4,layer5_result=o5,network_snapshot=snap,request=request,layer4_root=self.config.path_value("TDDT_LAYER4_ROOT"),prediction_snapshot=l4_prediction_snapshot)
                    if callable(prepare_diagnostic):
                        diagnostic_preparation=prepare_diagnostic(payload,execution_evidence=execution_evidence,
                            layer4_result=feedback_layer4,layer5_result=o5,network_snapshot=snap,
                            prediction_snapshot=l4_prediction_snapshot)
                except Exception as exc:
                    # A rejected downstream contract must retain the already executed
                    # L6 proof in this cycle, and must never turn into L7 success.
                    o7={"ok":False,"status":"FAILED","reason":"layer7_payload_contract_rejected","error":repr(exc)}
                    self._event(tx,"TDOC","Layer7","Reject",snap_ver,boundary,
                                "layer7_payload_contract_rejected","FAILED",{"error":repr(exc)})
                else:
                    o7=self._invoke(7,tx,"Layer6-ExecutionEvidence",payload,snap_ver,boundary,r7)
                outputs["7"]=o7
                if diagnostic_preparation is not None:
                    o7['native_diagnostic_preparation']=diagnostic_preparation
                if o7.get("ok") and isinstance(o7.get("result"),dict):
                    l7_routes=self.feedback_router.route_layer7(
                        o7["result"],transaction_id=tx,base_version=snap_ver,
                        run_context=current_run_context)
                    if "Layer2" not in (l7_routes.get("destinations") or {}):
                        ack={"schema":"TDOC-L7-to-L2-no-change-ack-v1","transaction_id":tx,
                             "source":"Layer7","destination":"Layer2","base_version":str(snap_ver),
                             "candidate_version":None,
                             "packet":{"NoChangeAcknowledgement":True,
                                       "commit_status":str(((o7.get("result") or {}).get("commit") or {}).get("status") or "NO_CHANGE")},
                             "run_context":FeedbackRouter._run_scope(current_run_context),
                             "persistence":"TRANSIENT_IN_MEMORY","learning_update":False}
                        ack["route_hash"]=sha256_obj(ack)
                        self._pending_l7_l2_ack=ack
                        l7_routes["layer2_no_change_ack"]={"status":"SCHEDULED_NEXT_CYCLE",
                            "route_hash":ack["route_hash"],"persistent":False,"learning_update":False}
                    pending=self.feedback_router.pending_context(run_context=current_run_context)
                    l3_apply=self.adapters[3].apply_layer7_feedback(pending) if pending else {"ok":True,"applied_transient":[]}
                    for dest,meta in (l7_routes.get("destinations") or {}).items(): self._event(tx,"Layer7",dest,"VersionedRoute",snap_ver,boundary,"layer7_adaptation_feedback","ROUTED",meta)
                    l7_routes["layer3_transient_application"]=l3_apply
                downstream["7"]={"status":"OK" if o7.get("ok") else "FAILED","enabled":True,"output":o7,"feedback_routing":l7_routes,"trigger_eligibility":l7_trigger,"consequence_only":False,"root_cause":not bool(o7.get("ok"))}
            else:
                downstream["7"]={"status":str(l7_trigger.get("status") or "HELD"),"enabled":True,"preflight":pf7,"reason":str(l7_trigger.get("reason") or "layer7_trigger_not_ready"),"trigger_eligibility":l7_trigger,"consequence_only":bool(l7_trigger.get("consequence_only")),"root_cause":bool(l7_trigger.get("root_cause"))}
                self._event(tx,"TDOC","Layer7","TriggerGuard",snap_ver,boundary,str(l7_trigger.get("reason") or "layer7_trigger_not_ready"),"SKIPPED" if l7_trigger.get("consequence_only") else "HELD",l7_trigger)
        else: raise ValueError(f"unsupported learning_mode: {mode}")
        l3_ua_pf=dict(l3_pf.get("unit_adapter") or {}); ua_admitted=bool(l3_ua_pf.get("ok",l3_pf.get("ok",False))); pf7_now=self.adapters[7].preflight()
        conform={"A_owner":True,"A_boundary":True,"A_atomic":bool(atomic if o3.get("ok") else False),"A_trace":self.provenance.health().get("ok",False),"A_no_direct_ownership":bool(o2.get("direct_layer_ownership") is False if o2 else True),
            "A_unit_adapter":bool(ua_admitted and ua_binding.get("health_ok",True)),"A_unit_adapter_executed":bool(ua_binding),"A_layer5_external":bool(pf5.get("ok")),"A_layer6_online_only":True,
            "A_layer7_external":bool(pf7_now.get("ok") and pf7_now.get("external_only")),"A_layer7_tdoc_owned":bool((pf7_now.get("registration_descriptor") or {}).get("trigger_owner")=="TDOC" and (pf7_now.get("registration_descriptor") or {}).get("direct_actuation") is False),
            "A_offline_bypasses_layer6_layer7":True if mode!="OFFLINE_SE" else downstream.get("6",{}).get("status")=="NOT_IN_OFFLINE_PATH" and downstream.get("7",{}).get("status")=="NOT_IN_OFFLINE_PATH",
            "A_offline_bypasses_layer6":True if mode!="OFFLINE_SE" else downstream.get("6",{}).get("status")=="NOT_IN_OFFLINE_PATH",
            "A_offline_loop_closure":True if mode!="OFFLINE_SE" else bool(offline_closed),"A_online_layer6_external":True if mode!="ONLINE_JSBSIM" else bool((pf6.get("layer6_client") or {}).get("ok") and (pf6.get("layer6_client") or {}).get("external_only")),
            "A_online_feedback_to_layer7":True if mode!="ONLINE_JSBSIM" or pre_release_verify or (locals().get("l7_trigger") or {}).get("consequence_only") else bool(o7 and o7.get("ok")),"A_layer7_versioned_route":True if mode!="ONLINE_JSBSIM" or pre_release_verify or (locals().get("l7_trigger") or {}).get("consequence_only") else bool(o7 and o7.get("ok") and l7_routes.get("owner")==self.OWNER),
            "A_online_previous_l7_ingress_to_l2":True if mode!="ONLINE_JSBSIM" or not pending_l7 else bool(((o2.get("layer3_runtime_meta") or {}).get("layer7_feedback_ingress") or {}).get("consumed") if pending_l7.get("Layer2") else True),
            "A_online_hold_no_actuation":True if mode!="ONLINE_JSBSIM" else bool(not o6 or o6.get("status")!="OBSERVATION_ONLY" or o6.get("actuation_executed") is False),
            "A_online_offline_knowledge_handoff":True if not online_training else bool(handoff.get("ok") and setd_precheck.get("ok")),
            "A_online_setd_identity_bound":True if not online_training else bool(((handoff.get("manifest") or {}).get("setd") or {}).get("package_id") == ((setd_precheck.get("manifest") or {}).get("package_id")))}
        architecture_keys=[k for k in conform if k not in ("A_unit_adapter_executed",)]; conform["A_invoke"]=all(bool(conform[k]) for k in architecture_keys)
        base_layers_ok=bool(o2.get("ok") and o3.get("ok") and o4.get("ok") and o5.get("ok"))
        if mode=="OFFLINE_SE":
            operational_route_ok=bool(offline_closed); knowledge_route_ok=True
        elif pre_release_verify:
            operational_route_ok=bool(base_layers_ok); knowledge_route_ok=bool(base_layers_ok)
        else:
            operational_route_ok=bool(o6 and o6.get("status")=="EXECUTED" and o7 and o7.get("ok"))
            knowledge_route_ok=bool(o6 and o6.get("status") in ("EXECUTED","OBSERVATION_ONLY") and o7 and o7.get("ok"))
        ok=bool(base_layers_ok and operational_route_ok); status="OK" if ok else ("DEGRADED" if base_layers_ok and knowledge_route_ok else "DEGRADED")
        result=CycleResult(tx,ver,ref,status,statuses,packets,o3.get("result"),o4.get("result"),downstream,conform,{"trigger_owner":self.OWNER,"boundary":boundary,"version":ver,"learning_mode":mode,"twin_registry_hash":sha256_obj(self.twin_registry.snapshot()),"network_state_hash":network_mirror.get("network_state_hash"),"transaction_hash":None,
            "layer5_root":pf5.get("root"),"layer6_scope":"ONLINE_JSBSIM_ONLY","layer7_root":pf7_now.get("root"),"layer7_version":pf7_now.get("version"),"offline_training_loop_closed":offline_closed,"online_execution_evidence_hash":execution_evidence.get("evidence_hash") if isinstance(execution_evidence,dict) else None,
            "online_knowledge_route_complete":knowledge_route_ok if mode=="ONLINE_JSBSIM" else None,"online_actuation_executed":bool(o6 and o6.get("status")=="EXECUTED") if mode=="ONLINE_JSBSIM" else None,
            "current_online_observation":(copy.deepcopy(current_online_observation) if mode=="ONLINE_JSBSIM" else None),
            "post_execution_observation":(copy.deepcopy(post_execution_observation) if mode=="ONLINE_JSBSIM" else None),
            "online_training":online_training if mode=="ONLINE_JSBSIM" else False,"layer7_trigger_eligibility":(locals().get("l7_trigger") if mode=="ONLINE_JSBSIM" and not pre_release_verify else None),"rtmo_pre_release_verify":{"enabled":bool(pre_release_verify),"phase":request.get("stage"),"non_actuating":bool(pre_release_verify),"layer6_invoked":False if pre_release_verify else None,"layer7_invoked":False if pre_release_verify else None},"offline_knowledge_handoff":({"ok":handoff.get("ok"),"status":handoff.get("status"),"reason":handoff.get("reason"),"manifest_path":handoff.get("manifest_path"),"knowledge_id":((handoff.get("manifest") or {}).get("knowledge_id")),"knowledge_version":((handoff.get("manifest") or {}).get("knowledge_version")),"setd_package_id":(((handoff.get("manifest") or {}).get("setd") or {}).get("package_id")),"setd_loaded":bool(setd_precheck.get("ok"))} if mode=="ONLINE_JSBSIM" else None),
            "online_previous_layer7_feedback":({"present":bool(pending_l7),"destinations":sorted(pending_l7),"layer2_ingress":((o2.get("layer3_runtime_meta") or {}).get("layer7_feedback_ingress") or {})} if mode=="ONLINE_JSBSIM" else None),
            "offline_timing_contract":offline_timing_contract,
            "unit_adapter":{"root":ua_binding.get("root") or l3_ua_pf.get("root"),"version":ua_binding.get("version") or (l3_ua_pf.get("api_probe") or {}).get("version"),"conversion_owner":ua_binding.get("conversion_owner") or l3_ua_pf.get("conversion_owner"),"admitted":ua_admitted,"executed":bool(ua_binding),"conversions":ua_binding.get("conversions"),"failures":ua_binding.get("failures")}})
        # Evidence-based Layer-4/Layer-5 MPC participation is recorded separately from
        # FINAL authorization and physical actuation.
        result.provenance["mpc_participation"]=cycle_mpc_participation(result.to_dict())
        result.provenance["decision_gate_trace"]=cycle_decision_gate_trace(result.to_dict())
        _ctl_summary=self.rtmo_control_latency.summary()
        _ctl_sample=None
        if mode=="ONLINE_JSBSIM" and not pre_release_verify and isinstance(o6,dict) and str(o6.get("status") or "")=="EXECUTED" and o6.get("actuation_executed") is True:
            _lat=max(0.0,float(locals().get("execution_ack_monotonic_s",time.perf_counter()))-float(snapshot_ready_monotonic_s))
            _ctl_sample={
                "schema":"TDOC-RTMO-control-transaction-latency-v1",
                "transaction_id":tx,"phase":request.get("stage"),"latency_s":_lat,
                "deadline_s":float(self.config.get("tdoc.rtmo.realtime.max_control_period_s",0.1)),
                "deadline_ok":bool(_lat<=float(self.config.get("tdoc.rtmo.realtime.max_control_period_s",0.1))),
                "start_semantics":"LAYER3_ATOMIC_OPERATIONAL_SNAPSHOT_READY",
                "end_semantics":"LAYER6_NATIVE_EXECUTION_ACK_RETURNED_TO_TDOC",
                "layer4_compute_in_fast_path":False if rtmo_fast_l4 else None,
                "artifact_reuse":bool(rtmo_fast_l4 and o4.get("status")=="RTMO_ARTIFACT_REUSE"),
                "post_ack_persistence_excluded":True,
            }
        result.provenance["rtmo_control_transaction"]={"sample":_ctl_sample,"summary":_ctl_summary}
        # Monotonic component timing is diagnostic only; it does not affect scheduling
        # or any decision/safety threshold.
        component_timing={}
        for lid,obj in (("L2",o2),("L3",o3),("L4",o4),("L5",o5),("L6",o6),("L7",o7)):
            if isinstance(obj,dict):
                try: component_timing[lid]=float((obj.get("orchestration_timing") or {}).get("elapsed_s"))
                except Exception: pass
        try: component_timing["L4_full_fidelity"]=float((((o4.get("result") or {}).get("provenance") or {}).get("full_fidelity_verification") or {}).get("elapsed_s"))
        except Exception: pass
        result.provenance["component_timing_s"]=component_timing
        result.provenance['layer2_wall_timings_s']=(o2.get('layer3_runtime_meta') or {}).get('wall_timings_s',{})
        result.provenance["transaction_hash"]=sha256_obj({k:v for k,v in result.to_dict().items() if k!="provenance"})
        # P1-4: actuating Online persistence is post-ack and nonblocking.  PRE_RELEASE
        # is explicitly non-actuating/outside the fast deadline; persisting its large
        # FF record before publication prevents a Python gzip worker from competing
        # with the newly opened fast lane through the GIL.
        if mode=="ONLINE_JSBSIM" and not pre_release_verify and self.online_persistence is not None:
            lane=self.online_persistence.submit(result.to_dict())
            result.provenance["online_persistence"]={
                "mode":"BOUNDED_ASYNC_POST_ACK","control_hot_path":False,**lane
            }
        elif mode=="ONLINE_JSBSIM":
            # PRE_RELEASE is online audit evidence, not a new general-knowledge
            # sample. Layer7 alone owns persistent online learning/feedback state.
            result.provenance["online_knowledge_persistence"]={
                "schema":"TDOC-online-knowledge-authority-v1",
                "general_knowledge_write":False,
                "persistent_learning_authority":"Layer7",
                "reason":"pre_release_is_audit_evidence_not_new_generic_knowledge",
            }
            st0=time.perf_counter(); layer_storage=self.layer_archive.record_cycle(result.to_dict()); component_timing["runtime_storage"]=time.perf_counter()-st0; result.provenance["layer_runtime_storage"]=layer_storage
            outdir=self.config.runtime_root/"cycles"; outdir.mkdir(parents=True,exist_ok=True)
            write_json_gz(outdir/f"{tx}.json.gz",result.to_dict(),indent=2)
        else:
            kt0=time.perf_counter(); ksummary=self.knowledge.record(result.to_dict()); component_timing["knowledge_ledger"]=time.perf_counter()-kt0; result.provenance["knowledge_ledger"]=ksummary
            st0=time.perf_counter(); layer_storage=self.layer_archive.record_cycle(result.to_dict()); component_timing["runtime_storage"]=time.perf_counter()-st0; result.provenance["layer_runtime_storage"]=layer_storage
            outdir=self.config.runtime_root/"cycles"; outdir.mkdir(parents=True,exist_ok=True)
            write_json_gz(outdir/f"{tx}.json.gz",result.to_dict(),indent=2)
        return result
    def realtime_control_metrics(self)->dict[str,Any]:
        out=self.rtmo_control_latency.summary()
        out["persistence_lane"]=self.online_persistence.status() if self.online_persistence is not None else {"mode":"SYNCHRONOUS"}
        out["l4_artifact_gate"]=self.rtmo_l4_artifacts.status()
        return out

    def prepare_synchronous_pre_release(self)->dict[str,Any]:
        """Drain audit writers before CPU-bound, non-actuating FF verification.

        Gzip/audit workers are deliberately asynchronous during the actuation hot
        path.  A phase-entry or plant-clock-rebase bootstrap is outside that path;
        draining here prevents those Python workers from contending with WingMPET
        and turning valid evidence into a host-load-dependent timeout.
        """
        timeout=float(self.config.get("tdoc.rtmo.persistence.flush_timeout_s",30.0))
        persistence_ok=(self.online_persistence.flush(timeout)
                        if self.online_persistence is not None else True)
        provenance_ok=self.provenance.flush(timeout)
        return {
            "ok":bool(persistence_ok and provenance_ok),
            "online_persistence_flushed":bool(persistence_ok),
            "online_provenance_flushed":bool(provenance_ok),
            "control_hot_path":False,
        }

    def begin_online_episode(self, episode_id:str)->dict[str,Any]:
        """Start one TDOC-owned online mission episode.

        The external PT_LIB remains the owner of ArduPilot/JSBSim lifecycle actions;
        TDOC only asks the Layer-6 boundary to open a fresh session for the episode.
        RTMO Layer-4 artifacts are run-scoped and are invalidated here so no artifact
        can cross an episode boundary.
        """
        self.provenance.set_async(True)
        self.rtmo_l4_artifacts.reset(f"begin_online_episode:{episode_id}")
        self._online_prior_cache=None
        self._last_current_online_plant_time_s=None
        self._last_post_execution_observation=None
        self._pending_l7_l2_ack=None
        return self.adapters[6].begin_online_episode(str(episode_id))

    def end_online_episode(self, episode_id:str|None=None)->dict[str,Any]:
        # Outside the control hot path: ensure queued evidence is durable before the
        # episode/session is finalized. This may take time but cannot alter actuation.
        persistence_flushed=True
        if self.online_persistence is not None:
            persistence_flushed=self.online_persistence.flush(float(self.config.get("tdoc.rtmo.persistence.flush_timeout_s",30.0)))
        provenance_flushed=self.provenance.flush(float(self.config.get("tdoc.rtmo.persistence.flush_timeout_s",30.0)))
        self.provenance.set_async(False)
        out=self.adapters[6].end_online_episode(episode_id)
        metrics=self.realtime_control_metrics()
        try: write_json_gz(self.config.runtime_root/"rtmo"/"realtime_control_metrics.json.gz",metrics,indent=2)
        except Exception: pass
        if isinstance(out,dict):
            out["online_persistence_flushed"]=bool(persistence_flushed)
            out["online_provenance_flushed"]=bool(provenance_flushed)
            out["realtime_control_metrics"]=metrics
        return out

    def finalize_offline_knowledge(self, knowledge:dict[str,Any]|None=None)->dict[str,Any]:
        """Finalize external Layer-5 SS→SETD and publish a TDOC handoff manifest."""
        ledger=self.knowledge.latest("OFFLINE_SE")
        if not ledger:
            return {"ok":False,"committed":False,"status":"NOT_READY","reason":"offline_knowledge_ledger_empty"}
        fin=self.adapters[5].finalize_offline_episode(knowledge)
        if not fin.get("committed"):
            return {"ok":False,"committed":False,"status":"NOT_READY","reason":fin.get("reason","layer5_setd_finalize_failed"),"layer5_finalization":fin}
        setd=self.adapters[5].load_setd()
        fp=self.adapters[5].kstore_fingerprint()
        handoff=self.offline_handoff.record_finalized(ledger_record=ledger,finalization=fin,setd=setd,kstore_fingerprint=fp,source_version=self.versions.active,layer5_root=str(self.config.path_value("TDDT_LAYER5_ROOT") or ""),climate_time_alignment=dict((knowledge or {}).get("climate_time_alignment") or {}))
        policy=self.knowledge_policy.compile(handoff_path=self.offline_handoff.latest_path) if handoff.get("ok") else {"ok":False,"status":"NOT_READY","reason":"handoff_not_ready"}
        policy_start=optional_policy_admission(policy)
        ready=bool(handoff.get("ok") and setd.get("ok") and policy_start.get("ok"))
        return {"ok":ready,"committed":bool(fin.get("committed")),"status":"READY" if ready else "NOT_READY",
                "readiness_scope":"OFFLINE_SETD_HANDOFF_ONLY; not control/flight readiness",
                "layer5_finalization":fin,"setd":setd,"handoff":handoff,"knowledge_policy_compilation":policy,
                "knowledge_policy_admission":policy_start,"policy_available":policy_start["policy_available"],
                "actuation_authority":False}

    def ensure_online_knowledge_policy(self, *, handoff_path:str|None=None)->dict[str,Any]:
        """Read-only startup check: valid Offline knowledge may lack a learned policy.

        OnlineStart never compiles/repairs policy files. Rejected/unreadable
        artifacts remain blockers; only explicit benign absence is optional.
        """
        gate=self.offline_handoff.validate_online_start({"offline_knowledge_manifest":handoff_path} if handoff_path else {})
        if not gate.get("ok"):
            return {"ok":False,"status":"NOT_READY","reason":"offline_handoff_not_ready","handoff_gate":gate}
        handoff=dict(gate.get("manifest") or {})
        loaded=self.knowledge_policy.load(handoff_hash=handoff.get("handoff_hash"))
        admission=optional_policy_admission(loaded)
        return {**admission,"reused":admission["policy_available"],"compiled":False,
                "handoff_hash":handoff.get("handoff_hash")}

    def route_feedback(self,error:dict[str,Any])->dict[str,Any]: return self.feedback_router.route(error)
    def safe_commit(self,candidate_version:str,*,authorized:bool,validated:bool)->dict[str,Any]: return self.versions.safe_commit(candidate_version,authorized=authorized,validated=validated)
    def rollback(self)->dict[str,Any]: return self.versions.rollback()
    def close(self):
        try:self.rtmo_l4_artifacts.close()
        except Exception:pass
        try:
            if self.online_persistence is not None:self.online_persistence.close(float(self.config.get("tdoc.rtmo.persistence.flush_timeout_s",30.0)))
        except Exception:pass
        try:self.provenance.close(float(self.config.get("tdoc.rtmo.persistence.flush_timeout_s",30.0)))
        except Exception:pass
        for a in self.adapters.values():
            try:a.close()
            except Exception:pass
    def __enter__(self):return self
    def __exit__(self,*exc):self.close()
