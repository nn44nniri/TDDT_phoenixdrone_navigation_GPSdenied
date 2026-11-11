from pathlib import Path

from layer5.config import Layer5Config
from layer5.phoenix import PhoenixSpecialization


def _phoenix():
    root=Path(__file__).resolve().parents[1]
    return PhoenixSpecialization(Layer5Config.from_yaml(root/'configs/configuration_phoenix.yaml'))


def _certificate(ph):
    return {
        'schema':'PTLIB-Phoenix-control-commission-v2','ok':True,
        'sitl_only':True,'physics_held_during_commissioning':True,
        'phoenix_profile_sha256':ph.hash,
        'mixer':{'validated':True,
            'elevon_matrix_rad_per_norm':[[-0.33,0.33],[0.33,0.33]],
            'trim_rad':[0.08,0.08]},
        'trim':{'ready':True,'pitch_request_norm':0.24,'throttle_request_norm':0.34},
        'throttle_path':{'validated':True},
        'plant_surface_contract':{
            'schema':'Phoenix-JSBSim-WingMPET-surface-contract-v1',
            'transform':'wing_left=-raw_right;wing_right=-raw_left',
            'simulation_only':True,'direct_pwm':False,
            'ardupilot_mixer_authoritative':True},
    }


def test_online_surface_contract_preserves_roll_and_reverses_symmetric_pitch():
    ph=_phoenix()
    assert ph.apply_runtime_validation_certificate(_certificate(ph))['ok'] is True
    out=ph.to_wingmpet_surface_coordinates(0.08,0.12)
    assert out['ok'] is True
    left,right=out['physical']
    assert abs(0.5*(left+right)+0.10)<1e-12
    assert abs(0.5*(right-left)-0.02)<1e-12
    matrix=ph.runtime_wingmpet_surface_matrix_rad
    assert 0.5*(float(matrix[0,1])+float(matrix[1,1])) < 0.0


def test_online_surface_conversion_fails_closed_without_versioned_contract():
    ph=_phoenix(); cert=_certificate(ph); cert.pop('plant_surface_contract')
    assert ph.apply_runtime_validation_certificate(cert)['ok'] is True
    out=ph.to_wingmpet_surface_coordinates(0.08,0.12)
    assert out['ok'] is False
    assert out['reason']=='online_plant_surface_contract_missing'
