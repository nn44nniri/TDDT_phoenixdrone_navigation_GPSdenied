from __future__ import annotations

from types import SimpleNamespace
from tddt_tdoc.adapters.layer5 import Layer5Adapter


def test_vertical_phase_bridge_accepts_layer5_owned_seed_without_claiming_proof():
    seed=0.8207749772937714
    a=Layer5Adapter.__new__(Layer5Adapter); a.config=SimpleNamespace(get=lambda key, default=None: 'OFFLINE_SE' if key == 'layers.layer6.online.transport' else default); a._ensure=lambda: None
    a.engine=SimpleNamespace(phoenix=SimpleNamespace(elevon_max=0.53))
    a.phoenix_profile_sha256=lambda: "profile-A"
    a.phoenix_airframe_binding_sha256=lambda: "airframe-A"
    a.wingmpet_fmu_sha256=lambda: "wing-A"
    a._verification_model_certificate={
      "ok":True,"simulation_only":True,
      "mixer":{"elevon_matrix_rad_per_norm":[[0.02,0.02],[-0.02,0.02]],"trim_rad":[-0.08,-0.08]},
      "trim":{"throttle_request_norm":0.30},
      "throttle_path":{"qualified_delta_from_operating_request_norm":[-0.1,0.1],"trim_norm":0.30,"gain_motor_norm_per_pilot_delta":1.0},
      "phase_throttle_paths":{
        "vertical_takeoff":{"model_qualified":True,"request_bounds_norm":[seed-.04,seed+.04],"operating_request_norm":seed,"operating_motor_norm":seed,"gain_motor_norm_per_pilot_delta":1.0,
          "operating_point_basis":"phoenix_source_motor_law_force_balance","certificate_schema":"Layer5-Phoenix-takeoff-command-map-v2","bound_phase":"vertical_takeoff",
          "surface_trim_basis":"phoenix_source_propwash_pitch_equilibrium","surface_trim_rad":[0.0,0.0],
          "physics_basis":{"physics_derived":True,"source_motor_law":True,"force_balance_valid":True,"takeoff_pitch_trim_derived":True,"takeoff_pitch_trim_valid":True,"takeoff_symmetric_elevon_trim_rad":0.0}},
        "hover":{"model_qualified":True,"certificate_schema":"Layer5-Phoenix-hover-command-map-v2","bound_phase":"hover",
          "phoenix_profile_sha256":"profile-A","airframe_binding_sha256":"airframe-A","wingmpet_fmu_sha256":"wing-A",
          "climate_fingerprint":"epoch-A","state_envelope":{"air_density_kg_m3":[0.7,1.35],"battery_soc":[0.2,1.0],"airspeed_m_s":[0.0,9.0]},
          "legacy_hover_operating_point_invalidated":True,
          "request_bounds_norm":[seed-.04,seed+.04],"operating_request_norm":seed,"operating_motor_norm":seed,"gain_motor_norm_per_pilot_delta":1.0,
          "operating_point_basis":"phoenix_source_motor_law_hover_force_balance",
          "surface_trim_rad":[0.0,0.0],
          "physics_basis":{"physics_derived":True,"source_motor_law":True,"hover_force_balance_valid":True,"air_density_context_valid":True,"battery_context_valid":True,"pilot_to_motor_mapping":{"schema":"Phoenix-pilot-to-motor-normalization-v1","motor_norm_gain":1.0,"motor_norm_offset":0.0}}}}}
    for phase in ("vertical_takeoff","hover"):
      out=a._wingmpet_verification_mix([0.0,0.0,seed,0.0],phase=phase,operating_context=({"air_density_kg_m3":0.958,"battery_soc":0.993,"environment_fingerprint":"epoch-A"} if phase=="hover" else None),state_context=({"airspeed_m_s":4.5} if phase=="hover" else None))
      assert out["ok"] is True
      assert out["simulation_only"] is True
      assert out["not_field_evidence"] is True
      assert out["scope"] == "L4_WINGMPET_FULL_FIDELITY_ONLY"
      assert out["direct_pwm"] is False


