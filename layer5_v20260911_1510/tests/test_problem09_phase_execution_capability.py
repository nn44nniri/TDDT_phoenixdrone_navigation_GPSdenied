from pathlib import Path
import copy, sys, yaml
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from layer5.config import Layer5Config
from layer5.phoenix import PhoenixSpecialization
BASE=yaml.safe_load((ROOT/'configs/configuration_phoenix.yaml').read_text())

def cfg():
    return Layer5Config(copy.deepcopy(BASE),ROOT/'configs/configuration_phoenix.yaml')

def online_cert(ph):
    phase_path={
      'validated':True,'request_bounds_norm':[0.65,0.85],
      'target_motor_bounds_norm':[0.70,0.90],
      'target_motor_operating_norm':0.80,
      'operating_request_norm':0.75,
      'operating_left_motor_norm':0.80,'operating_right_motor_norm':0.80,
      'left_gain_motor_norm_per_request_norm':1.0,
      'right_gain_motor_norm_per_request_norm':1.0,
      'motor_feedback_quantization_norm':0.001,
      'max_target_motor_error_norm':0.001,'max_affine_residual_norm':0.001,
      'measurement_scope':'held_sitl_pilot_domain_through_ArduPilot_mixer',
      'source':'test_commissioning_fixture',
    }
    return {
      'schema':'PTLIB-Phoenix-control-commission-v2','ok':True,'sitl_only':True,
      'physics_held_during_commissioning':True,'phoenix_profile_sha256':ph.hash,
      'mixer':{'validated':True,'elevon_matrix_rad_per_norm':[[-0.30,0.10],[0.30,0.10]],'trim_rad':[0.0,0.0]},
      'trim':{'ready':True,'pitch_request_norm':0.0,'throttle_request_norm':0.34},
      'throttle_path':{'validated':True},'nominal_tracker_authorized':True,
      'servo_dynamics':{'identified':False,'measured':False},
      'esc_mapping':{'identified':False,'measured':False},
      'phase_throttle_paths':{'vertical_takeoff':dict(phase_path),'hover':dict(phase_path)},
      'direct_pwm':False,
    }

def test_problem09_phase_execution_is_compositional_not_autopilot_claim():
    ph=PhoenixSpecialization(cfg())
    assert ph.vtol_autopilot_controller_validated is False
    assert ph.true_zero_speed_hover_validated is False
    assert ph.aero_full_validated is False
    assert ph.apply_runtime_validation_certificate(online_cert(ph))['ok'] is True
    cap=ph.phase_execution_capability()
    assert all(cap['phase_supported'][p] for p in ('vertical_takeoff','hover','transition_forward','transition_back','vertical_landing'))
    assert cap['ardupilot_mixer_authoritative'] is True
    assert cap['direct_pwm'] is False
    assert cap['claim_limits']['vtol_autopilot_controller_validated'] is False
    assert cap['claim_limits']['true_zero_speed_hover_dynamic_validated'] is False
    assert cap['claim_limits']['aerodynamics_full_envelope_validated'] is False
    assert cap['claim_limits']['qlpv_nonzero_correction_ready'] is False

def test_problem09_without_commission_certificate_remains_fail_closed():
    ph=PhoenixSpecialization(cfg())
    cap=ph.phase_execution_capability()
    assert cap['phase_supported']['hover'] is False
    assert 'runtime_control_certificate_not_accepted' in cap['phase_blockers']['hover']
