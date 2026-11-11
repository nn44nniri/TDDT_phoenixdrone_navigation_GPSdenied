from types import SimpleNamespace
from tddt_tdoc.adapters.layer5 import Layer5Adapter

BASE=0.8208448785

def _a(with_physics=True):
    a=Layer5Adapter.__new__(Layer5Adapter); a.config=SimpleNamespace(get=lambda key, default=None: 'OFFLINE_SE' if key == 'layers.layer6.online.transport' else default); a._ensure=lambda: None
    a.phoenix_profile_sha256=lambda: "profile-A"
    a.phoenix_airframe_binding_sha256=lambda: "airframe-A"
    a.wingmpet_fmu_sha256=lambda: "wing-A"
    a.engine=SimpleNamespace(phoenix=SimpleNamespace(elevon_max=0.53))
    hp={"model_qualified":True,"certificate_schema":"Layer5-Phoenix-hover-command-map-v2","bound_phase":"hover","phoenix_profile_sha256":"profile-A","airframe_binding_sha256":"airframe-A","wingmpet_fmu_sha256":"wing-A","climate_fingerprint":"epoch-A","state_envelope":{"air_density_kg_m3":[0.7,1.35],"battery_soc":[0.2,1.0],"airspeed_m_s":[0.0,9.0]},"legacy_hover_operating_point_invalidated":True,"request_bounds_norm":[BASE-.04,BASE+.04],"operating_request_norm":BASE,"operating_motor_norm":BASE,"gain_motor_norm_per_pilot_delta":1.0,"operating_point_basis":"phoenix_source_motor_law_hover_force_balance","surface_trim_rad":[0.0,0.0]}
    if with_physics:
        hp["physics_basis"]={"physics_derived":True,"source_motor_law":True,"hover_force_balance_valid":True,"air_density_context_valid":True,"battery_context_valid":True,"pilot_to_motor_mapping":{"schema":"Phoenix-pilot-to-motor-normalization-v1","motor_norm_gain":1.0,"motor_norm_offset":0.0}}
    a._verification_model_certificate={"ok":True,"mixer":{"elevon_matrix_rad_per_norm":[[0.02,0.02],[-0.02,0.02]],"trim_rad":[-0.08,-0.08]},"trim":{"throttle_request_norm":0.3},"throttle_path":{"qualified_delta_from_operating_request_norm":[-.1,.1],"trim_norm":.3,"gain_motor_norm_per_pilot_delta":1.0},"phase_throttle_paths":{"hover":hp}}
    return a

def test_hover_bridge_requires_force_balance_provenance():
    out=_a(False)._wingmpet_verification_mix([0,0,BASE,0],phase="hover",operating_context={"air_density_kg_m3":0.958,"battery_soc":0.993,"environment_fingerprint":"epoch-A"},state_context={"airspeed_m_s":4.5})
    assert out["ok"] is False
    assert out["reason"]=="wingmpet_verification_hover_physics_basis_missing"

def test_hover_bridge_accepts_bounded_physics_derived_command_only_for_ff():
    out=_a(True)._wingmpet_verification_mix([0,0,BASE,0],phase="hover",operating_context={"air_density_kg_m3":0.958,"battery_soc":0.993,"environment_fingerprint":"epoch-A"},state_context={"airspeed_m_s":4.5})
    assert out["ok"] is True
    assert out["physical"]==[0.0,0.0,BASE,BASE]
    assert out["phase_surface_trim_rad"]==[0.0,0.0]
    assert out["scope"]=="L4_WINGMPET_FULL_FIDELITY_ONLY"
    assert out["simulation_only"] is True and out["not_field_evidence"] is True
    assert out["direct_pwm"] is False

def test_hover_bridge_remains_fail_closed_outside_qualified_envelope():
    out=_a(True)._wingmpet_verification_mix([0,0,BASE+.05,0],phase="hover",operating_context={"air_density_kg_m3":0.958,"battery_soc":0.993,"environment_fingerprint":"epoch-A"},state_context={"airspeed_m_s":4.5})
    assert out["ok"] is False
    assert out["reason"]=="wingmpet_verification_hover_throttle_outside_qualified_neighborhood"
