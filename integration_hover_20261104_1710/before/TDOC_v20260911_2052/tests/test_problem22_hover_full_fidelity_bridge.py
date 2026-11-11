from __future__ import annotations

from types import SimpleNamespace

from tddt_tdoc.adapters.layer5 import Layer5Adapter


SEED=0.8207749772937714

def _adapter_with_phase_certificate():
    a=Layer5Adapter.__new__(Layer5Adapter)


    # Within _adapter_with_phase_certificate()
    a.config = SimpleNamespace(
        get=lambda key, default=None: (
            "OFFLINE_SE"
            if key == "layers.layer6.online.transport"
            else default
        )
    )
    a.engine=SimpleNamespace(phoenix=SimpleNamespace(elevon_max=0.53))
    a._ensure=lambda: None
    a.phoenix_profile_sha256=lambda: "profile-A"
    a.phoenix_airframe_binding_sha256=lambda: "airframe-A"
    a.wingmpet_fmu_sha256=lambda: "wing-A"
    a._verification_model_certificate={
        "schema":"Layer5-Phoenix-offline-model-certificate-v3","ok":True,"simulation_only":True,
        "mixer":{"elevon_matrix_rad_per_norm":[[0.02,0.02],[-0.02,0.02]],"trim_rad":[-0.08,-0.08]},
        "trim":{"pitch_request_norm":0.0,"throttle_request_norm":0.30},
        "throttle_path":{"qualified_delta_from_operating_request_norm":[-0.10,0.10],"trim_norm":0.30,"gain_motor_norm_per_pilot_delta":1.0},
        "phase_throttle_paths":{
            "hover":{"model_qualified":True,"certificate_schema":"Layer5-Phoenix-hover-command-map-v2","bound_phase":"hover","phoenix_profile_sha256":"profile-A","airframe_binding_sha256":"airframe-A","wingmpet_fmu_sha256":"wing-A","climate_fingerprint":"epoch-A","state_envelope":{"air_density_kg_m3":[0.7,1.35],"battery_soc":[0.2,1.0],"airspeed_m_s":[0.0,9.0]},"legacy_hover_operating_point_invalidated":True,"request_bounds_norm":[SEED-0.04,SEED+0.04],"operating_request_norm":SEED,"operating_motor_norm":SEED,"gain_motor_norm_per_pilot_delta":1.0,
                     "qualification_scope":"command_mapping_only; physical vertical-regime feasibility deferred to FF replay",
                     "operating_point_basis":"phoenix_source_motor_law_hover_force_balance",
                     "surface_trim_rad":[0.0,0.0],
                     "physics_basis":{"physics_derived":True,"source_motor_law":True,"hover_force_balance_valid":True,"air_density_context_valid":True,"battery_context_valid":True,"pilot_to_motor_mapping":{"schema":"Phoenix-pilot-to-motor-normalization-v1","motor_norm_gain":1.0,"motor_norm_offset":0.0}}},
            "vertical_takeoff":{"model_qualified":True,"certificate_schema":"Layer5-Phoenix-takeoff-command-map-v2","bound_phase":"vertical_takeoff","request_bounds_norm":[SEED-0.04,SEED+0.04],"operating_request_norm":SEED,"operating_motor_norm":SEED,"gain_motor_norm_per_pilot_delta":1.0,
                     "qualification_scope":"command_mapping_only; physical vertical-regime feasibility deferred to FF replay",
                     "operating_point_basis":"phoenix_source_motor_law_force_balance","surface_trim_basis":"phoenix_source_propwash_pitch_equilibrium","surface_trim_rad":[0.0,0.0],
                     "physics_basis":{"physics_derived":True,"source_motor_law":True,"force_balance_valid":True,"takeoff_pitch_trim_derived":True,"takeoff_pitch_trim_valid":True,"takeoff_symmetric_elevon_trim_rad":0.0}},
        }}
    return a


def test_hover_solution2_seed_is_phase_qualified():
    out=_adapter_with_phase_certificate()._wingmpet_verification_mix([0.0,0.0,SEED,0.0],phase="hover",operating_context={"air_density_kg_m3":0.958,"battery_soc":0.993,"environment_fingerprint":"epoch-A"},state_context={"airspeed_m_s":4.5})
    assert out["ok"] is True
    assert out["phase_throttle_qualification"] == "hover_command_mapping_only"
    assert out["physical"] == [0.0,0.0,SEED,SEED]


def test_vertical_takeoff_solution2_seed_is_phase_qualified():
    out=_adapter_with_phase_certificate()._wingmpet_verification_mix([0.0,0.0,SEED,0.0],phase="vertical_takeoff")
    assert out["ok"] is True
    assert out["phase_throttle_qualification"] == "vertical_takeoff_command_mapping_only"


def test_legacy_hover_005_is_no_longer_promoted_after_solution2():
    out=_adapter_with_phase_certificate()._wingmpet_verification_mix([0.0,0.0,0.05,0.0],phase="hover",operating_context={"air_density_kg_m3":0.958,"battery_soc":0.993,"environment_fingerprint":"epoch-A"},state_context={"airspeed_m_s":4.5})
    assert out["ok"] is False
    assert out["reason"] == "wingmpet_verification_hover_throttle_outside_qualified_neighborhood"


def test_generic_forward_certificate_remains_unchanged():
    out=_adapter_with_phase_certificate()._wingmpet_verification_mix([0.0,0.0,0.05,0.0],phase="cruise")
    assert out["ok"] is False
    assert out["reason"] == "wingmpet_verification_throttle_outside_qualified_neighborhood"
    assert out["delta_from_operating_request_norm"] == -0.25
