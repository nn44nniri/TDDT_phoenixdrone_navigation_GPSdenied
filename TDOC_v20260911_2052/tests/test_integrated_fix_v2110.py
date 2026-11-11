from __future__ import annotations
import inspect
from tddt_tdoc.adapters.layer3 import Layer3Adapter
from tddt_tdoc.vehicle_state import phoenix_state_from_ptlib_snapshot


def test_full_fidelity_contract_accepts_physical_bridge():
    sig=inspect.signature(Layer3Adapter.full_fidelity_replay)
    assert 'physical_actions' in sig.parameters
    assert 'bridge_record' in sig.parameters
    assert 'physical_action_provider' in sig.parameters
    traj=[[0.0]*20,[0.0]*20,[0.0]*20]
    acts=[{'left_motor_cmd':.4,'right_motor_cmd':.4,'left_elevon_rad':0.0,'right_elevon_rad':0.0} for _ in range(2)]
    out=Layer3Adapter._validate_full_fidelity_physical_actions(traj,acts,{'ok':True})
    assert out['ok'] and out['horizon']==2 and len(out['physical_actions'])==2


def test_full_fidelity_contract_fails_closed_on_short_or_bad_action():
    traj=[[0.0]*20,[0.0]*20,[0.0]*20]
    out=Layer3Adapter._validate_full_fidelity_physical_actions(traj,[{'left_motor_cmd':1.2,'right_motor_cmd':0.0,'left_elevon_rad':0.0,'right_elevon_rad':0.0}],{'ok':True})
    assert not out['ok']
    assert any('horizon_short' in x for x in out['reasons'])
    assert any('motor_out_of_range' in x for x in out['reasons'])


def test_surface_motion_does_not_count_as_propulsion():
    snap={'groups':{
      'ACTUATOR':{'age_s':.01,'payload':{'servo1_raw':1479,'servo2_raw':1479,'servo3_raw':1000,'servo4_raw':1000}},
      'AIRSPEED':{'age_s':.01,'payload':{'groundspeed':0.0,'airspeed':0.5}},
      'ATTITUDE':{'age_s':.01,'payload':{}},'POSITION':{'age_s':.01,'payload':{}},'VELOCITY':{'age_s':.01,'payload':{}}
    }}
    st=phoenix_state_from_ptlib_snapshot(snap,{})
    m=st['motion']
    assert m['surface_actuation_active'] is False
    assert m['raw_surface_output_active'] is True
    assert m['propulsion_actuation_active'] is False
    assert m['translation_active'] is False
    assert m['state']=='TRIM_ONLY_HELD'
    assert m['raw_surface_output_active'] is True
    assert m['authorized_actuation'] is False


def test_full_fidelity_stage_contract_keeps_passive_boundaries_fail_closed():
    from tddt_tdoc.adapters.layer4 import Layer4Adapter
    for stage in ("on_ground","touchdown"):
        c=Layer4Adapter._full_fidelity_stage_contract({"stage":stage})
        assert c["applicable"] is False
        assert c["sarg_stage"] is None
        assert c["passive_boundary_phase"] is True
        assert c["reason"] == "passive_boundary_phase_deferred"


def test_full_fidelity_stage_contract_maps_controlled_vtol_and_forward_phases_to_sarg_priors():
    from tddt_tdoc.adapters.layer4 import Layer4Adapter
    expected={
        "vertical_takeoff":"takeoff_transition",
        "hover":"takeoff_transition",
        "transition_forward":"takeoff_transition",
        "climb":"climb","cruise":"cruise","turn":"turn","approach":"approach",
        "transition_back":"approach","vertical_landing":"approach",
    }
    for stage,sarg in expected.items():
        c=Layer4Adapter._full_fidelity_stage_contract({"stage":stage})
        assert c["applicable"] is True
        assert c["sarg_stage"] == sarg


def test_authorized_layer6_execution_counts_as_actuation_without_translation():
    snap={'groups':{
      'ACTUATOR':{'age_s':.01,'payload':{'servo1_raw':1479,'servo2_raw':1479,'servo3_raw':1300,'servo4_raw':1300}},
      'AIRSPEED':{'age_s':.01,'payload':{'groundspeed':0.0,'airspeed':13.0}},
      'ATTITUDE':{'age_s':.01,'payload':{}},'POSITION':{'age_s':.01,'payload':{}},'VELOCITY':{'age_s':.01,'payload':{}}
    }}
    st=phoenix_state_from_ptlib_snapshot(snap,{}, {'status':'EXECUTED','actuation_executed':True})
    assert st['motion']['authorized_actuation'] is True
    assert st['motion']['propulsion_actuation_active'] is True
    assert st['motion']['state']=='ACTUATED_NO_TRANSLATION'
