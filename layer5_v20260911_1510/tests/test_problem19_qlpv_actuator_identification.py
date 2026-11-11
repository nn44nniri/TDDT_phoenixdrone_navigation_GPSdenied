from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))

from layer5.config import Layer5Config
from layer5.phoenix import PhoenixSpecialization
from layer5.qlpv import QLPVCorrectionKernel


def cfg():
    return Layer5Config.from_yaml(ROOT/'configs/configuration_phoenix.yaml')


def cert_v2():
    return {
      'schema':'PTLIB-Phoenix-control-commission-v2','ok':True,'sitl_only':True,
      'physics_held_during_commissioning':True,'direct_pwm':False,
      'mixer':{'validated':True,'elevon_matrix_rad_per_norm':[[0.30,0.20],[-0.30,0.20]],'trim_rad':[0.05,0.05]},
      'trim':{'ready':True,'pitch_request_norm':0.20,'throttle_request_norm':0.35},
      'throttle_path':{'validated':True,'operating_request_norm':0.35,
          'operating_left_motor_norm':0.35,'operating_right_motor_norm':0.35,
          'left_gain_motor_norm_per_request_norm':1.0,'right_gain_motor_norm_per_request_norm':1.0},
      'nominal_tracker_authorized':True,
      'servo_dynamics':{'identified':True,'measured':True,'simulation_only':True,
          'scope':'effective_sitl_command_path','tau_s':[0.02,0.021],
          'rate_limit_rad_s':[1.2,1.1]},
      'esc_mapping':{'identified':True,'measured':True,'simulation_only':True,
          'scope':'effective_sitl_command_path','operating_request_norm':0.35,
          'operating_left_motor_norm':0.35,'operating_right_motor_norm':0.35,
          'left_gain_motor_norm_per_request_norm':1.0,'right_gain_motor_norm_per_request_norm':1.0},
      'qlpv_nonzero_correction_authorized':False,
      'authorization_owner':'Layer5 qLPV kernel + PSF/RTA/Governance',
    }


def test_v2_certificate_identifies_effective_sitl_actuator_gates_only():
    c=cfg(); ph=PhoenixSpecialization(c)
    out=ph.apply_runtime_validation_certificate(cert_v2())
    assert out['ok'] and out['accepted']
    gates=ph.correction_validation_gates()
    assert gates['servo_dynamics'] is True
    assert gates['esc_mapping'] is True
    assert gates['qlpv_nonzero_correction'] is True
    assert ph.direct_pwm_allowed is False
    ev=ph.status()['actuator_identification_evidence']
    assert ev['simulation_only_identification'] is True
    assert ev['identification_scope']=='effective_sitl_command_path'


def test_runtime_actuator_qLPV_model_is_bound_and_airframe_matched():
    c=cfg(); ph=PhoenixSpecialization(c); cert=cert_v2()
    assert ph.apply_runtime_validation_certificate(cert)['ok']
    q=QLPVCorrectionKernel(c,ph)
    assert q.status()['model_loaded'] is False
    b=q.bind_runtime_actuator_model(cert)
    assert b['ok'] is True and b['simulation_only'] is True
    st=q.status()
    assert st['model_loaded'] is True
    assert st['calibrated'] is True
    assert st['airframe_binding_match'] is True
    assert st['nonzero_correction_authorized'] is True
    assert st['model_source']=='runtime_sitl_actuator_identification_v1'
    assert st['model_scope']=='layer5_local_actuator_correction_only'


def test_v1_static_certificate_cannot_unlock_runtime_actuator_model():
    c=cfg(); ph=PhoenixSpecialization(c)
    old=cert_v2(); old['schema']='PTLIB-Phoenix-control-commission-v1'
    old['servo_dynamics']={'identified':False,'measured':False}
    old['esc_mapping']={'identified':False,'measured':False}
    assert ph.apply_runtime_validation_certificate(old)['ok']
    q=QLPVCorrectionKernel(c,ph)
    out=q.bind_runtime_actuator_model(old)
    assert out['ok'] is False
    assert q.status()['nonzero_correction_authorized'] is False
