from tddt_layer7.config import Layer7Config
from tddt_layer7.adaptation import AdaptationEngine
from tddt_layer7.validation import ValidationEngine
from tddt_layer7.models import *
from tddt_layer7.utils import dataclass_hash


def base():
    sem={'x':{'quantity':'x','unit':'m','frame':'ENU'}}
    ex=ExecutionEvidence({'x':2},x_obs={'x':2},evidence_type=EvidenceType.FIELD,version='1',output_semantics=sem,
        observation_id='o',input_observation_id='o',decision_id='d',correlates_to_decision_id='d',command_id='c',correlates_to_actuator_command_id='c')
    pr=PredictionEvidence({'x':1},x_hat_td={'x':1},version='1',output_semantics=sem)
    nw=NetworkSnapshot(std={'x':1},weights={'T':1.0},twin_validity={'T':1.0},couplings={'T->U':0.5},topology=[('T','U')],version='1')
    h=AdaptationHints(state_gain={'x':0.5},state_bounds={'x':[-10,10]},tep_centroids={'T':[[0.0]]},tep_descriptor={'T':[0.0]},tep_max_distance={'T':1.0},
                      crp_id='c',sarg_id='s',mrg={'x':1})
    return TDOCInput(ex,pr,nw,hints=h)


def test_fast_state_correction():
    inp=base(); r=ResidualBundle(state_residual={'x':1},state=1); rd=RouteDecision(layer2=True); tr=TriggerDecision(fast=True)
    c=AdaptationEngine(Layer7Config()).build(inp,r,rd,tr,AttributionResult({'Domain':1},1,'Domain'))
    assert c.delta_x['x']==0.5 and not c.persistent


def test_non_field_blocks_persistent():
    inp=base(); inp.execution.evidence_type=EvidenceType.SIMULATION
    c=AdaptationEngine(Layer7Config()).build(inp,ResidualBundle(domain=2),RouteDecision(layer2=True),TriggerDecision(slow=True),AttributionResult({'Domain':1},1,'Domain'))
    assert not c.persistent and 'persistent_blocked_non_field' in c.reasons


def test_invalid_twin_forces_weight_zero():
    inp=base(); inp.network.twin_validity['T']=0.0; inp.hints.weight_bounds={'T':[0,1]}
    c=AdaptationEngine(Layer7Config()).build(inp,ResidualBundle(domain=2),RouteDecision(layer2=True),TriggerDecision(slow=True),AttributionResult({'Domain':1},1,'Domain'))
    assert c.delta_w['T']==-1.0


def test_omega_only_updates_on_layer32_route():
    inp=base(); inp.hints.omega_delta={'o':1}; inp.hints.omega_bounds={'o':[0,2]}
    c=AdaptationEngine(Layer7Config()).build(inp,ResidualBundle(domain=2),RouteDecision(layer2=True),TriggerDecision(slow=True),AttributionResult({'Domain':1},1,'Domain'))
    assert c.delta_omega=={}


def test_structural_validation_fails_without_candidate_specific_shadow_and_stability():
    inp=base(); inp.hints.coupling_bounds={'T->U':[0,1]}
    c=CandidateUpdate(delta_k={'T->U':0.1},structural=True,persistent=True)
    v=ValidationEngine(Layer7Config()).validate(inp,c,RouteDecision(layer32=True),TriggerDecision(slow=True,ess=4),True,True)
    assert not v.stability_ok and not v.shadow_ok and not v.commit_gate


def test_structural_validation_passes_with_candidate_bound_external_evidence():
    inp=base(); inp.hints.coupling_bounds={'T->U':[0,1]}; inp.hints.authorized=inp.hints.identity_ok=inp.hints.policy_ok=inp.hints.trace_ok=True
    c=CandidateUpdate(delta_k={'T->U':0.1},structural=True,persistent=True); h=dataclass_hash(c)
    ext={'base_version':'1','candidate_hash':h,'stability_ok':True,'active_metrics':{'error':2},'candidate_metrics':{'error':1},'violation_count':0,'coverage_ok':True,'margin':0.5,'sample_count':4}
    v=ValidationEngine(Layer7Config()).validate(inp,c,RouteDecision(layer32=True),TriggerDecision(slow=True,ess=4),True,True,ext)
    assert v.commit_gate