def test_layer2_certificate_contains_seed_bound_takeoff_and_hover_paths():
    from pathlib import Path
    from tddt_tdoc.adapters.layer2 import Layer2Adapter
    a=Layer2Adapter.__new__(Layer2Adapter)
    a.twins_root=Path(__file__).resolve().parents[2]/"twins_layer2_v20260820_1233"
    a.wing=object(); a._ensure=lambda: None
    def fake_integrate(_wing,state,action,**kwargs):
        return dict(state),{"vtol_feasible":True}
    a._wing27_integrate=fake_integrate
    seed={"ok":True,"source":"test-source","simulation_only":True,"not_field_evidence":True,"direct_pwm":False,
          "equilibrium_collective_norm":0.7957749772937713,
          "physics_basis":{"physics_derived":True,"source_motor_law":True,"force_balance_valid":True,"hover_force_balance_valid":True,"mass_kg":0.65,"air_density_context_valid":True,"battery_context_valid":True,"pilot_to_motor_mapping":{"schema":"Phoenix-pilot-to-motor-normalization-v1","motor_norm_gain":1.0,"motor_norm_offset":0.0},"takeoff_pitch_trim_derived":True,"takeoff_pitch_trim_valid":True,"takeoff_symmetric_elevon_trim_rad":0.0,"vertical_landing_physics_basis":{"schema":"Phoenix-vertical-landing-physics-basis-v1","phase":"vertical_landing","physics_derived":True,"source_motor_law":True,"landing_force_reference_valid":True,"air_density_context_valid":True,"battery_context_valid":True,"terminal_agl_corridor_valid":True,"terminal_agl_corridor_authority":"Layer4 verified landing corridor","descent_rate_target_m_s":1.0,"max_collective_reduction_norm":0.08,"max_braking_boost_norm":0.06,"pilot_to_motor_mapping":{"schema":"Phoenix-pilot-to-motor-normalization-v1","motor_norm_gain":1.0,"motor_norm_offset":0.0}}},
          "phases":{
            "vertical_takeoff":{"throttle_request_norm":0.8207749772937714,"aileron_norm":0.0,"elevator_norm":0.0,"rudder_norm":0.0,"operating_point_basis":"phoenix_source_motor_law_force_balance","surface_trim_basis":"phoenix_source_propwash_pitch_equilibrium","surface_trim_rad":[0.0,0.0],"symmetric_elevon_trim_rad":0.0},
            "hover":{"throttle_request_norm":0.8207749772937714,"aileron_norm":0.0,"elevator_norm":0.0,"rudder_norm":0.0,"operating_point_basis":"phoenix_source_motor_law_hover_force_balance"},
            "vertical_landing":{"throttle_request_norm":0.8207749772937714,"aileron_norm":0.0,"elevator_norm":0.0,"rudder_norm":0.0,"operating_point_basis":"phoenix_source_motor_law_landing_force_balance_reference"}}}
    cert=a.derive_offline_phoenix_model_certificate("sha",phase_seed_controls=seed)
    assert cert["ok"] is True
    for phase in ("vertical_takeoff","hover"):
        rec=cert["phase_throttle_paths"][phase]
        assert rec["model_qualified"] is True
        assert rec["request_bounds_norm"][0] < 0.8207749772937714 < rec["request_bounds_norm"][1]
        assert 0.05 < rec["request_bounds_norm"][0]


def test_layer4_persists_phase_on_verified_candidate_contract():
    import inspect
    from tddt_tdoc.adapters.layer4 import Layer4Adapter
    src=inspect.getsource(Layer4Adapter._tdoc_full_fidelity)
    assert 'cdict["tdoc_phase"]=phase_contract["requested_stage"]' in src
