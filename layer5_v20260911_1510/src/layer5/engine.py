from __future__ import annotations
import time
from pathlib import Path
from typing import Any
from .config import Layer5Config
from .contract import Layer4ContractAdapter, IntegrityFreshnessGate, AdmissibilitySupervisor
from .compute import EdgeProfileManager
from .decision import ObjectiveNormalizer, LexicographicDecisionEngine, SensitivityEngine, ConfidenceEngine, OperationalModeManager
from .environment import Layer2VirtualEnvironmentAdapter, PTLibJSBSimAdapter
from .execution import DecisionExplainer, Layer6Assembler, Layer7EvidenceRouter
from .experience import EpisodeAggregator
from .governance import GovernanceManager
from .knowledge import KStore, KnowledgeReferenceResolver
from .lifecycle import EnvironmentRouter
from .models import Layer5Result, Decision, ControlAction
from .phoenix import PhoenixSpecialization
from .policy import PolicyAdapter
from .qlpv import QLPVCorrectionKernel
from .registry import CandidateRegistry
from .safety import PredictiveSafetyFilter, BaselineSafeController, RuntimeAssurance
from .tdoc import TDOCManager
from .utils import sha256_obj, finite_float
from .native_pilot_boundary import audit as audit_native_pilot
from .native_readiness import NativeReadinessResult


