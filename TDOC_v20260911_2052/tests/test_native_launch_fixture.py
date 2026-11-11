"""R3.05 replaces the retired stand's tests with rejection/no-clamping contracts."""
import math
from pathlib import Path
from types import SimpleNamespace
import pytest
from tddt_tdoc.adapters.layer3 import Layer3Adapter
from tddt_tdoc.adapters.layer4 import Layer4Adapter
from tddt_tdoc.native_ground_policy import require_native_contacts

TDOC=Path(__file__).resolve().parents[1]


def cfg(values=None):
    return SimpleNamespace(get=lambda key,default=None:(values or {}).get(key,default))


def test_retired_fixture_cannot_be_enabled_in_external_configuration():
    with pytest.raises(ValueError,match='retired by R3.05'):
        require_native_contacts(cfg({'layers.layer6.online.ideal_launch_fixture':True}))


def test_legacy_fixture_request_rejects_before_layer3_initialization():
    a=Layer3Adapter.__new__(Layer3Adapter);a.config=cfg()
    a._ensure=lambda:pytest.fail('retired payload must reject before initializing replay')
    req={'context':{'online_state_ingress':{'ptlib_ingress':{'environment_contract':{'launch_fixture':{}}}}}}
    with pytest.raises(ValueError,match='retired launch fixture'):
        a.full_fidelity_replay({}, {}, req, None, .1)


def test_raw_wingmpet_ground_assertion_is_not_a_hidden_launch_stand():
    from tddt_tdoc.config import TDOCConfig
    from tddt_tdoc.adapters.layer2 import Layer2Adapter
    adapter=Layer2Adapter(TDOCConfig.from_yaml(TDOC/'configs/tdoc.yaml'))
    session=adapter.open_isolated_session();pitch=math.radians(89.)/2
    state=dict(pn=0.,pe=0.,pd=-.141025821708132,vn=0.,ve=0.,vd=0.,
        qw=math.cos(pitch),qx=0.,qy=math.sin(pitch),qz=0.,p_rate=0.,q_rate=0.,r_rate=0.,
        omega_left=0.,omega_right=0.,soc=1.)
    try:
        after,record=Layer3Adapter._wing27_physical_step(session['wing'],state,
            dict(left_motor_cmd=0.,right_motor_cmd=0.,left_elevon_rad=0.,right_elevon_rad=0.),
            dt=.1,wind_ned=[0.,0.,0.],air_density=1.2,ground_supported=True)
        assert after['pd']>state['pd'] and after['vd']>.9
        assert record['outputs']['next_pd']==after['pd']
        assert 'launch_fixture' not in record and 'unconstrained_model_outputs' not in record
        assert not record['ground_contact_dynamics_modeled']
        assert record['ground_support_scope']=='feasibility_assertion_only'
    finally:
        adapter.close_isolated_session(session);adapter.close()


def test_layer4_rejects_fixture_even_with_positive_legacy_claims():
    row={'source':'WingMPET+ideal-SITL-launch-fixture','outputs':{'vtol_feasible':1.},
         'substep_trace':[{'vtol_feasible':True}],'action_consumed':True,'vtol_feasible':True,
         'unconstrained_model_outputs':{'vtol_feasible':1.},
         'launch_fixture':{'contract':{'simulation_only':True},'trace':[{'engaged':True,'support_force_up_n':6.37}]}}
    result=Layer4Adapter._wingmpet_physical_verdict([{'physical_replay':row,'physical_mission_safe':True,'physical_action_consumed':True}])
    assert not result['all_rows_vtol_feasible']
    assert result['rows'][0]['retired_launch_fixture_present']


def test_native_certificate_identity_contains_retirement_policy(monkeypatch):
    monkeypatch.setattr("tddt_tdoc.model_compatibility.capture_model_dependencies",lambda config:{"model_id":"test-fourfoot"})
    a=Layer4Adapter.__new__(Layer4Adapter)
    a.config=cfg({'layers.layer6.online.transport':'Layer6Client_GUI_MAVLink'})
    a.engine=SimpleNamespace(model_hash='base-model')
    assert a._verification_model_identity({'learning_mode':'ONLINE_JSBSIM'})!='base-model'
    assert a._verification_model_identity({'learning_mode':'OFFLINE_SE'})!='base-model'
    with pytest.raises(ValueError,match='retired launch fixture'):
        a._verification_model_identity({'learning_mode':'ONLINE_JSBSIM','launch_fixture':{}})
