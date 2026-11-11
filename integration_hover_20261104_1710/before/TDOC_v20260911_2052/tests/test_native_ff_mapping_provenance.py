"""Native model replay must not inherit retired held-FMI commissioning gates."""
from types import SimpleNamespace
from tddt_tdoc.adapters.layer5 import Layer5Adapter


def adapter(transport,prior=True):
    a=Layer5Adapter.__new__(Layer5Adapter)
    a.config=SimpleNamespace(get=lambda key,default=None:transport)
    a._certificate_model_gate=lambda cert:{'ok':True}  # This unit isolates mapping; R3.06 tests exercise the real gate.
    a._ensure=lambda:None;a.ensure_native_control_binding=lambda:{'ok':prior}
    a.engine=SimpleNamespace(phoenix=SimpleNamespace(runtime_certificate={},elevon_max=.53))
    physics={'physics_derived':True,'source_motor_law':True,'force_balance_valid':True,
             'takeoff_pitch_trim_derived':True,'takeoff_pitch_trim_valid':True,'takeoff_symmetric_elevon_trim_rad':0.}
    a._verification_model_certificate={'ok':True,'mixer':{'elevon_matrix_rad_per_norm':[[.1,.1],[-.1,.1]],'trim_rad':[0.,0.]},
        'phase_throttle_paths':{'vertical_takeoff':{'model_qualified':True,'request_bounds_norm':[.7,.9],
         'operating_request_norm':.8,'operating_motor_norm':.8,'gain_motor_norm_per_pilot_delta':1.,
         'operating_point_basis':'phoenix_source_motor_law_force_balance','certificate_schema':'Layer5-Phoenix-takeoff-command-map-v2',
         'bound_phase':'vertical_takeoff','surface_trim_basis':'phoenix_source_propwash_pitch_equilibrium',
         'surface_trim_rad':[0.,0.],'physics_basis':physics}}}
    return a


def mix(a,throttle=.8):
    return a._wingmpet_verification_mix([0.,0.,throttle,0.],phase='vertical_takeoff',
        operating_context={'attitude_rate_state':{'source':'PT_LIB/FMI current simulation state','simulation_only':True}})


def test_native_model_replay_does_not_request_retired_execution_certificate():
    result=mix(adapter('Layer6Client_GUI_MAVLink'))
    assert result['ok'] and result['physical']==[0.,0.,.8,.8]
    assert result['scope']=='L4_WINGMPET_FULL_FIDELITY_ONLY'
    assert result['direct_pwm'] is False and result['not_field_evidence']
    assert 'native execution checked separately' in result['execution_mapping']


def test_legacy_backend_keeps_commissioning_requirement():
    result=mix(adapter('legacy'))
    assert not result['ok'] and 'online_execution_mapping_missing' in result['reason']


def test_native_requires_accepted_prior_and_retains_model_limits():
    assert not mix(adapter('Layer6Client_GUI_MAVLink',False))['ok']
    result=mix(adapter('Layer6Client_GUI_MAVLink'),1.)
    assert not result['ok'] and 'outside_qualified_neighborhood' in result['reason']


def test_full_fidelity_rejection_keeps_nested_physical_reason():
    from tddt_tdoc.adapters.layer3 import FullFidelityPolicyError
    binding={'tracker':{'error':"ValueError('vertical_trajectory_collective_outside_physical_bounds')"}}
    result={'ok':False,'reason':'vertical_takeoff_trajectory_collective_unavailable','bridge_record':{'binding':binding}}
    replay={'physical_state':{'vd':6.128717775990518},'replay_step_s':1.,'simulation_only':True}
    error=FullFidelityPolicyError(result['reason'],step_index=1,provider_result=result,replay_evidence=replay)
    assert isinstance(error,RuntimeError)
    assert error.policy_failure['provider_result']['bridge_record']['binding']==binding
    assert error.policy_failure['step_index']==1 and error.policy_failure['actuation_authority'] is False
    assert error.policy_failure['replay_evidence']==replay
    assert str(error)=='full_fidelity_action_policy_failed:vertical_takeoff_trajectory_collective_unavailable'
