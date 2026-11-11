"""Regressions for the observed native startup/ARM blocking conditions."""
from types import SimpleNamespace

import pytest

from tddt_tdoc.adapters.layer2 import Layer2Adapter
from tddt_tdoc.native_control_binding import ground_contact
from tddt_tdoc.online_mission import bind_pending_navigation_initialization
from tddt_tdoc.rtmo.capability_gate import online_on_ground_boundary_evidence


def test_failed_first_ingress_does_not_lose_mission_known_mesa_seed():
    adapter=Layer2Adapter.__new__(Layer2Adapter)
    initial={'pn':4430453.,'pe':474390.,'pd':-1798.75,'vn':0.,'ve':0.,'vd':0.,
             'qw':.7071067811865476,'qx':0.,'qy':.7071067811865476,'qz':0.}
    for _ in range(3):
        context={'online_episode_id':'episode','rtmo_data_authority':{'navigation':'MESA_STD'}}
        assert bind_pending_navigation_initialization(context,adapter,'episode',initial)
        # Failed ingress returns before calling Layer2; the next cycle must keep the seed.
    sets=[];mesa=SimpleNamespace(fmu=SimpleNamespace(set=lambda **kw:sets.append(kw)))
    aligned=adapter._align_mesa_simulation_state(mesa,{'learning_mode':'ONLINE_JSBSIM',
        'context':context,'stage':'vertical_takeoff'},{},source='test')
    assert aligned['applied'] and sets[0]['rN']==initial['pn']
    assert sets[0]['rD']==initial['pd'] and sets[0]['qy']==initial['qy']
    assert not bind_pending_navigation_initialization(context,adapter,'episode',initial)
    assert 'rtmo_navigation_initial_state' not in context
    # Even a repeated stale initialization request cannot re-anchor this episode.
    context.update(rtmo_navigation_initial_state=initial,rtmo_allow_initial_navigation_alignment=True)
    assert not adapter._align_mesa_simulation_state(mesa,{'learning_mode':'ONLINE_JSBSIM',
        'context':context},{},source='test')['applied']
    assert len(sets)==1
    assert bind_pending_navigation_initialization({},adapter,'next-episode',initial)


def manual_ground_status():
    return {'mavlink_connected':True,'mode':'MANUAL','armed':False,'landed_state':None,
            'groundspeed_m_s':.2194649577,'relative_altitude_m':.097,'vertical_speed_m_s':-.16847646,
            'velocity_n_m_s':.1,'velocity_e_m_s':.1,'velocity_d_m_s':-.168,
            'telemetry_age_s':{k:.01 for k in ('HEARTBEAT','ATTITUDE','VFR_HUD','GLOBAL_POSITION_INT')}}


def test_manual_mode_without_landed_state_has_demo_aligned_ground_evidence():
    status=manual_ground_status()
    assert ground_contact(status)
    evidence=online_on_ground_boundary_evidence({'ok':True,'ready':True,
        'transport':'Layer6Client_GUI_MAVLink','current_gui_state':status})
    assert evidence['supported'] and evidence['checks']['on_ground']


@pytest.mark.parametrize('change',[{'landed_state':'IN_AIR'},{'landed_state':'TAKEOFF'},
    {'landed_state':'LANDING'},{'relative_altitude_m':2.},{'groundspeed_m_s':2.},
    {'vertical_speed_m_s':2.},{'vertical_speed_m_s':None},{'groundspeed_m_s':float('nan')},
    {'mavlink_connected':False}])
def test_ground_fallback_does_not_authorize_airborne_or_incomplete_status(change):
    status=manual_ground_status();status.update(change)
    assert not ground_contact(status)


def test_ground_boundary_still_requires_fresh_telemetry():
    status=manual_ground_status();status['telemetry_age_s']['GLOBAL_POSITION_INT']=5.
    evidence=online_on_ground_boundary_evidence({'ok':True,'ready':True,
        'transport':'Layer6Client_GUI_MAVLink','current_gui_state':status})
    assert not evidence['supported']


@pytest.mark.parametrize('final,airborne',[(True,False),(True,True),(False,False)])
def test_native_model_only_final_cannot_release_takeoff_even_on_ground(final,airborne):
    from tddt_tdoc.adapters.layer5 import Layer5Adapter
    a=Layer5Adapter.__new__(Layer5Adapter)
    a._ensure=lambda:None
    # This test isolates actuation-domain rejection after the independent model gate.
    a._certificate_model_gate=lambda cert:{'ok':True}
    a.root='external-layer5';a.kstore_root='unused';a.module=SimpleNamespace()
    a.config=SimpleNamespace(get=lambda key,default=None:'Layer6Client_GUI_MAVLink'
        if key=='layers.layer6.online.transport' else default)
    result={'gates':{'FINAL':final},'layer6_package':{'u_cmd':[0,0,.5,0]} if final else None}
    a.engine=SimpleNamespace(run=lambda *args,**kwargs:result)
    gui=manual_ground_status()
    if airborne:gui['landed_state']='IN_AIR'
    out=a.invoke({'learning_mode':'ONLINE_JSBSIM','layer4_result':{},'request':{
        'stage':'vertical_takeoff','transaction_id':'tx',
        'context':{'online_state_ingress':{'ptlib_ingress':{'gui_state':gui}}}}})['result']
    assert out['gates']['FINAL'] is False
    assert out['layer6_package'] is None
    if final:
        assert out['native_intent_rejection']=='native_pilot_controller_prediction_unavailable'
    assert not ((out.get('layer6_package') or {}).get('execution_intent') or {}).get('discrete')
