from pathlib import Path
import math

from layer5.config import Layer5Config
from layer5.phoenix import PhoenixSpecialization


def _phoenix():
    root=Path(__file__).resolve().parents[1]
    return PhoenixSpecialization(Layer5Config.from_yaml(root/'configs/configuration_phoenix.yaml'))


def test_takeoff_trim_is_source_propwash_derived_not_forward_trim():
    ph=_phoenix()
    seed=ph.offline_vtol_seed_controls({'air_density_kg_m3':1.0,'battery_soc':0.9})
    pb=seed['physics_basis']; rec=seed['phases']['vertical_takeoff']
    assert seed['ok'] is True
    assert pb['takeoff_pitch_trim_derived'] is True
    assert pb['takeoff_pitch_trim_valid'] is True
    assert rec['surface_trim_basis']=='phoenix_source_propwash_pitch_equilibrium'
    assert rec['surface_trim_rad']==[pb['takeoff_symmetric_elevon_trim_rad']]*2
    # Zero pitch-acceleration request must solve to neutral; critically it must not
    # inherit the legacy forward-flight -0.08 rad trim.
    assert abs(pb['takeoff_symmetric_elevon_trim_rad']) < 1e-12
    assert abs(pb['takeoff_symmetric_elevon_trim_rad'] - (-0.08)) > 0.05
    terms=pb['takeoff_propwash_terms']
    expected_slope=pb['motor_count']*(terms['omega_cmd_rad_s']**2)*(terms['k_pitch']+terms['cp_z_m']*terms['k_lift'])
    assert math.isclose(pb['takeoff_propwash_pitch_slope_Nm_per_rad'],expected_slope,rel_tol=0,abs_tol=1e-12)
    assert abs(pb['takeoff_pitch_moment_residual_Nm']) < 1e-12


def test_takeoff_collective_accepts_explicit_vertical_acceleration_request():
    ph=_phoenix()
    base=ph.offline_vtol_seed_controls({'air_density_kg_m3':1.0,'battery_soc':0.9})
    boosted=ph.offline_vtol_seed_controls({'air_density_kg_m3':1.0,'battery_soc':0.9,
        'vertical_takeoff_accel_request_m_s2':1.0,'vertical_takeoff_climb_rate_ref_m_s':2.5})
    assert boosted['physics_basis']['vertical_takeoff_thrust_basis']=='same_cycle_vertical_acceleration_request'
    assert boosted['physics_basis']['vertical_takeoff_accel_request_m_s2']==1.0
    assert boosted['physics_basis']['vertical_takeoff_climb_rate_ref_m_s']==2.5
    assert boosted['phases']['vertical_takeoff']['throttle_request_norm'] > base['phases']['vertical_takeoff']['throttle_request_norm']


def test_phase_specific_offline_mixer_uses_takeoff_trim_without_changing_global_trim():
    ph=_phoenix()
    # Minimal valid offline certificate around the existing model mixer.
    cert={
      'schema':'Layer5-Phoenix-offline-model-certificate-v4','ok':True,'learning_mode':'OFFLINE_SE',
      'simulation_only':True,'online_ready':False,'direct_pwm':False,'phoenix_profile_sha256':ph.hash,
      'airframe_binding_sha256':ph.airframe_binding_hash(),
      'mixer':{'model_qualified':True,'elevon_matrix_rad_per_norm':[[0.02,0.02],[-0.02,0.02]],'trim_rad':[-0.08,-0.08]},
      'trim':{'ready':True,'pitch_request_norm':0.0,'throttle_request_norm':0.30},
      'throttle_path':{'model_qualified':True},
      'phase_throttle_paths':{
        'vertical_takeoff':{'surface_trim_rad':[0.0,0.0]},
        'hover':{'certificate_schema':'Layer5-Phoenix-hover-command-map-v2','legacy_hover_operating_point_invalidated':True,'surface_trim_rad':[0.0,0.0]}
      }
    }
    assert ph.apply_runtime_validation_certificate(cert)['ok'] is True
    takeoff=ph.mix_physical([0.0,0.0,0.82,0.0],phase='vertical_takeoff')
    generic=ph.mix_physical([0.0,0.0,0.82,0.0])
    assert takeoff['ok'] is True and takeoff['physical'][:2]==[0.0,0.0]
    assert takeoff['surface_trim_source']=='phase_specific_offline_certificate'
    assert generic['physical'][:2]==[-0.08,-0.08]
