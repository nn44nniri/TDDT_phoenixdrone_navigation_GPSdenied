from pathlib import Path
import math
from layer5.config import Layer5Config
from layer5.phoenix import PhoenixSpecialization


def _phoenix():
    root=Path(__file__).resolve().parents[1]
    return PhoenixSpecialization(Layer5Config.from_yaml(root/"configs/configuration_phoenix.yaml"))


def test_hover_operating_point_is_environment_bound_and_mapping_explicit():
    ph=_phoenix()
    seed=ph.offline_vtol_seed_controls({"air_density_kg_m3":0.958,"battery_soc":0.994,"environment_fingerprint":"abc"})
    assert seed["ok"] is True
    pb=seed["physics_basis"]; h=seed["phases"]["hover"]
    assert pb["air_density_kg_m3"]==0.958 and pb["battery_soc"]==0.994
    assert pb["environment_fingerprint"]=="abc"
    assert pb["air_density_context_valid"] is True and pb["battery_context_valid"] is True
    pm=pb["pilot_to_motor_mapping"]
    assert pm["schema"]=="Phoenix-pilot-to-motor-normalization-v1"
    assert pm["motor_norm_gain"]==1.0 and pm["motor_norm_offset"]==0.0
    assert h["pilot_throttle_norm"]==h["motor_norm"]==h["throttle_request_norm"]
    assert abs(h["omega_cmd_rad_s_per_motor"] - pb["motor_omega_max_rad_s"]*h["motor_norm"]) < 1e-12
    expected=pb["motor_count"]*pb["motor_k_t_N_per_rad_s2"]*h["omega_cmd_rad_s_per_motor"]**2
    assert abs(h["predicted_total_thrust_N"]-min(pb["available_total_thrust_N"],expected)) < 1e-10


def test_hover_low_soc_is_fail_closed_not_generic_forward_fallback():
    ph=_phoenix()
    seed=ph.offline_vtol_seed_controls({"air_density_kg_m3":1.0,"battery_soc":0.05})
    assert seed["ok"] is False
    assert seed["physics_basis"]["battery_context_valid"] is False
    a=[0.0]*19; b=[0.0]*19; a[9]=b[9]=2.0
    cand={"tdoc_phase":"hover","phoenix_operating_context":{"air_density_kg_m3":1.0,"battery_soc":0.05},"trajectory":[a,b],"actions":[[0]*5]}
    seq,diag=ph._trajectory_nominal_controls(cand)
    assert seq==[] and diag["reason"]=="vertical_operating_context_invalid"


def test_hover_density_outside_validity_envelope_is_fail_closed_without_fake_kt_scaling():
    ph=_phoenix()
    seed=ph.offline_vtol_seed_controls({"air_density_kg_m3":0.4,"battery_soc":1.0})
    assert seed["ok"] is False
    pb=seed["physics_basis"]
    assert pb["air_density_context_valid"] is False
    assert "not rho-scaled" in pb["air_density_semantics"]
