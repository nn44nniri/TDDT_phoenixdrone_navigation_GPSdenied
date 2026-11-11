import math
import pytest
from tddt_layer7.config import Layer7Config
from tddt_layer7.evidence import EvidenceGateway
from tddt_layer7.residuals import ResidualEngine
from tddt_layer7.tdoc_adapter import Layer7SideModule


def payload():
    sem={'a':{'quantity':'value','unit':'1','frame':'scalar'}}
    return {'transaction_id':'t','v':'1',
      'ExecutionEvidence':{'y_real':{'a':2.0},'u_cmd':{'u':1.0},'u_applied':{'u':0.5},'x_obs':{'x':3.0},
        'timestamp':1.0,'planned_timestamp':0.8,'version':'1','evidence_type':'field',
        'observation_id':'o','input_observation_id':'o','decision_id':'d','correlates_to_decision_id':'d',
        'command_id':'c','correlates_to_actuator_command_id':'c','output_semantics':sem},
      'PredictionEvidence':{'y_hat_td':{'a':1.0},'x_hat_td':{'x':1.0},'sigma_pred':{'a':1.0},
        'timestamp':1.0,'version':'1','output_semantics':sem},
      'NetworkSnapshot':{'coc_cost':0.2,'timestamp':1.0,'version':'1','twin_validity':{}},
      'AdaptationHints':{'model_error':{'m':0.4},'alignment_error':{'a':0.3}}}


def test_evidence_version_gate():
    inp=Layer7SideModule.input_from_dict(payload()); assert EvidenceGateway(Layer7Config()).check(inp).ok
    inp.prediction.version='2'; assert not EvidenceGateway(Layer7Config()).check(inp).ok


def test_invalid_evidence_type_is_not_promoted_to_field():
    p=payload(); p['ExecutionEvidence']['evidence_type']='unknown'
    with pytest.raises(ValueError): Layer7SideModule.input_from_dict(p)


def test_correlation_chain_mismatch_rejected():
    p=payload(); p['ExecutionEvidence']['input_observation_id']='other'
    inp=Layer7SideModule.input_from_dict(p); g=EvidenceGateway(Layer7Config()).check(inp)
    assert not g.correlation_ok and not g.ok


def test_semantic_mismatch_rejected():
    p=payload(); p['PredictionEvidence']['output_semantics']={'a':{'quantity':'value','unit':'m','frame':'scalar'}}
    inp=Layer7SideModule.input_from_dict(p); g=EvidenceGateway(Layer7Config()).check(inp)
    assert not g.semantic_ok and not g.ok


def test_residual_decomposition():
    r=ResidualEngine().compute(Layer7SideModule.input_from_dict(payload()))
    assert r.prediction==1.0 and r.state==2.0 and r.execution==0.5
    assert r.model==0.4 and r.alignment==0.3 and r.domain>2.3
    assert set(r.vector)=={'obs','model','state','align','cpl','sync','pred','dec','exec'}


def test_raw_pairs_compute_missing_residuals():
    p=payload(); p['AdaptationHints']={'observation_measurements':{'s':3},'observation_reference':{'s':1},
        'coupling_observed':{'A->B':4},'coupling_predicted':{'A->B':1},
        'synchronization_source_times':{'A':2.0},'synchronization_canonical_time':1.0}
    r=ResidualEngine().compute(Layer7SideModule.input_from_dict(p))
    assert r.observation==2 and r.coupling==3 and r.synchronization==1
    assert r.residual_sources['obs']=='computed'


def test_latency_residual():
    r=ResidualEngine().compute(Layer7SideModule.input_from_dict(payload())); assert abs(r.latency-0.2)<1e-9


def test_state_attitude_residual_uses_shortest_wrapped_angle():
    p=payload()
    p['ExecutionEvidence']['x_obs']={'yaw_rad':math.pi-0.01,'q_rate_rad_s':4.0}
    p['PredictionEvidence']['x_hat_td']={'yaw_rad':-math.pi+0.01,'q_rate_rad_s':1.0}
    r=ResidualEngine().compute(Layer7SideModule.input_from_dict(p))
    assert abs(r.state_residual['yaw_rad']+0.02)<1e-12
    assert r.state_residual['q_rate_rad_s']==3.0
    assert abs(r.state-math.sqrt(0.02**2+3.0**2))<1e-12
