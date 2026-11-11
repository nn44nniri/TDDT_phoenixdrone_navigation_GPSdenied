from types import SimpleNamespace
import copy
import pytest
from tddt_tdoc.adapters.layer5 import Layer5Adapter
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter


def state():
    return dict(mavlink_connected=True,mode='QHOVER',armed=False,throttle=0.,
        landed_state='ON_GROUND',telemetry_age_s={'SYS_STATUS':.01,'EKF_STATUS_REPORT':.01},
        ekf_status={'flags':55},fcu_health={'onboard_control_sensors_present':268435456,
        'onboard_control_sensors_health':268435456})


def layer5():
    a=Layer5Adapter.__new__(Layer5Adapter)
    a.config=SimpleNamespace(get=lambda key,default=None:'Layer6Client_GUI_MAVLink')
    a.ensure_native_control_binding=lambda:{'ok':True}
    a.online_control_readiness=lambda:{'ok':True}
    return a


@pytest.mark.parametrize('change',[{'landed_state':'IN_AIR'},{'throttle':.01},
    {'mode':'MANUAL'},{'ekf_status':{'flags':0}}])
def test_ground_arm_rejects_unsafe_conditions(change):
    s=state();s.update(change)
    d=layer5().native_ground_arm_decision(s,transport_ready=True)
    assert not d['ok'] and not d['gates']['FINAL']


def test_ground_arm_prior_and_readiness_are_mandatory():
    a=layer5()
    assert not a.native_ground_arm_decision(state(),transport_ready=False)['ok']
    a.ensure_native_control_binding=lambda:{'ok':False}
    assert not a.native_ground_arm_decision(state(),transport_ready=True)['ok']


def test_healthy_ground_status_and_prior_are_not_arm_authority():
    result=layer5().native_ground_arm_decision(state(),transport_ready=True)
    assert result['ground_conditions_met'] is True
    assert not result['ok'] and result['gates']['FINAL'] is False
    assert result['reason']=='ground_arm_requires_current_verified_D_exec'
    assert result['D_exec'] is result['layer6_package'] is result['u_cmd'] is None
    assert result['discrete']==[]


def test_retired_bootstrap_emits_no_preparation_or_arm_commands():
    a=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter);a.layer5=layer5()
    a._session_open=True;a._session_generation=1;a._option=lambda key,default=None:.1
    a._fresh=lambda s:True;a._environment_ready=lambda s:True
    calls=[];s=state();s['mode']='MANUAL'
    def submit(command,**kw):
        calls.append((command,kw));s['mode']='QHOVER'
        if kw['discrete']==['ARM']:s['armed']=True
        return SimpleNamespace(ok=True,error=None)
    a._get_client=lambda:SimpleNamespace(gui_status=lambda **kw:copy.deepcopy(s),submit_command=submit)
    with pytest.raises(RuntimeError,match='requires_current_verified_D_exec'):
        a._prepare_ground_arm()
    assert calls==[]


def test_ack_without_armed_telemetry_cannot_pass():
    a=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter);a.layer5=layer5()
    a._session_open=True;a._session_generation=1;a._option=lambda key,default=None:.01
    a._fresh=lambda s:True;a._environment_ready=lambda s:True
    a._get_client=lambda:SimpleNamespace(gui_status=lambda **kw:state(),
        submit_command=lambda *args,**kw:SimpleNamespace(ok=True,error=None))
    with pytest.raises(RuntimeError,match='requires_current_verified_D_exec'):
        a._prepare_ground_arm()
