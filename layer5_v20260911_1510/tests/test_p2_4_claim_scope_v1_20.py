from pathlib import Path
import copy, yaml

from layer5.config import Layer5Config
from layer5.phoenix import PhoenixSpecialization

ROOT=Path(__file__).resolve().parents[1]
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

def test_claim_scope_is_limited_to_sitl_for_current_profile():
    ph=PhoenixSpecialization(cfg())
    scope=ph.claim_scope(); limits=scope['claim_limits']
    assert scope['status']=='CLAIM_LIMITED_TO_SITL'
    assert scope['scope_ceiling']=='SITL_SIMULATION_ONLY'
    assert scope['current_sitl_category_permitted'] is True
    assert scope['claim_limits_block_current_sitl_by_themselves'] is False
    assert scope['beyond_sitl_claims_allowed'] is False
    assert scope['commissioning_may_upgrade_claim_limits'] is False
    assert limits['vtol_autopilot_controller_validated'] is False
    assert limits['aerodynamics_full_envelope_validated'] is False
    assert limits['real_aircraft_flight_validated'] is False

def test_successful_sitl_commissioning_does_not_upgrade_broader_claims():
    ph=PhoenixSpecialization(cfg())
    before=ph.claim_limits().copy()
    assert ph.apply_runtime_validation_certificate(online_cert(ph))['ok'] is True
    cap=ph.phase_execution_capability(); after=cap['claim_limits']; scope=cap['claim_scope']
    assert cap['phase_supported']['hover'] is True
    assert cap['phase_supported']['transition_forward'] is True
    assert after['vtol_autopilot_controller_validated'] is before['vtol_autopilot_controller_validated'] is False
    assert after['aerodynamics_full_envelope_validated'] is before['aerodynamics_full_envelope_validated'] is False
    assert after['real_aircraft_flight_validated'] is before['real_aircraft_flight_validated'] is False
    assert scope['status']=='CLAIM_LIMITED_TO_SITL'
    assert scope['beyond_sitl_claims_allowed'] is False

def test_real_aircraft_claim_is_source_bound_not_hard_promoted():
    ph=PhoenixSpecialization(cfg())
    assert ph.real_aircraft_flight_validated is False
    assert ph.status()['real_aircraft_flight_validated'] is False
    assert ph.status()['claim_scope']['scope_ceiling']=='SITL_SIMULATION_ONLY'
