"""Research history is causal; empirical data cannot grant a future certificate."""
from copy import deepcopy
import pytest
from layer4.native_history_model import NativeHistoryEnvelopeModel
from layer4.native_provider_contract import DOMAIN


def inputs():
    history=[{'plant_time_s':t,'plant_state':[0.,0.,0.,.01,0.,0.,1.,0.,0.,0.,0.,0.,0.],
              'pwm':[1500.,1500.,1000.,1000.]} for t in (1.,1.05,1.1)]
    backend={'armed':False,'initial_mode':'QHOVER'}
    plan=[{'domain':DOMAIN,'mode':'QHOVER','discrete':[],'u_cmd':[.1,-.1,0.,.1]} for _ in range(3)]
    return history,backend,plan


def test_research_model_retains_unknown_state_risk_and_has_no_execution_authority():
    h,b,p=inputs();model=NativeHistoryEnvelopeModel(h,b)
    prediction=model.predict_reference(p,.2,.1)
    assert prediction['samples'][0]['plant_state'][0]==pytest.approx(.001)
    assert prediction['samples'][-1]['plant_state'][0]==pytest.approx(.006)
    assert prediction['uncertainty']['risk_upper_bound'] is None
    assert not prediction['future_rollouts_covered'] and not prediction['operational_prediction_qualified']
    with pytest.raises(ValueError,match='not qualified'):model.verify_qualification({}, {}, {})


@pytest.mark.parametrize('change',['short','reverse','duplicate','gap','nan','armed','mode','quaternion'])
def test_invalid_causal_history_or_state_scope_rejected(change):
    h,b,p=inputs()
    if change=='short':h=h[:2]
    if change=='reverse':h.reverse()
    if change=='duplicate':h[-1]['plant_time_s']=h[-2]['plant_time_s']
    if change=='gap':h[-1]['plant_time_s']=2.
    if change=='nan':h[-1]['plant_state'][3]=float('nan')
    if change=='armed':b['armed']=True
    if change=='mode':b['initial_mode']='FBWA'
    if change=='quaternion':h[-1]['plant_state'][6]=0.
    with pytest.raises(ValueError):NativeHistoryEnvelopeModel(h,b)


@pytest.mark.parametrize('change',['throttle','ARM','mode','horizon','axis'])
def test_research_domain_never_silently_expands(change):
    h,b,p=inputs();dt=.2
    if change=='throttle':p[0]['u_cmd'][2]=.01
    if change=='ARM':p[0]['discrete']=['ARM']
    if change=='mode':p[0]['mode']='QSTABILIZE'
    if change=='horizon':dt=1.
    if change=='axis':p[0]['u_cmd'][0]=.3
    model=NativeHistoryEnvelopeModel(h,b)
    with pytest.raises(ValueError):model.predict_reference(p,dt,.1)


def test_a_qualification_dictionary_does_not_turn_research_into_a_qualified_model():
    h,b,p=inputs();m=NativeHistoryEnvelopeModel(h,b,qualification={'qualified':True,'risk_upper_bound':0.})
    with pytest.raises(ValueError,match='future error/risk coverage'):m.verify_qualification({}, {}, {})
