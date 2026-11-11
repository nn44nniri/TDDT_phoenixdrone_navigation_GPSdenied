from __future__ import annotations

import copy
import math
from typing import Any, Mapping

from .aggregation import FastToMesoAggregator, MesoToSlowAggregator
from .alignment import STDAlignment
from .config import Layer3Config
from .consistency import ConsistencyManager
from .coupling import CouplingManager, CouplingOperatorRegistry
from .dependency import DependencyRegistry
from .emergence import EmergenceMonitor, HiddenLoopMonitor
from .guidance import MesoGuidanceManager, SafeCommitManager, SlowPolicyManager
from .knowledge import CRPRepository, SARGRepository
from .management import (AntiChatteringManager, DecisionCoordinator, FeedbackRouter,
                         HealthManager, ProvenanceManager, TwinRegistry, VersionManager)
from .models import NetworkSnapshot, StepResult
from .stability import StabilityMonitor
from .scheduler import TimeScheduler
from .stores import JSONLStore, SnapshotStore
from .tdoc_adapter import TDOCAdapter
from .topology import TopologyManager
from .utils import jsonable, safe_float
from .weights import WeightManager


class Layer3Engine:
    """TDDT Dynamic N-Twin Layer 3 (3.1/3.2/3.3).

    Network execution is two phase: plan_network computes STD/W/G/K/Phi first;
    an external runtime may then execute destination couplings, and step assesses
    the resulting packets using that exact plan.
    """
    def __init__(self,config:Layer3Config):
        self.config=config; self.dependencies=DependencyRegistry(config)
        strict=bool(config.runtime.get('strict_dependency_validation',True))
        self.python_dependency_status=self.dependencies.validate_python_runtime(strict=bool(config.runtime.get('strict_python_dependency_validation',strict)))
        path=self.dependencies.validate(strict=strict); api=self.dependencies.validate_api_contracts(strict=strict); ds=self.dependencies.validate_datasets(strict=strict)
        self.dependency_status={**path,'api_contracts':api,'datasets':ds,'all_ok':bool(path.get('all_ok') and api.get('all_ok') and ds.get('all_ok'))}
        self.tdoc=TDOCAdapter(config); self.registry=TwinRegistry(config); self.provenance=ProvenanceManager(config)
        self.health_manager=HealthManager(config); self.feedback_router=FeedbackRouter(); self.decision_coordinator=DecisionCoordinator()
        self.version_manager=VersionManager(config,config.policy); self.anti_chattering=AntiChatteringManager(config.network.chatter_max_changes_per_window)
        self.alignment=STDAlignment(config,self.tdoc,self.registry); self.weights=WeightManager(config); self.topology=TopologyManager(config)
        self.couplings=CouplingManager(config,self.topology); self.coupling_operators=CouplingOperatorRegistry()
        self.consistency=ConsistencyManager(config,self.registry); self.stability=StabilityMonitor(config); self.emergence=EmergenceMonitor(); self.hidden_loop=HiddenLoopMonitor(config)
        self.crp=CRPRepository(config); self.sarg=SARGRepository(config); self.fast_to_meso=FastToMesoAggregator(); self.meso_to_slow=MesoToSlowAggregator()
        self.mrg_manager=MesoGuidanceManager(config); self.slow_manager=SlowPolicyManager(config); self.safe_commit_manager=SafeCommitManager(bool(config.runtime.get('require_structural_authorization',True)))
        self.time_scheduler=TimeScheduler(config); self.ss_store=JSONLStore(config.resolve(config.paths.ss_kstore)); self.setd_store=JSONLStore(config.resolve(config.paths.setd_kstore)); self.snapshot_store=SnapshotStore(config.resolve(config.paths.snapshots_dir))
        self.fast_buffer=[]; self.meso_buffer=[]; self.fast_index=self.meso_index=self.slow_index=0
        self.current_meso_guidance={'window_index':0,'transient_coupling_delta':{},'risk_signal':0.0,'load_signal':0.0,'provenance':{'initial':True}}
        self.current_slow_policy=copy.deepcopy(config.policy); self.last_slow_knowledge={}; self.at_synchronization_barrier=True

    def plan_network(self,packets:Mapping[str,Mapping[str,Any]]|list[Mapping[str,Any]],*,reference_time_s:float,
                     context:Mapping[str,Any]|None=None,safety_gates:Mapping[str,bool]|None=None,
                     constraints:Mapping[str,Any]|None=None,risks:Mapping[str,Any]|None=None,objectives:Mapping[str,Any]|None=None)->dict[str,Any]:
        registry_status=self.registry.validate_all(packets)
        unhealthy={k for k,v in registry_status['details'].items() if not v['ok']} if bool(self.config.runtime.get('failure_containment',True)) else set()
        std=self.alignment.build_std(packets,float(reference_time_s),context=context,constraints=constraints,risks=risks,objectives=objectives)
        weights,wdetail=self.weights.compute(std,self.current_slow_policy)
        graph=self.topology.active_graph(std,weights,safety_gates,unhealthy_nodes=unhealthy)
        K,kdetail=self.couplings.compute(std,weights,graph,self.current_slow_policy,self.current_meso_guidance.get('transient_coupling_delta') or {})
        pre_coc=self.consistency.evaluate(std,weights,graph,K)
        effects=self.coupling_operators.apply(std,graph,K,coupling_details=kdetail,edge_residuals=pre_coc.get('edge_residuals') or {})
        return {'reference_time_s':float(reference_time_s),'std':std,'weights':weights,'weight_details':wdetail,'graph':graph,
                'couplings':K,'coupling_details':kdetail,'coupling_effects':effects,'registry':registry_status,'unhealthy_nodes':sorted(unhealthy),
                'pre_coupling_coc':pre_coc,'formula':'3.2-1..3.2-11: compute G/K/Phi before destination execution'}

    def _hold_last_safe(self,reference_time_s:float,fast_record:dict[str,Any],health:dict[str,Any])->StepResult|None:
        prev=self.health_manager.last_safe_snapshot
        if not prev:return None
        data=copy.deepcopy(prev)
        # Historical safe data must keep its original observation epoch. This is
        # diagnostic-only; do NOT refresh stale evidence or preserve an executable action.
        data['health']=dict(health)
        data['health']['execution_eligible']=False
        data['decision_route']={'destination':'Layer5','layer3_executes_actuator':False,
                                'prediction_candidate':{},'action_candidate':{},
                                'status':'HELD_DIAGNOSTIC_ONLY'}
        data.setdefault('provenance',{})['hold_last_safe']=True
        data['provenance']['held_at_reference_time_s']=float(reference_time_s)
        data['provenance']['original_reference_time_s']=prev.get('reference_time_s')
        return StepResult(snapshot=NetworkSnapshot(**data),fast_record=fast_record)

    def step(self,packets:Mapping[str,Mapping[str,Any]]|list[Mapping[str,Any]],*,reference_time_s:float,context:Mapping[str,Any]|None=None,
             regime_id:int|None=None,stage:str|int='cruise',prediction:Mapping[str,Any]|None=None,action:Mapping[str,Any]|None=None,
             safety_gates:Mapping[str,bool]|None=None,constraints:Mapping[str,Any]|None=None,risks:Mapping[str,Any]|None=None,
             objectives:Mapping[str,Any]|None=None,anchor_results:Mapping[str,Any]|None=None,runtime_couplings:Mapping[str,Any]|None=None,
             network_plan:Mapping[str,Any]|None=None)->StepResult:
        clock_fast=self.time_scheduler.fast_tick(float(reference_time_s)); self.fast_index=int(clock_fast['fast_index']); self.at_synchronization_barrier=False
        ctx=dict(context or {}); ctx.setdefault('regime_id',regime_id); ctx.setdefault('stage',stage)
        plan=dict(network_plan or self.plan_network(packets,reference_time_s=reference_time_s,context=ctx,safety_gates=safety_gates,constraints=constraints,risks=risks,objectives=objectives))
        registry_status=self.registry.validate_all(packets)
        std=self.alignment.build_std(packets,float(reference_time_s),context=ctx,constraints=constraints,risks=risks,objectives=objectives,
            control_status={'environmental_anchor_candidates':dict(anchor_results or {}),'runtime_couplings':dict(runtime_couplings or {}),
                            'coupling_plan':{'graph':plan['graph'],'couplings':plan['couplings'],'details':plan['coupling_details']}})
        weights=dict(plan['weights']); wdetail=dict(plan['weight_details']); graph=dict(plan['graph']); K=dict(plan['couplings']); kdetail=dict(plan['coupling_details']); effects=dict(plan['coupling_effects'])
        coc=self.consistency.evaluate(std,weights,graph,K); stability=self.stability.evaluate(ctx,graph,K)
        rt=dict(runtime_couplings or {}); emergence=self.emergence.evaluate(weights,K,coc.get('mismatch'),graph,counterfactuals=rt.get('counterfactuals'))
        hidden=self.hidden_loop.evaluate(K,coc.get('edge_normalized_residuals') or {},graph,counterfactual_states=rt.get('counterfactual_states'))
        mm=coc.get('mismatch'); mismatch_for_route=float(mm) if mm is not None and math.isfinite(float(mm)) else 0.0
        feedback=self.feedback_router.route(std=std.to_dict(),mismatch=mismatch_for_route,stability=stability,hidden_loop=hidden); decision=self.decision_coordinator.route(prediction,action)
        gate_fraction=sum(1 for r in std.representations.values() if r.gate)/max(1,len(std.representations)); tdoc_ok=bool(self.tdoc.health.get('ok',True))
        health=self.health_manager.evaluate(registry_ok=bool(registry_status['ok']),dependency_ok=bool(self.dependency_status.get('all_ok')),tdoc_ok=tdoc_ok,gate_fraction=gate_fraction); self.tdoc.flush_health()
        margin=coc.get('consistency_margin'); gate_fault=any(not r.gate for r in std.representations.values())
        fast_record={'kind':'Layer3FastRecord','fast_index':self.fast_index,'reference_time_s':float(reference_time_s),'std':std.to_dict(),'packets':jsonable(packets),'registry':registry_status,
            'weights':weights,'weight_details':wdetail,'couplings':K,'coupling_details':kdetail,'coupling_effects':effects,'topology':graph,
            'center_of_consistency':coc.get('center') or {},'mismatch':mm,'network_residual_raw_ned_m_s':coc.get('network_residual_raw_ned_m_s'),
            'consistency_margin':margin,'consistency_region':coc.get('consistency_region') or {},'edge_residuals':coc.get('edge_residuals') or {},
            'edge_normalized_residuals':coc.get('edge_normalized_residuals') or {},'constraint_residuals':coc.get('constraint_residuals') or {},
            'observation_residuals':coc.get('observation_residuals') or {},'coc_observation_rank':coc.get('observation_rank'),
            'coc_structural_rank':coc.get('structural_rank'),'coc_missing_expected_observations':coc.get('missing_expected_observations') or [],
            'uncertainty_td':coc.get('uncertainty_td') or {},'coc_status':coc.get('status'),
            'stability':stability,'emergence':emergence,'hidden_loop':hidden,'feedback_route':feedback,'decision_route':decision,'prediction':dict(prediction or {}),
            'action_candidate_only':dict(action or {}),'previous_meso_guidance':copy.deepcopy(self.current_meso_guidance),'current_slow_policy':copy.deepcopy(self.current_slow_policy),
            'gate_fault':gate_fault,'health':health,'time_scheduler':self.time_scheduler.snapshot(),'coupling_execution_order':rt.get('execution_order') or [],
            'provenance':{'layer':'3','sublayers':['3.1','3.2','3.3-fast'],'formula_source':'TDDT_Dynamic_NTwin_Layer3_details_FA_v3.md','layer3_actuator_command':False,
                          'two_phase_coupling':True,'plan_precedes_destination_execution':bool(network_plan is not None)}}
        self.ss_store.append(fast_record); self.fast_buffer.append(fast_record); self.provenance.record('fast',{'fast_index':self.fast_index,'policy_version':self.current_slow_policy.get('version'),'health_mode':health['mode']})
        if health['mode']=='HOLD_LAST_SAFE':
            held=self._hold_last_safe(reference_time_s,fast_record,health)
            if held:return held
        meso_event=slow_event=None; M,L=self.time_scheduler.meso_fast_steps,self.time_scheduler.slow_meso_windows
        if len(self.fast_buffer)>=M:
            clock_meso=self.time_scheduler.meso_boundary(); self.meso_index=int(clock_meso['meso_index']); block,self.fast_buffer=self.fast_buffer[:M],self.fast_buffer[M:]
            aggregate=self.fast_to_meso.aggregate(block,self.meso_index,self.config.time.fast_dt_s); crp_match=self.crp.match(aggregate.get('crp_context') or {})
            sarg_ref=(self.sarg.select(regime_id,stage,int(crp_match['crp_id']),reference_admissible=bool(crp_match.get('matched'))) if crp_match.get('selected') else {'selected':False,'reason':'crp_not_selected'})
            sarg_dev=self.sarg.deviation(aggregate,sarg_ref); candidate=self.topology.generate_candidate(aggregate.get('violation_persistence') or {},graph)
            guidance=self.mrg_manager.build(self.meso_index,aggregate,crp_match,sarg_ref,sarg_dev,candidate); self.current_meso_guidance=guidance
            meso_event={'kind':'Layer3MesoEvent','meso_index':self.meso_index,'aggregate':aggregate,'crp_match':crp_match,'sarg_reference':sarg_ref,'sarg_deviation':sarg_dev,'guidance':guidance,'hidden_loop':aggregate.get('hidden_loop') or hidden,'clock':clock_meso,'provenance':{'loop':'Fast->Meso->CRP->SARG->MRG->Fast','topology_change':'candidate_only'}}
            self.meso_buffer.append(meso_event)
            if len(self.meso_buffer)>=L:
                clock_slow=self.time_scheduler.slow_boundary(); self.slow_index=int(clock_slow['slow_index']); mb,self.meso_buffer=self.meso_buffer[:L],self.meso_buffer[L:]
                sk=self.meso_to_slow.aggregate(mb,self.slow_index); self.last_slow_knowledge=sk; newp=self.slow_manager.update(self.current_slow_policy,sk,self.slow_index); self.current_slow_policy=newp
                self.version_manager.commit(newp,reason='slow_supervisory_sample_and_hold',safe=True)
                slow_event={'kind':'Layer3SlowEvent','slow_index':self.slow_index,'knowledge':sk,'supervisory_policy':copy.deepcopy(newp),'clock':clock_slow,
                            'layer7_candidate':{'topology_candidates':[(e.get('guidance') or {}).get('candidate_topology',{}) for e in mb],'requires_safe_commit':True},
                            'provenance':{'loop':'Fast->Meso->Slow->Fast','sample_and_hold':True,'permanent_adaptation_route':'Layer7->validation->Commit_safe'}}
                self.setd_store.append(slow_event); self.at_synchronization_barrier=True
            self.at_synchronization_barrier=True
        snapshot=NetworkSnapshot(policy_version=str(self.current_slow_policy.get('version','unknown')),reference_time_s=float(reference_time_s),std=std.to_dict(),topology=graph,weights=weights,couplings=K,
            center_of_consistency=coc.get('center') or {},mismatch=mm,consistency_margin=margin,consistency_region=dict(coc.get('consistency_region') or {}),stability=stability,emergence=emergence,hidden_loop=hidden,
            guidance=copy.deepcopy(self.current_meso_guidance),slow_policy=copy.deepcopy(self.current_slow_policy),uncertainty_td=dict(coc.get('uncertainty_td') or {}),constraints=dict(std.constraints),context=dict(std.context),
            registry=self.registry.snapshot(),coupling_effects=effects,tdoc_health=copy.deepcopy(self.tdoc.health),feedback_route=feedback,decision_route=decision,dependency_status=copy.deepcopy(self.dependency_status),
            version_state=self.version_manager.snapshot(),health=health,provenance={'snapshot_kind':'atomic_layer3_network_snapshot','fast_index':self.fast_index,'meso_index':self.meso_index,'slow_index':self.slow_index,
                'policy_version':self.current_slow_policy.get('version'),'time_scheduler':self.time_scheduler.snapshot(),'actuator_command':False,'network_plan_applied':True})
        if health['mode']=='NORMAL':self.health_manager.last_safe_snapshot=snapshot.to_dict()
        if bool(self.config.runtime.get('persist_every_snapshot',True)):self.snapshot_store.write(self.fast_index,snapshot.to_dict())
        return StepResult(snapshot=snapshot,fast_record=fast_record,meso_event=meso_event,slow_event=slow_event)

    def commit_layer7(self,adaptation:Mapping[str,Any],*,approved:bool=True,authorization:str|None=None)->dict[str,Any]:
        changes=dict(adaptation.get('topology_changes') or {}); allowed,reason=self.anti_chattering.allow(changes)
        if not allowed:return {'committed':False,'reason':reason,'policy':copy.deepcopy(self.current_slow_policy)}
        result=self.safe_commit_manager.commit(self.current_slow_policy,adaptation,approved=approved,at_barrier=self.at_synchronization_barrier,authorization=authorization)
        if result.get('committed'):
            self.topology.commit(result.get('topology_changes') or {}); self.current_slow_policy=result['policy']; self.version_manager.commit(self.current_slow_policy,reason='Layer7 Commit_safe',safe=True)
            self.provenance.record('safe_commit',{'version':self.current_slow_policy.get('version'),'authorization':bool(authorization)}); self.at_synchronization_barrier=False
        return result
    def rollback(self,*,reason:str='operator_or_health_request')->dict[str,Any]:
        state=self.version_manager.rollback(reason=reason); self.current_slow_policy=copy.deepcopy(self.version_manager.active); self.provenance.record('rollback',{'reason':reason,**state}); return {'rolled_back':True,**state,'policy':copy.deepcopy(self.current_slow_policy)}
