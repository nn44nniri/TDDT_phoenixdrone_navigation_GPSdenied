import pytest
from tddt_layer7.config import Layer7Config
from tddt_layer7.cli import demo_payload
from tddt_layer7.tdoc_adapter import Layer7SideModule
from tddt_layer7.adaptation import AdaptationEngine
from tddt_layer7.models import *


def test_architecture_invariants_cannot_be_disabled():
    with pytest.raises(ValueError): Layer7Config(require_field_for_persistent=False)
    with pytest.raises(ValueError): Layer7Config(fail_closed_structural=False)
    with pytest.raises(ValueError): Layer7Config(tdoc_managed=False)
    with pytest.raises(ValueError): Layer7Config(direct_actuation_allowed=True)


def test_rate_hierarchy_is_enforced():
    from tddt_layer7.config import RateConfig
    with pytest.raises(ValueError): Layer7Config(rates=RateConfig(fast_state=.1,meso_relation=.2,slow_structural=.01))


def test_gp_correction_populates_prediction_uncertainty_feedback(tmp_path):
    out=Layer7SideModule(state_root=tmp_path/'s').process(demo_payload())
    assert out['route']['layer2']
    assert out['feedback_layer2']['CorrectedPrediction']
    assert out['feedback_layer2']['CorrectedUncertainty']
    assert any(k.startswith('prediction:') for k in out['feedback_layer2']['DeltaU'])


def test_feedback_contract_has_distinct_deltaR_and_TEP(tmp_path):
    out=Layer7SideModule(state_root=tmp_path/'s').process(demo_payload())
    f=out['feedback_layer2']
    for k in ['DeltaX','DeltaB','DeltaTheta','DeltaU','DeltaQ','DeltaV','DeltaR','TEP_candidate','Reason','rho_attr','nu_new']:
        assert k in f
    assert f['DeltaR'] is not f['TEP_candidate']


def test_candidate_specific_validation_rejects_wrong_hash(tmp_path):
    def val(req):
        return {'base_version':req['base_version'],'candidate_hash':'wrong','stability_ok':True,
                'active_metrics':{'error':2},'candidate_metrics':{'error':1},'violation_count':0,
                'coverage_ok':True,'margin':1,'sample_count':2}
    m=Layer7SideModule(tdoc_validation_callback=val,state_root=tmp_path/'s')
    # Need six samples to reach slow trigger.
    last=None
    for i in range(6):
        p=demo_payload(); p['transaction_id']=str(i)
        for sec in ['ExecutionEvidence','PredictionEvidence','NetworkSnapshot']: p[sec]['timestamp']=100+i
        p['NetworkSnapshot']['coc_cost']=2.0
        p['AdaptationHints'].update({'structural_error':2,'maturity':1,'authorized':True,'identity_ok':True,'policy_ok':True,'trace_ok':True,
            'coupling_error':{'MESA->WingMPET':2},'coupling_sensitivity':{'MESA->WingMPET':1},'coupling_variance':{'MESA->WingMPET':.2},
            'coupling_bounds':{'MESA->WingMPET':[0,1]}})
        last=m.process(p)
    assert last['trigger']['slow'] and not last['validation']['stability_ok'] and not last['validation']['shadow_ok']


def test_tep_field_update_is_candidate_until_commit():
    cfg=Layer7Config(); eng=AdaptationEngine(cfg)
    ex=ExecutionEvidence({'x':1},evidence_type=EvidenceType.FIELD)
    pr=PredictionEvidence({'x':1})
    nw=NetworkSnapshot(weights={'T':1},twin_validity={'T':1},twin_quality={'T':1},twin_freshness={'T':1},twin_reliability={'T':.5})
    h=AdaptationHints(tep_success={'T':True},tep_cell={'T':'c'},crp_id='c',sarg_id='s',mrg={'g':1},weight_bounds={'T':[0,1]})
    inp=TDOCInput(ex,pr,nw,hints=h)
    before=eng.reliability.cell('T','c').mean
    cand=eng.build(inp,ResidualBundle(domain=2),RouteDecision(layer2=True),TriggerDecision(slow=True),AttributionResult({'Domain':1},1,'Domain'))
    assert cand.tep_candidate and eng.reliability.cell('T','c').mean==before


def test_unknown_adaptation_hint_is_rejected():
    p=demo_payload(); p['AdaptationHints']['not_a_contract_field']=1
    with pytest.raises(ValueError): Layer7SideModule.input_from_dict(p)
