from pathlib import Path
from types import SimpleNamespace

from tddt_tdoc.adapters.layer5 import Layer5Adapter


def _adapter(cert):
    a=Layer5Adapter.__new__(Layer5Adapter); a.config=SimpleNamespace(get=lambda key, default=None: 'OFFLINE_SE' if key == 'layers.layer6.online.transport' else default)
    a._ensure=lambda: None
    a.engine=SimpleNamespace(phoenix=SimpleNamespace(elevon_max=0.53))
    a._verification_model_certificate=cert
    return a


def test_takeoff_bridge_uses_same_physics_derived_surface_trim():
    seed=0.82; trim=0.0
    pb={'physics_derived':True,'source_motor_law':True,'force_balance_valid':True,
        'takeoff_pitch_trim_derived':True,'takeoff_pitch_trim_valid':True,
        'takeoff_symmetric_elevon_trim_rad':trim}
    cert={'ok':True,'mixer':{'elevon_matrix_rad_per_norm':[[0.02,0.02],[-0.02,0.02]],'trim_rad':[-0.08,-0.08]},
          'trim':{'throttle_request_norm':0.30},'throttle_path':{},'phase_throttle_paths':{
          'vertical_takeoff':{'model_qualified':True,'request_bounds_norm':[seed-.04,seed+.04],
          'operating_request_norm':seed,'operating_motor_norm':seed,'gain_motor_norm_per_pilot_delta':1.0,
          'operating_point_basis':'phoenix_source_motor_law_force_balance','certificate_schema':'Layer5-Phoenix-takeoff-command-map-v2',
          'bound_phase':'vertical_takeoff','surface_trim_basis':'phoenix_source_propwash_pitch_equilibrium',
          'surface_trim_rad':[trim,trim],'physics_basis':pb}}}
    out=_adapter(cert)._wingmpet_verification_mix([0.0,0.0,seed,0.0],phase='vertical_takeoff')
    assert out['ok'] is True
    assert out['phase_surface_trim_rad']==[0.0,0.0]
    assert out['physical'][:2]==[0.0,0.0]
    assert out['phase_throttle_qualification']=='vertical_takeoff_command_mapping_only'


def test_takeoff_bridge_rejects_forward_trim_mismatch_against_physics_basis():
    seed=0.82
    pb={'physics_derived':True,'source_motor_law':True,'force_balance_valid':True,
        'takeoff_pitch_trim_derived':True,'takeoff_pitch_trim_valid':True,
        'takeoff_symmetric_elevon_trim_rad':0.0}
    cert={'ok':True,'mixer':{'elevon_matrix_rad_per_norm':[[0.02,0.02],[-0.02,0.02]],'trim_rad':[-0.08,-0.08]},
          'trim':{'throttle_request_norm':0.30},'throttle_path':{},'phase_throttle_paths':{
          'vertical_takeoff':{'model_qualified':True,'request_bounds_norm':[seed-.04,seed+.04],
          'operating_request_norm':seed,'operating_motor_norm':seed,'gain_motor_norm_per_pilot_delta':1.0,
          'operating_point_basis':'phoenix_source_motor_law_force_balance','certificate_schema':'Layer5-Phoenix-takeoff-command-map-v2',
          'bound_phase':'vertical_takeoff','surface_trim_basis':'phoenix_source_propwash_pitch_equilibrium',
          'surface_trim_rad':[-0.08,-0.08],'physics_basis':pb}}}
    out=_adapter(cert)._wingmpet_verification_mix([0.0,0.0,seed,0.0],phase='vertical_takeoff')
    assert out['ok'] is False
    assert out['reason']=='wingmpet_verification_vertical_takeoff_physics_basis_missing'
