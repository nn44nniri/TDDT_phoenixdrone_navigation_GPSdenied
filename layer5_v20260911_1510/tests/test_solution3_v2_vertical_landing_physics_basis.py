from pathlib import Path

from layer5.config import Layer5Config
from layer5.phoenix import PhoenixSpecialization


def _phoenix():
    root=Path(__file__).resolve().parents[1]
    return PhoenixSpecialization(Layer5Config.from_yaml(root/'configs/configuration_phoenix.yaml'))


def test_vertical_landing_has_independent_descent_aware_physics_basis():
    ph=_phoenix()
    seed=ph.offline_vtol_seed_controls({'air_density_kg_m3':0.958,'battery_soc':0.993,
        'environment_fingerprint':'solution3-v2','vertical_landing_descent_rate_ref_m_s':1.2})
    assert seed['ok'] is True
    rec=seed['phases']['vertical_landing']
    pb=seed['physics_basis']
    lb=pb['vertical_landing_physics_basis']
    assert rec['operating_point_basis']=='phoenix_source_motor_law_landing_force_balance_reference'
    assert rec['landing_physics_basis_schema']=='Phoenix-vertical-landing-physics-basis-v1'
    assert lb['schema']=='Phoenix-vertical-landing-physics-basis-v1'
    assert lb['phase']=='vertical_landing'
    assert lb['physics_derived'] is True and lb['source_motor_law'] is True
    assert lb['landing_force_reference_valid'] is True
    assert lb['descent_rate_target_m_s']==1.2
    assert lb['terminal_agl_corridor_authority']=='Layer4 verified landing corridor'
    assert lb['terminal_agl_corridor_valid'] is True
    assert lb['terminal_agl_target_m']==0.0
    assert lb['max_collective_reduction_norm'] > 0.0
    assert lb['max_braking_boost_norm'] > 0.0
    assert lb['reference_thrust_to_weight_ratio'] >= 1.0
    assert lb['pilot_to_motor_mapping']['schema']=='Phoenix-pilot-to-motor-normalization-v1'
    # This is a landing contract, not a shallow alias to hover's state envelope.
    assert 'descent_rate_target_m_s' in lb
    assert 'terminal_agl_corridor_authority' in lb


def test_vertical_landing_basis_fails_closed_when_environment_invalid():
    ph=_phoenix()
    seed=ph.offline_vtol_seed_controls({'air_density_kg_m3':0.1,'battery_soc':0.993})
    lb=seed['physics_basis']['vertical_landing_physics_basis']
    assert seed['ok'] is False
    assert lb['air_density_context_valid'] is False
    assert lb['landing_force_reference_valid'] is False
