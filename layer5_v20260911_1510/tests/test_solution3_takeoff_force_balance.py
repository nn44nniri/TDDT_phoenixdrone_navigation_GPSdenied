from pathlib import Path
import math
from layer5.config import Layer5Config
from layer5.phoenix import PhoenixSpecialization


def test_solution3_takeoff_seed_uses_source_motor_law():
    root=Path(__file__).resolve().parents[1]
    cfg=Layer5Config.from_yaml(root/"configs/configuration_phoenix.yaml")
    ph=PhoenixSpecialization(cfg)
    seed=ph.offline_vtol_seed_controls()
    pb=seed["physics_basis"]
    assert seed["ok"] is True
    assert pb["source_motor_law"] is True
    expected_available=pb["motor_count"]*pb["motor_k_t_N_per_rad_s2"]*(pb["motor_omega_max_rad_s"]**2)
    assert abs(pb["available_total_thrust_N"]-expected_available) < 1e-9
    expected=math.sqrt(pb["vertical_takeoff_required_thrust_N"]/pb["available_total_thrust_N"])
    assert abs(seed["phases"]["vertical_takeoff"]["throttle_request_norm"]-expected)<1e-12
