import pytest
from tddt_tdoc.online_mission import LocalHoverAcceptance,bind_local_hover_reference
from tddt_tdoc.rtmo.phase_manager import MissionPhaseManager,PHASE_ORDER

INIT={'pn':100.,'pe':200.,'pd':-1800.}
def cycle(t,height=3.,l7='OK'):
    return {'future_layers':{'6':{'output':{'status':'EXECUTED','actuation_executed':True,
        'execution_evidence':{'segment_closure_proof':{'qualified':True,'snapshot':{
            'plant_time_s':t,'latitude_deg':40.,'longitude_deg':-105.,'altitude_msl_m':1800.+height,'armed':True}}}}},'7':{'status':l7}}}

def test_explicit_local_profile_preserves_canonical_phase_ids_and_normal_missions():
    m=MissionPhaseManager.local_hover()
    assert m.phase_order==('on_ground','vertical_takeoff','hover','vertical_landing','touchdown')
    for _ in range(3):m.advance(sim_time_s=1.)
    assert m.token(cycle=1,sim_time_s=1.)['phase_id']==10
    assert MissionPhaseManager().phase_order==PHASE_ORDER

def test_ground_time_missing_feedback_and_uncovered_intervals_do_not_count_as_hover():
    a=LocalHoverAcceptance([-105.,40.],INIT,duration_s=1.,height_m=3.,max_gap_s=.1)
    assert not a.update('hover',cycle(1.,height=0.))['phase_ready']
    assert not a.update('hover',cycle(2.,l7='FAILED'))['phase_ready']
    assert not a.update('hover',cycle(3.))['phase_ready']
    assert a.update('hover',cycle(4.))['consecutive_hold_s']==0.
    for i in range(1,12):r=a.update('hover',cycle(4.+i*.1))
    assert r['phase_ready'] and a.completed

def test_mission_objective_never_replaces_navigation_state_or_fabricates_prior():
    r={'stage':'hover','context':{}}
    bind_local_hover_reference(r,INIT,duration_s=60.,height_m=3.)
    c=r['context'];assert c['phase_target_agl_m']==3.
    assert c['online_phase_reference_targets']['hover']==[200.,100.,1803.]
    assert 'rtmo_navigation_initial_state' not in c
    assert c['local_hover_test']['actuation_authority'] is False
    with pytest.raises(ValueError):bind_local_hover_reference(r,INIT,duration_s=0.,height_m=3.)


@pytest.mark.parametrize('flags', [
    ['--online-local-hover-seconds','1'],
    ['--online-episodes','1','--online-local-hover-seconds','0'],
    ['--online-episodes','1','--online-local-hover-seconds','61'],
    ['--online-episodes','1','--online-local-hover-seconds','1','--online-local-hover-height-m','nan'],
])
def test_invalid_local_hover_never_starts_a_native_workflow(flags, monkeypatch, capsys):
    from pathlib import Path
    from tddt_tdoc import cli
    monkeypatch.setattr(cli.subprocess,'run',lambda *a,**kw:pytest.fail('invalid mission started'))
    config=Path(__file__).parents[1]/'configs/tdoc.yaml'
    assert cli.main(['--config',str(config),'nav-online',*flags])==2
    assert 'REFUSED' in capsys.readouterr().out