class Layer5Engine:
    """ERA-qLPV-MPC Layer-5 orchestrator.

    Layer 5 consumes Layer-4 candidates and performs decision/safety/governance only.
    It never generates Pareto points, commits W/K/G, directly emits PWM, or retrains a Twin.
    """
    def __init__(self, config: Layer5Config):
        self.config=config
        self.tdoc=TDOCManager(config)
        self.phoenix=PhoenixSpecialization(config)
        self.adapter=Layer4ContractAdapter(config)
        self.compute_profile=EdgeProfileManager(config)
        self.registry=CandidateRegistry(config)
        self.ci_gate=IntegrityFreshnessGate(config)
        self.adm=AdmissibilitySupervisor(config)
        self.policy_adapter=PolicyAdapter(config)
        self.qlpv=QLPVCorrectionKernel(config,self.phoenix)
        self.normalizer=ObjectiveNormalizer(config)
        self.selector=LexicographicDecisionEngine(config)
        self.sensitivity=SensitivityEngine(config)
        self.confidence=ConfidenceEngine(config)
        self.mode=OperationalModeManager(config)
        self.psf=PredictiveSafetyFilter(config,self.phoenix)
        self.baseline=BaselineSafeController(config)
        self.rta=RuntimeAssurance()
        self.gov=GovernanceManager(config)
        self.explainer=DecisionExplainer()
        self.l6assembler=Layer6Assembler(config)
        self.evidence_router=Layer7EvidenceRouter()
        self.references=KnowledgeReferenceResolver(config)
        self.kstore=KStore(config)
        self.episode=EpisodeAggregator(config)
        self.router=EnvironmentRouter(config)
        self.offline_env=Layer2VirtualEnvironmentAdapter()
        self.online_env: PTLibJSBSimAdapter|None=None
        self.last_safe=None
        self._online_setd_cache:dict[str,Any]|None=None
        self._online_setd_manifest_signature:tuple[int,int]|None=None
        self.last_policy:dict[str,Any]={}; self.last_refs:dict[str,Any]={}; self.last_episode_provenance:dict[str,Any]={}
        self.knowledge_quality_counts={'safety_hold_cycles':0,'verified_candidate_cycles':0,'final_authorized_cycles':0}
        self.native_readiness_checker=None
        self.native_execution_checker=None

    @classmethod
    def from_yaml(cls,path:str|Path)->"Layer5Engine": return cls(Layer5Config.from_yaml(path))

    def register_layer2_twin_adapter(self,twin:str,fn)->None:
        """Register a versioned domain adapter for A2^virt OFFLINE_SE execution."""
        self.offline_env.register(twin,fn)

    def set_online_environment_adapter(self,adapter:PTLibJSBSimAdapter)->None:
        self.online_env=adapter

    def _load_online_setd(self)->dict[str,Any]:
        """Verify/load the immutable Online prior once per committed manifest.

        SETD is guidance, never current actuation authority.  Re-reading and
        reconstructing the same committed package inside every fast transaction
        adds disk latency but no freshness evidence.  A manifest replacement
        invalidates this cache automatically.
        """
        manifest=self.kstore.root/"MANIFEST.json"
        try:
            stat=manifest.stat(); signature=(int(stat.st_mtime_ns),int(stat.st_size))
        except OSError:
            signature=None
        if self._online_setd_cache is None or signature!=self._online_setd_manifest_signature:
            self._online_setd_cache=self.kstore.load_setd()
            self._online_setd_manifest_signature=signature
        return self._online_setd_cache

    @staticmethod
    def _setd_runtime_evidence(setd:dict[str,Any]|None)->dict[str,Any]:
        """Bounded identity/quality evidence for cycle records; full SETD stays in KStore."""
        src=dict(setd or {}); manifest=dict(src.get("manifest") or {})
        knowledge=dict(src.get("knowledge") or {})
        return {
            "ok":bool(src.get("ok")),"reason":src.get("reason"),
            "manifest":{k:manifest.get(k) for k in (
                "schema","package_id","episode_id","committed","experience_count",
                "logical_content_sha256","knowledge_quality",
            ) if k in manifest},
            "knowledge_identity":{k:knowledge.get(k) for k in (
                "schema","episode_id","content_sha256","source_ss_manifest_sha256",
                "model_version","policy_version","experience_count",
            ) if k in knowledge},
            "recovery":src.get("recovery"),
            "full_prior_location":"Layer5 KStore committed SETD package",
            "cached_read_only_prior":True,
            "actuation_authority":False,
        }

    def preflight(self,strict_external:bool=False)->dict[str,Any]:
        t=self.tdoc.probe(); p=self.phoenix.status(); q=self.qlpv.status(); cp=self.compute_profile.plan(); ks=self.kstore.status()
        docs={name:(self.config.package_root/"docs"/name).exists() for name in ["Formalism_layer5_v4.md","TDDT_Dynamic_NTwin_Layer5_details_FA_v2.md","HOW_USAGE_layer4.md","Summary_layer2_layer3_layer4.md"]}
        deps={}
        markers={"TDDT_LAYER4_ROOT":["src/layer4"],"TDDT_LAYER3_ROOT":["src/layer3"],"TDDT_TWINS_ROOT":["fmus/MESA_NAV.fmu","fmus/WingMPET.fmu"],"TDDT_CLIMATE_ROOT":["fmu/MicroMetTwin.fmu"],"TDDT_PTLIB_ROOT":["pt_lib/__init__.py","pt_lib/config.py","pt_lib/controller.py","pt_lib/lifecycle.py","config/pt_lib_sitl.yaml"]}
        for name,rels in markers.items():
            root=self.config.env_path(name); missing=[rel for rel in rels if not root or not (root/rel).exists()]
            deps[name]={"root":str(root) if root else None,"configured":root is not None,"ok":bool(root and not missing),"missing":missing,"required":"mode-dependent"}
        core=all(docs.values()) and bool(p.get("physical_actuators")) and self.registry is not None and self.policy_adapter is not None and self.episode is not None
        # Strict external preflight means TDOC must be usable. Other roots are mode-dependent and
        # are reported rather than globally required because OFFLINE_SE and ONLINE_JSBSIM differ.
        external_ok=t["ok"] if strict_external else True
        confidence_weights=dict(self.config.get("confidence.weights",{}) or {})
        confidence_reward_keys=("separation","margin","full_fidelity","freshness")
        confidence_penalty_keys=("risk","uncertainty","sensitivity")
        reward_weights=[float(confidence_weights.get(k,0.0)) for k in confidence_reward_keys]
        penalty_weights=[float(confidence_weights.get(k,0.0)) for k in confidence_penalty_keys]
        reward_weight_sum=sum(reward_weights)
        weights_nonnegative=all(x>=0.0 for x in reward_weights+penalty_weights)
        normalization_valid=bool(weights_nonnegative and reward_weight_sum>0.0)
        max_possible_confidence=1.0 if normalization_valid else 0.0
        auto_threshold=float(self.config.get("confidence.auto_threshold",0.8))
        auto_reachable=bool(normalization_valid and max_possible_confidence+1e-12>=auto_threshold)
        if not weights_nonnegative: reachability_reason="CONFIDENCE_WEIGHTS_MUST_BE_NONNEGATIVE"
        elif reward_weight_sum<=0.0: reachability_reason="CONFIDENCE_POSITIVE_WEIGHT_SUM_INVALID"
        elif not auto_reachable: reachability_reason="AUTO_CONFIDENCE_UNREACHABLE"
        else: reachability_reason=None
        confidence_reachability={
            "formula":"Formalism_layer5_v4 L5-58N: (positive weighted evidence - nonnegative weighted penalties) / W_positive, clipped to [0,1]",
            "normalization":"W_positive = separation + margin + full_fidelity + freshness weight sum",
            "reward_weight_sum":reward_weight_sum,
            "penalty_weight_sum":sum(penalty_weights),
            "normalization_valid":normalization_valid,
            "max_possible_confidence":max_possible_confidence,
            "auto_threshold":auto_threshold,
            "auto_reachable":auto_reachable,
            "reason":reachability_reason,
            "threshold_relaxed":False,
        }
        component_coverage={
            "CI_guard":True,"candidate_registry":True,"admissibility":True,"policy_guidance_adapter":True,"qlpv_kernel":True,
            "decision_confidence":True,"operational_mode_human_authority":True,"predictive_safety_filter":True,"runtime_assurance":True,
            "governance_provenance":True,"layer6_assembler":True,"single_episode_lifecycle":True,"knowledge_reference_plane":True,
            "SS_SETD_KStore":True,"KStore_segmented_FILE":bool(ks.get("backend")!="FILE" or (not ks.get("monolithic_ss_stream") and ks.get("setd_multi_file"))),"TDDT_KSTORE_ROOT":True,"fast_meso_slow_aggregation":True,"MRG":True,"Phoenix_specialization":True,"Layer7_evidence":True,
            "network_commit":False,"model_retraining":False,"direct_pwm":False
        }
        return {"ok":bool(core and external_ok),"core_resources_ok":core,"tdoc":t,"external_dependencies":deps,"phoenix":p,"qlpv":q,
                "documents":docs,"components":component_coverage,"compute_profile":cp,"knowledge_store":ks,"offline_environment":self.offline_env.status(),"strict_external":strict_external,
                "confidence_reachability":confidence_reachability,
                "native_pilot_controller":audit_native_pilot({},learning_mode="ONLINE_JSBSIM"),
                "preflight_scope":"PACKAGE_AND_EXTERNAL_DEPENDENCY_ONLY_NOT_NATIVE_EXECUTION_READINESS",
                "deployment_note":"Full-envelope/transition and non-zero Phoenix qLPV correction remain fail-closed until their explicit validation evidence is supplied; L5-58N confidence normalization is fail-closed when W_positive is non-positive or any confidence weight is negative; AUTO still requires the configured confidence.auto_threshold and all independent safety/governance gates."}

    def run(self, layer4:dict[str,Any], learning_mode:str|None=None, human_approval:bool|None=None,
            human_approver:str|None=None, knowledge_refs:dict[str,Any]|None=None, now_s:float|None=None,
            policy_update:dict[str,Any]|None=None, execute_environment:bool=False,
            layer2_contexts:dict[str,dict[str,Any]]|None=None, requested_mode:str|None=None)->Layer5Result|NativeReadinessResult:
        if isinstance(layer4,dict) and layer4.get('schema')=='Layer4-native-execution-result-v1':
            from .native_operational import evaluate_native_execution
            return evaluate_native_execution(self,layer4,self.native_execution_checker,
                learning_mode=(learning_mode or self.config.get('lifecycle.learning_mode','OFFLINE_SE')))
        # Read-only reference inspection precedes any SETD/episode admission.
        # A reference result is not adapted into an operational Layer5 candidate.
        if isinstance(layer4,dict) and layer4.get('schema')=='Layer4-native-reference-result-v1':
            from .native_readiness import evaluate_reference_readiness
            return evaluate_reference_readiness(self,layer4,self.native_readiness_checker,
                learning_mode=(learning_mode or self.config.get('lifecycle.learning_mode','OFFLINE_SE')))
        t0=time.perf_counter()
        compute_plan=self.compute_profile.plan()
        lm=(learning_mode or self.config.get("lifecycle.learning_mode","OFFLINE_SE")).upper()
        route=self.router.route(lm)
        snap_v=str(layer4.get("snapshot_version") or ""); snap_h=str(layer4.get("snapshot_hash") or "")
        tdoc_boundary=self.tdoc.validate_layer4_envelope(layer4)
        refs=self.references.resolve(knowledge_refs or self._references_from_layer4(layer4),snap_v,snap_h)
        lifecycle_gate={"ok":True,"reasons":[],"learning_mode":lm,"offline_episode_limit":1,"online_retraining_episodes":0}
        if refs.get("references") and (not refs.get("ok") or not refs.get("coherent") or not refs.get("use_for_normal_decision")):
            lifecycle_gate["ok"]=False
            if not refs.get("ok"): lifecycle_gate["reasons"].append("versioned_hashed_knowledge_reference_required")
            if not refs.get("coherent"): lifecycle_gate["reasons"].append("knowledge_snapshot_incoherent")
            if refs.get("low_coverage"): lifecycle_gate["reasons"].append("TEP_coverage_below_minimum")
        setd={"ok":False,"reason":"not_loaded_offline"}
        if lm=="ONLINE_JSBSIM":
            setd=self._load_online_setd()
            if self.config.get("lifecycle.setd_required_online",True) and not setd.get("ok"):
                lifecycle_gate["ok"]=False; lifecycle_gate["reasons"].append("valid_SETD_required_for_online")
            lifecycle_gate["new_training_episode"]=False; lifecycle_gate["model_retraining_L5"]=False
        elif lm!="OFFLINE_SE":
            lifecycle_gate["ok"]=False; lifecycle_gate["reasons"].append("unsupported_learning_mode")
        # Admit durable storage before any OFFLINE decision is selected. The
        # single-episode limit is intentional: a consumed KStore must fail closed
        # instead of producing FINAL controls that cannot become durable knowledge.
        if lm=="OFFLINE_SE" and tdoc_boundary["ok"] and lifecycle_gate["ok"] and self.kstore.episode_id is None:
            try:
                self.kstore.start_episode()
                lifecycle_gate["episode_admission"]="started"
            except RuntimeError as exc:
                lifecycle_gate["ok"]=False
                lifecycle_gate["episode_admission"]="rejected"
                lifecycle_gate["reasons"].append(f"offline_episode_admission_failed:{exc}")
        latest_mrg=self.episode.mrg_history[-1] if self.episode.mrg_history else None
        policy=self.policy_adapter.adapt(layer4,policy_update,episode_mrg=latest_mrg,setd_prior=setd if lm=="ONLINE_JSBSIM" else None)
        if not tdoc_boundary["ok"] or not lifecycle_gate["ok"]:
            return self._fallback_result(lm,route,snap_v,snap_h,"contract_or_lifecycle_gate",tdoc_boundary,refs,lifecycle_gate,t0,policy=policy,setd=self._setd_runtime_evidence(setd))
        # Native pilot is *not* an actuator request.  In this release Layer4 FF
        # replays model-only FMUs and Layer5 cannot establish the ArduPilot
        # controller/mixer/spool mapping.  No offline FF/cert/SETD/approval or
        # caller-supplied readiness flag may become ONLINE FINAL.  This branch
        # must occur before selecting or constructing any Layer6 package.
        native_boundary=audit_native_pilot(layer4,learning_mode=lm)
        mission_binding=getattr(self,'native_mission_binding',None) if lm=='ONLINE_JSBSIM' else None
        mission_ready=mission_binding.operational_readiness() if mission_binding is not None else None
        if native_boundary["online_native_pilot_required"] and not (mission_ready or {}).get('ok'):
            return self._fallback_result(
                lm,route,snap_v,snap_h,native_boundary["reason"],tdoc_boundary,
                refs,lifecycle_gate,t0,policy=policy,
                setd=self._setd_runtime_evidence(setd),
                extra_gates={"native_pilot_controller":native_boundary,
                             "native_control_ready":False,
                             "native_pilot_FINAL_eligible":False,
                             "FINAL":False},
            )

        if lm=='OFFLINE_SE' and bool(layer4.get('verified_candidates')):
            self.knowledge_quality_counts['verified_candidate_cycles']+=1
        # L5-95 hard compute path starts at CI/ADM. Knowledge/SETD loading, TDOC
        # boundary setup and policy materialization are orchestration/setup work and
        # must not consume the hard CI→ADM→qLPV→PSF→RTA→GOV deadline.
        hard_t0=time.perf_counter()
        candidates=self.registry.load(self.adapter.candidates(layer4))
        admissible=[]; ci_records={}; adm_records={}; bind_records={}; policy_records={}
        for c in candidates:
            c.ci=self.ci_gate.evaluate(c,layer4,now_s); ci_records[c.candidate_id]=c.ci.to_dict()
            seq,bind=self.phoenix.bind_candidate(c.raw); bind_records[c.candidate_id]=bind; c.logical_actions=seq if bind.get("ok") else []
            c.adm=self.adm.evaluate(c,cvar_enabled=compute_plan.get("cvar_enabled")); adm_records[c.candidate_id]=c.adm.to_dict()
            pg=self._candidate_policy_gate(c,policy.get("active_constraints",{})); policy_records[c.candidate_id]=pg
            if c.ci.ok and c.adm.ok and bind.get("ok") and pg["ok"]: admissible.append(c)
        if not admissible:
            return self._fallback_result(lm,route,snap_v,snap_h,"no_admissible_bound_candidate",tdoc_boundary,refs,lifecycle_gate,t0,policy=policy,setd=self._setd_runtime_evidence(setd),
                extra_gates={"candidate_registry":self.registry.status(),"integrity":ci_records,"admissibility":adm_records,"policy_gate":policy_records,"phoenix_binding":bind_records})

        solved=[]; solver_records={}; context=self._context_from_layer4(layer4)
        compute_plan=self.compute_profile.degrade(compute_plan,(time.perf_counter()-hard_t0)*1000.0,float(self.config.get("edge.deadline_ms",75.0)))
        for c in admissible[:int(compute_plan.get("solve_max",self.config.get("edge.solve_max",12)))]:
            c.solver=self.qlpv.solve(c,context=context,guidance=policy.get("gamma")); solver_records[c.candidate_id]=c.solver
            vp=c.solver.get("vp") or {}; c.vp=type(c.ci)(bool(vp.get("ok",False)),list(vp.get("reasons",[])),dict(vp.get("metrics",{})))
            if c.solver.get("ok") and c.vp.ok: solved.append(c)
        if not solved:
            return self._fallback_result(lm,route,snap_v,snap_h,"no_verification_preserving_solver_candidate",tdoc_boundary,refs,lifecycle_gate,t0,policy=policy,setd=self._setd_runtime_evidence(setd),
                extra_gates={"candidate_registry":self.registry.status(),"integrity":ci_records,"admissibility":adm_records,"policy_gate":policy_records,"phoenix_binding":bind_records,"solver":solver_records})

        weights=policy["decision_weights"]
        normalization_initial=self.normalizer.normalize(solved,{})
        preliminary,_=self.selector.rank(solved,weights,{})
        sensitivities=self.sensitivity.compute(preliminary,weights,top_k=int(compute_plan.get("sensitivity_top_k",self.config.get("edge.sensitivity_top_k",3))))
        normalization_final=self.normalizer.normalize(solved,sensitivities)
        ranked,recommendation=self.selector.rank(solved,weights,sensitivities); selected=ranked[0]
        corrected=selected.solver["corrected_actions"]; first=list(corrected[0])
        rho,rho_parts=self.confidence.compute(
            ranked,selected,
            finite_float((sensitivities.get(selected.candidate_id) or {}).get("q_S"),0.0),
            weights=weights,
        )
        risk=selected.violation_probability
        human=bool(self.config.get("operational.human_approval",False) if human_approval is None else human_approval)
        phx_status=self.phoenix.status()
        offline_model_authorized=bool((phx_status.get("runtime_validation_certificate") or {}).get("kind")=="OFFLINE_MODEL" and phx_status.get("nominal_control_ready"))
        fixture_simulation_authorized=bool(self.config.get("testing.allow_fixture_verification",False))
        offline_simulation_authorized=bool(lm=="OFFLINE_SE" and (offline_model_authorized or fixture_simulation_authorized))
        mode_info=self.mode.resolve(rho,risk,bool(self.config.get("operational.autonomous_authorized",True)),human,requested=requested_mode,
                                    learning_mode=lm,simulation_authorized=offline_simulation_authorized)
        human_state={"approval":human,"approver":human_approver,"required":mode_info.get("human_required"),"mode":mode_info.get("mode")}
        explanation=self.explainer.build(selected=selected,ranking=[c.candidate_id for c in ranked],recommendation=recommendation.to_dict(),confidence=rho,
                                         active_constraints=policy.get("active_constraints",{}),human=human_state,policy=policy)
        budget_deadline_ms=float(self.config.get("edge.deadline_ms",75.0))
        psf=self.psf.filter(first,selected.vp.ok,policy.get("active_constraints",{})); fallback=self.baseline.action(self.last_safe)
        base_preconditions=bool(selected.ci.ok and selected.adm.ok and selected.vp.ok and policy_records[selected.candidate_id]["ok"])
        # If the hard path is already late before RTA, fail closed immediately.
        # Otherwise RTA executes and its own cost is included in the final L5-95 check.
        budget_pre_rta_ms=(time.perf_counter()-hard_t0)*1000.0
        budget_pre_rta_ok=budget_pre_rta_ms<=budget_deadline_ms
        rta=self.rta.choose(psf,fallback,bool(base_preconditions and budget_pre_rta_ok))
        evidence_type="offline_simulation_reference" if lm=="OFFLINE_SE" else "online_SITL_evidence"
        gov=self.gov.authorize(mode_info,selected.candidate_id,snap_v,snap_h,rho,rho_parts,human_approver,weights,policy,evidence_type,explanation)
        budget_elapsed_ms=(time.perf_counter()-hard_t0)*1000.0
        budget_ok=budget_elapsed_ms<=budget_deadline_ms
        preconditions=bool(base_preconditions and budget_ok)
        final_gate=bool(preconditions and psf["gate"] and rta["source"]=="advanced" and gov["gate"])
        if lm=='OFFLINE_SE' and final_gate:
            self.knowledge_quality_counts['final_authorized_cycles']+=1
        apply=time.time(); deadline=apply+float(self.config.get("contract.max_decision_age_s",5.0))
        control=None; l6=None
        selected_phase=str(selected.raw.get("tdoc_phase") or selected.raw.get("phase") or "").strip().lower()
        constraints={"pilot_domain":{"a":[-1,1],"e":[-1,1],"t":[0,1],"r":[-1,1]},"active":policy.get("active_constraints",{}),
                     "physical":selected.solver.get("physical_tube",{}),"direct_pwm":False,"layer6_revalidation_required":True,
                     "tdoc_phase":selected_phase or None,
                     "command_semantics":"PILOT_LOGICAL_AILERON_ELEVATOR_THROTTLE_RUDDER",
                     "command_domain":"pilot_reference_not_post_mixer_actuator",
                     "model_scope":"OFFLINE_MODEL_ONLY",
                     "native_execution_authorized":False}
        if final_gate:
            # receding-horizon invariant: only u_{k|k} is exposed as a committable decision action.
            control=ControlAction(first,self.phoenix.LOGICAL_ORDER,constraints,apply,deadline,"ERA-qLPV-MPC/PSF/RTA")
            self.last_safe=list(first)
            if lm=="ONLINE_JSBSIM":
                safety={"psf":True,"rta":True,"fallback":"baseline_safe_controller","layer6_device_gate_required":True}
                provenance=dict(gov['provenance'])
                authorization=None
                if mission_binding is not None:
                    from .execution import mission_execution_intent,_SITLMissionAuthorization
                    status=self.native_status_source()
                    intent=mission_execution_intent(selected_phase,status)
                    provenance.update(execution_intent=intent,execution_scope='SITL_CLOSED_LOOP',
                        native_controller_certified=False,flight_certified=False)
                    constraints.update(model_scope='TRANSDOMAIN_MODEL_PREDICTION_NATIVE_EXECUTION_DIAGNOSTIC',
                        native_execution_authorized=True,execution_profile='closed_loop_sitl')
                    args=dict(command=first,actuator_targets=self.phoenix.PHYSICAL_ACTUATORS,
                        constraints=constraints,safety_policy=safety,apply_time=apply,
                        deadline_time=deadline,confidence=rho,provenance=provenance)
                    authorization=_SITLMissionAuthorization(args,selected=selected,
                        final_gate=final_gate,intent=intent)
                l6=self.l6assembler.assemble(first,self.phoenix.PHYSICAL_ACTUATORS,constraints,
                    safety,apply,deadline,rho,provenance,execution_authorization=authorization)
        decision=Decision(gov["decision_id"],selected.candidate_id,mode_info["mode"],rho,{"governance":gov["gate"],"human":human,"human_required":mode_info.get("human_required"),"final_gate":final_gate})

        environment_feedback={"executed":False,"mode":lm,"reason":"execution_not_requested"}
        layer7_evidence={}
        if execute_environment:
            if not final_gate:
                environment_feedback={"executed":False,"mode":lm,"reason":"final_safety_governance_gate_false"}
            elif lm=="OFFLINE_SE":
                if self.offline_env.ready():
                    out=self.offline_env.rollout({"command":first,"decision_id":gov["decision_id"],"candidate_id":selected.candidate_id},layer2_contexts)
                    environment_feedback={"executed":True,"mode":lm,"backend":"WingMPET+MESA_NAV+Climate","layer2_twin_outputs":out,"layer6_call":False,"jsbsim_call":False}
                else:
                    environment_feedback={"executed":False,"mode":lm,"reason":"A2virt_three_twin_adapters_not_registered","adapter_status":self.offline_env.status(),"layer6_call":False,"jsbsim_call":False}
            else:
                env=self.online_env or PTLibJSBSimAdapter(self.config); self.online_env=env
                try:
                    out=env.step(first); environment_feedback={"executed":True,"mode":lm,"backend":"PTLIB_Adapter∘JSBSim_L6",**out}
                    layer7_evidence=self.evidence_router.build(decision_id=gov["decision_id"],candidate_id=selected.candidate_id,result_kind="ONLINE_JSBSIM",
                        observations=out.get("sensor_observation"),truth=out.get("environment_truth"),events=out.get("simulator_event"),knowledge_faults=refs.get("errors",[]))
                except Exception as e:
                    environment_feedback={"executed":False,"mode":lm,"backend":"PTLIB_Adapter∘JSBSim_L6","reason":repr(e)}

        # Single-episode streaming experience. Durable writes happen after the hard decision/safety path.
        exp=self._experience_record(layer4,selected,first if final_gate else rta.get("command"),rho,policy,gov,environment_feedback)
        kstatus=self.kstore.status(); experience_events={"recorded":False,"reason":"online_no_persistent_write" if lm=="ONLINE_JSBSIM" else None}
        if lm=="OFFLINE_SE":
            if self.kstore.episode_id is not None:
                try:
                    self.kstore.append(exp,kind="FAST_EXPERIENCE",cycle=self.episode.fast_count+1,provenance=gov["provenance"],data_version=refs.get("knowledge_version"))
                    agg=self.episode.append_fast(exp,refs); experience_events={"recorded":True,"fast":exp,"aggregation":agg}
                    if agg.get("meso"):
                        self.kstore.append({"aggregate":agg["meso"],"mrg":agg.get("mrg")},kind="MESO_AGGREGATE",provenance=gov["provenance"],data_version=refs.get("knowledge_version"))
                        self.kstore.checkpoint({"meso":agg["meso"],"mrg":agg.get("mrg")})
                    if agg.get("slow"):
                        self.kstore.append({"aggregate":agg["slow"]},kind="SLOW_AGGREGATE",provenance=gov["provenance"],data_version=refs.get("knowledge_version"))
                    kstatus=self.kstore.status()
                except Exception as e:
                    experience_events={"recorded":False,"critical_experience_loss":True,"reason":repr(e)}; kstatus=self.kstore.status()
                    # I26: critical experience loss marks the episode incomplete and is surfaced to Layer 7.
                    layer7_evidence=self.evidence_router.build(decision_id=gov["decision_id"],candidate_id=selected.candidate_id,result_kind="KSTORE_FAULT",
                        observations=None,truth=None,events=None,knowledge_faults=[repr(e)])
            else:
                experience_events={"recorded":False,"critical_experience_loss":True,"reason":"offline_episode_not_active"}

        if not layer7_evidence and refs.get("layer7_evidence"):
            layer7_evidence=self.evidence_router.build(decision_id=gov["decision_id"],candidate_id=selected.candidate_id,result_kind="KNOWLEDGE_EVIDENCE",
                observations=None,truth=None,events=None,knowledge_faults=[str(x) for x in refs.get("layer7_evidence",[])])

        gates={"tdoc_boundary":tdoc_boundary,"lifecycle":lifecycle_gate,"knowledge":refs,"candidate_registry":self.registry.status(),"integrity":ci_records,
               "admissibility":adm_records,"policy_gate":policy_records,"phoenix_binding":bind_records,"solver":solver_records,
               "normalization":{"initial":normalization_initial,"final":normalization_final},"sensitivity":sensitivities,"compute_profile":compute_plan,
               "budget":{"ok":budget_ok,"elapsed_hard_path_ms":budget_elapsed_ms,"elapsed_before_rta_ms":budget_pre_rta_ms,"deadline_ms":budget_deadline_ms,
                         "scope":"L5-95: CI+ADM+qLPV+PSF+RTA+GOV; orchestration/knowledge setup excluded","safety_bounds_relaxed":False,
                         "physical_validation_gates_relaxed":False},"psf":psf,"rta":rta,"governance":gov,"FINAL":final_gate}
        prov={"layer":"5","formalism":"Formalism_layer5_v4","base_document":"TDDT_Dynamic_NTwin_Layer5_details_FA_v2","elapsed_ms":(time.perf_counter()-t0)*1000,
              "read_only_network":True,"only_first_mpc_move":True,"pareto_generation":False,"full_fidelity_simulation_in_L5":False,"topology_search_in_L5":False,
              "model_retraining":False,"layer4_input_digest":sha256_obj(layer4),"environment_route":route,"decision":gov["provenance"],
              "evidence_type":evidence_type,"offline_confidence_is_operational":False if lm=="OFFLINE_SE" else None,"compute_profile":compute_plan}
        self.last_policy=dict(policy); self.last_refs=dict(refs); self.last_episode_provenance=dict(gov.get("provenance") or {})
        compliance=self._compliance(final_gate,selected,gates,policy,explanation,l6,route,refs,kstatus)
        return Layer5Result(ok=True,learning_mode=lm,operational_mode=mode_info["mode"],snapshot_version=snap_v,snapshot_hash=snap_h,
            recommendation=recommendation.to_dict(),decision=decision.to_dict(),control_action=control.to_dict() if control else None,layer6_package=l6.to_dict() if l6 else None,
            gates=gates,fallback={"used":not final_gate,**fallback},knowledge={"references":refs,"kstore":kstatus,"setd":self._setd_runtime_evidence(setd),"online_persistent_write":False,
                    "reference_lookup_bounded":True,"raw_TEP_rewrite":False},tdoc=self.tdoc.probe(),phoenix=self.phoenix.status(),provenance=prov,
            policy=policy,explanation=explanation,layer7_evidence=layer7_evidence,experience=experience_events,environment_feedback=environment_feedback,
            candidate_registry=self.registry.status(),compliance=compliance)

    def finalize_offline_episode(self,knowledge:dict[str,Any]|None=None)->dict[str,Any]:
        if self.kstore.status().get("critical_loss"):
            return {"committed":False,"ok":False,"reason":"critical_experience_loss_episode_incomplete","status":self.kstore.status()}
        summary=self.episode.summary()
        supplied=dict(knowledge or {})
        q=dict(self.knowledge_quality_counts)
        supplied_q=dict(supplied.get("knowledge_quality") or {})
        q.update(supplied_q)
        for k in ("safety_hold_cycles","verified_candidate_cycles","final_authorized_cycles","closed_loop_effect_cycles","joint_control_proof_cycles","control_learning_cycles","non_control_learning_cycles","full_fidelity_runtime_error_cycles"):
            try:q[k]=int(q.get(k,0) or 0)
            except Exception:q[k]=0
        q["safety_knowledge_valid"]=bool(q["safety_hold_cycles"]>0 or q["final_authorized_cycles"]>0)
        global_control_evidence=bool(q["verified_candidate_cycles"]>0 and q["final_authorized_cycles"]>0 and q["closed_loop_effect_cycles"]>0 and q["full_fidelity_runtime_error_cycles"]==0)
        phase_cov=q.get("phase_control_coverage") if isinstance(q.get("phase_control_coverage"),dict) else {}
        phase_gate_present="all_required_control_phases" in phase_cov
        # Solution-14: absence is NOT evidence.  TDOC must explicitly supply
        # phase-level coverage; otherwise Layer5 finalization stays fail-closed.
        phase_complete=bool(phase_cov.get("all_required_control_phases")) if phase_gate_present else False
        supplied_gate_present="control_knowledge_valid" in supplied_q
        supplied_control_valid=bool(supplied_q.get("control_knowledge_valid")) if supplied_gate_present else False
        # Monotonic validity contract: Layer5 may only preserve/downgrade TDOC's
        # explicit claim.  It can never synthesize true from counters when TDOC
        # omitted the gate, supplied false, or phase coverage is incomplete.
        monotonic_input_ready=bool(supplied_gate_present and phase_gate_present and supplied_control_valid and phase_complete)
        q["control_knowledge_valid"]=bool(monotonic_input_ready and global_control_evidence)
        q["control_knowledge_valid_inputs"]={
            "contract":"Layer5-offline-control-validity-monotonic-v1",
            "global_control_evidence":global_control_evidence,
            "supplied_tdoc_control_valid_present":supplied_gate_present,
            "supplied_tdoc_control_valid":supplied_control_valid if supplied_gate_present else None,
            "phase_control_coverage_present":phase_gate_present,
            "all_required_control_phases":phase_complete if phase_gate_present else None,
            "explicit_inputs_complete":bool(supplied_gate_present and phase_gate_present),
            "monotonic_input_ready":monotonic_input_ready,
            "false_to_true_upgrade_allowed":False,
        }
        q["control_readiness_semantics"]="requires explicit TDOC control-validity=true + explicit complete required-phase same-cycle joint proof + global verified/FINAL/control-effect evidence with zero FF runtime errors; missing inputs and explicit false are fail-closed, and Layer5 may only downgrade true to false"
        payload={"policy_version":((self.last_policy.get("theta") or {}).get("version") if self.last_policy else self.config.get("governance.policy_version")),
                 "source":"Layer5 single-episode Fast/Meso/Slow aggregation","episode_summary":summary,"bounded_ram":True,"full_episode_in_ram":False,
                 "decision_weights":dict(self.last_policy.get("decision_weights") or {}),"Theta5":dict(self.last_policy.get("theta") or {}),
                 "CRP_SARG_TEP_references":dict(self.last_refs.get("references") or {}),"knowledge_version":self.last_refs.get("knowledge_version"),
                 "knowledge_hash":self.last_refs.get("knowledge_hash"),"MRG_history":summary.get("mrg",[]),"episode_provenance":dict(self.last_episode_provenance),
                 "knowledge_quality":q}
        supplied.pop("knowledge_quality",None); payload.update(supplied)
        out=self.kstore.finalize(payload)
        if isinstance(out,dict): out["knowledge_quality"]=q; out["ok"]=bool(out.get("committed",True))
        return out

    def _candidate_policy_gate(self,c,active:dict[str,Any])->dict[str,Any]:
        reasons=[]; metrics={}
        jc=max([finite_float(n.get("J_C"),0.0) for n in c.network_trajectory if isinstance(n,dict)] or [0.0])
        mu=min([finite_float(n.get("mu"),0.0) for n in c.network_trajectory if isinstance(n,dict)] or [0.0])
        risk=c.violation_probability; metrics={"risk":risk,"J_C_peak":jc,"mu_min":mu}
        for key,cmp,val in (("risk_max","max",risk),("J_C_max","max",jc),("mu_min","min",mu)):
            if key not in active: continue
            try:
                bound=float(active[key])
                if (cmp=="max" and val>bound) or (cmp=="min" and val<bound): reasons.append(f"active_policy_{key}")
            except Exception: reasons.append(f"active_policy_{key}_invalid")
        return {"ok":not reasons,"reasons":reasons,"metrics":metrics,"unknown_constraints_forwarded_to_L6":sorted(set(active)-{"risk_max","J_C_max","mu_min","throttle_max","abs_rudder_max"})}

    def _context_from_layer4(self,d:dict[str,Any])->dict[str,Any]:
        p=d.get("provenance") or {}; return {"snapshot_version":d.get("snapshot_version"),"snapshot_hash":d.get("snapshot_hash"),
            "knowledge":p.get("knowledge") or {},"budget":d.get("budget") or {},"robust_summary":d.get("robust_summary") or {},"source":"Layer4_read_only"}

    def _experience_record(self,layer4,selected,command,rho,policy,gov,environment_feedback)->dict[str,Any]:
        net=selected.network_trajectory or []; last=net[0] if net and isinstance(net[0],dict) else {}
        return {"schema":"L5-fast-experience-v1","snapshot_version":selected.snapshot_version,"snapshot_hash":selected.snapshot_hash,"candidate_id":selected.candidate_id,
                "u_exec":list(command) if isinstance(command,list) else command,"environment_executed":bool(environment_feedback.get("executed")),
                "environment_mode":environment_feedback.get("mode"),"objectives":dict(selected.objective_vector),"risk":selected.violation_probability,
                "uncertainty":(selected.adm.metrics if selected.adm else {}).get("mean_cov_trace",0.0),"decision_confidence":rho,
                "network":{"J_C":last.get("J_C"),"mu":last.get("mu"),"W_epi":last.get("W") or last.get("weights"),"K":last.get("K") or last.get("couplings")},
                "Gamma":policy.get("gamma"),"Theta5":policy.get("theta"),"provenance":gov.get("provenance"),"raw_dataset_scan":False}

    def _record_offline_hold_experience(self,*,snap_v:str,snap_h:str,reason:str,refs:dict[str,Any],policy:dict[str,Any],
                                        provenance:dict[str,Any],extra_gates:dict[str,Any]|None=None)->dict[str,Any]:
        """Persist valid OFFLINE_SE HOLD decisions as single-episode learning evidence.

        HOLD is a valid Layer-5 decision outcome. It must never become actuation, but
        discarding every HOLD cycle prevents SS/SETD finalization in the intentionally
        fail-closed Phoenix deployment. Contract/lifecycle-invalid cycles are filtered
        by the caller and remain non-trainable.
        """
        extra_gates=extra_gates or {}
        network={}
        # Preserve any already-computed network/decision context without inventing data.
        for group in ("admissibility","policy_gate"):
            rows=extra_gates.get(group)
            if isinstance(rows,dict):
                for rec in rows.values():
                    metrics=(rec or {}).get("metrics") if isinstance(rec,dict) else None
                    if isinstance(metrics,dict):
                        if network.get("J_C") is None and metrics.get("J_C_peak") is not None: network["J_C"]=metrics.get("J_C_peak")
                        if network.get("mu") is None and metrics.get("mu_min") is not None: network["mu"]=metrics.get("mu_min")
        exp={"schema":"L5-fast-hold-experience-v1","snapshot_version":snap_v or None,"snapshot_hash":snap_h or None,
             "candidate_id":None,"u_exec":None,"environment_executed":False,"environment_mode":"OFFLINE_SE",
             "objectives":{},"risk":None,"uncertainty":None,"decision_confidence":0.0,"network":network,
             "Gamma":policy.get("gamma") if isinstance(policy,dict) else None,"Theta5":policy.get("theta") if isinstance(policy,dict) else None,
             "provenance":provenance,"hold_reason":reason,"actuation_executed":False,"training_role":"decision_hold_knowledge",
             "raw_dataset_scan":False}
        if self.kstore.episode_id is None:
            try: self.kstore.start_episode()
            except RuntimeError as exc:
                return {"recorded":False,"reason":repr(exc),"fast":exp}
        if self.kstore.episode_id is None:
            return {"recorded":False,"reason":"offline_episode_not_active","fast":exp}
        try:
            self.kstore.append(exp,kind="FAST_HOLD_EXPERIENCE",cycle=self.episode.fast_count+1,provenance=provenance,data_version=refs.get("knowledge_version"))
            self.knowledge_quality_counts['safety_hold_cycles']+=1
            agg=self.episode.append_fast(exp,refs); out={"recorded":True,"fast":exp,"aggregation":agg,"hold":True}
            if agg.get("meso"):
                self.kstore.append({"aggregate":agg["meso"],"mrg":agg.get("mrg")},kind="MESO_AGGREGATE",provenance=provenance,data_version=refs.get("knowledge_version"))
                self.kstore.checkpoint({"meso":agg["meso"],"mrg":agg.get("mrg")})
            if agg.get("slow"):
                self.kstore.append({"aggregate":agg["slow"]},kind="SLOW_AGGREGATE",provenance=provenance,data_version=refs.get("knowledge_version"))
            self.last_policy=dict(policy or {}); self.last_refs=dict(refs or {}); self.last_episode_provenance=dict(provenance or {})
            return out
        except Exception as exc:
            return {"recorded":False,"critical_experience_loss":True,"reason":repr(exc),"fast":exp}

    def _references_from_layer4(self,d:dict[str,Any])->dict[str,Any]:
        k=(d.get("provenance") or {}).get("knowledge") or {}; out={}
        sarg=k.get("sarg_reference")
        if isinstance(sarg,dict):
            rec={"id":sarg.get("sarg_id") or (sarg.get("pattern") or {}).get("sarg_id"),"version":sarg.get("version"),"hash":sarg.get("hash") or (sarg.get("dataset_contract") or {}).get("hash"),
                 "guidance":sarg.get("guidance") or {},"snapshot_version":sarg.get("snapshot_version"),"snapshot_hash":sarg.get("snapshot_hash")}
            if all(rec.get(x) for x in ("id","version","hash")): out["SARG"]=rec
        tep=k.get("tep_reference") or k.get("tep_references")
        if isinstance(tep,(dict,list)) and tep: out["TEP"]=tep
        crp_id=k.get("crp_id")
        rec={"id":str(crp_id) if crp_id is not None else None,"version":k.get("crp_version"),"hash":k.get("crp_hash"),"guidance":k.get("crp_guidance") or {}}
        if all(rec.get(x) for x in ("id","version","hash")): out["CRP"]=rec
        return out

    def _compliance(self,final_gate,selected,gates,policy,explanation,l6,route,refs,kstatus)->dict[str,Any]:
        base={
            "A_input":bool(gates.get("tdoc_boundary",{}).get("ok")),"A_verified":bool(selected.verified),"A_multi":True,
            "A_risk":bool(selected.adm and "P_viol" in selected.adm.metrics),"A_network":bool(selected.adm and "J_C_peak" in selected.adm.metrics and "mu_min" in selected.adm.metrics),
            "A_confidence":True,"A_safety":bool(gates.get("psf") and gates.get("rta")),"A_trace":bool(gates.get("governance",{}).get("traceable")),
            "A_L6":bool(route.get("layer6_call") is False or l6 is not None or not final_gate)
        }
        formalism={
            "core_25_25_implemented":True,"SE_TDDT_v4_8_8_implemented":True,"Knowledge_v4_12_12_implemented":True,"Phoenix_v4_12_12_implemented":True,
            "qLPV_solver_is_intentional_subset":True,"read_only_WKG":True,"only_first_move":True,"no_model_retraining":True,"no_direct_pwm":True,
            "policy_guidance":bool(policy),"reason_package":bool(explanation),"bounded_reference_lookup":bool(refs.get("lookup_method")),
            "KStore_recovery":True,"KStore_backend":kstatus.get("backend"),"KStore_root_configurable":bool(kstatus.get("environment_variable")=="TDDT_KSTORE_ROOT"),"KStore_segmented_file_layout":bool(kstatus.get("backend")!="FILE" or (not kstatus.get("monolithic_ss_stream") and kstatus.get("setd_multi_file"))),"fast_meso_slow_MRG":True,"Layer7_authority_for_persistent_update":True,
            "full_envelope_fail_closed_without_evidence":not self.phoenix.full_envelope or self.phoenix.transition_validated
        }
        return {"base_document":base,"base_compliance_5":all(base.values()),"formalism_v4":formalism,"implementation_coverage":all(formalism.values()),
                "runtime_FINAL":bool(final_gate),"note":"Coverage means implemented and routed. Deployment-only advanced Phoenix modes remain intentionally fail-closed until evidence gates pass."}

    def _fallback_result(self,lm,route,snap_v,snap_h,reason,tdoc_boundary,refs,lifecycle,t0,extra_gates=None,policy=None,setd=None):
        fb=self.baseline.action(self.last_safe); mode="HOLD"; did="L5D-FALLBACK-"+sha256_obj([snap_v,snap_h,reason])[:12]
        decision=Decision(did,None,mode,0.0,{"final_gate":False,"reason":reason})
        gates={"tdoc_boundary":tdoc_boundary,"lifecycle":lifecycle,"knowledge":refs,"FINAL":False}; gates.update(extra_gates or {})
        prov={"layer":"5","formalism":"Formalism_layer5_v4","base_document":"TDDT_Dynamic_NTwin_Layer5_details_FA_v2","reason":reason,"elapsed_ms":(time.perf_counter()-t0)*1000,
              "read_only_network":True,"only_first_mpc_move":True,"pareto_generation":False,"model_retraining":False,"direct_pwm":False,"environment_route":route}
        # Valid fail-closed HOLD is still decision knowledge. Invalid contract/lifecycle
        # input remains non-trainable and never starts the single offline episode.
        trainable_hold=bool(lm=="OFFLINE_SE" and tdoc_boundary.get("ok") and lifecycle.get("ok") and reason in {"no_admissible_bound_candidate","no_verification_preserving_solver_candidate"})
        experience={"recorded":False,"reason":"hold_not_trainable"}
        if trainable_hold:
            experience=self._record_offline_hold_experience(snap_v=snap_v,snap_h=snap_h,reason=reason,refs=refs,policy=policy or {},provenance=prov,extra_gates=extra_gates)
        kstatus=self.kstore.status()
        return Layer5Result(ok=True,learning_mode=lm,operational_mode=mode,snapshot_version=snap_v or None,snapshot_hash=snap_h or None,
            recommendation={"ranking":[],"scores":{},"tradeoff":{"reason":reason},"evidence":{}},decision=decision.to_dict(),control_action=None,layer6_package=None,
            gates=gates,fallback={"used":True,"reason":reason,**fb},knowledge={"references":refs,"kstore":kstatus,"setd":setd or {},"online_persistent_write":False,
            "hold_knowledge_recorded":bool(experience.get("recorded"))},tdoc=self.tdoc.probe(),phoenix=self.phoenix.status(),provenance=prov,policy=policy or {},
            experience=experience,environment_feedback={"executed":False,"mode":lm,"reason":"layer5_hold_no_actuation"},candidate_registry=self.registry.status(),
            compliance={"runtime_FINAL":False,"fallback_is_required_behavior":True,"reason":reason,"hold_decision_knowledge_valid":trainable_hold})
