from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import math

from tddt_tdoc.adapters.layer5 import Layer5Adapter


def _adapter(seed=0.819, *, legacy_ptlib_simulation=False):
    a=Layer5Adapter.__new__(Layer5Adapter); a.config=SimpleNamespace(get=lambda key, default=None: ('LEGACY_PTLIB_SIMULATION_FIXTURE' if legacy_ptlib_simulation else 'OFFLINE_SE') if key == 'layers.layer6.online.transport' else default)
    a._ensure=lambda: None
    a.engine=SimpleNamespace(phoenix=SimpleNamespace(elevon_max=0.53))
    a._verification_model_certificate={
      "ok":True,"simulation_only":True,
      "mixer":{"elevon_matrix_rad_per_norm":[[0.02,0.02],[-0.02,0.02]],"trim_rad":[-0.08,-0.08]},
      "trim":{"throttle_request_norm":0.30},
      "throttle_path":{"qualified_delta_from_operating_request_norm":[-0.1,0.1],"trim_norm":0.30,"gain_motor_norm_per_pilot_delta":1.0},
      "phase_throttle_paths":{
        "vertical_takeoff":{
          "model_qualified":True,"request_bounds_norm":[seed-.04,seed+.04],
          "operating_request_norm":seed,"operating_motor_norm":seed,"gain_motor_norm_per_pilot_delta":1.0,
          "operating_point_basis":"phoenix_source_motor_law_force_balance",
          "certificate_schema":"Layer5-Phoenix-takeoff-command-map-v2","bound_phase":"vertical_takeoff",
          "surface_trim_basis":"phoenix_source_propwash_pitch_equilibrium","surface_trim_rad":[0.0,0.0],
          "physics_basis":{"physics_derived":True,"source_motor_law":True,"force_balance_valid":True,
            "takeoff_pitch_trim_derived":True,"takeoff_pitch_trim_valid":True,"takeoff_symmetric_elevon_trim_rad":0.0}}}}
    return a


def test_takeoff_bridge_accepts_only_physics_bound_operating_point():
    a=_adapter()
    out=a._wingmpet_verification_mix([0.0,0.0,0.819,0.0],phase="vertical_takeoff")
    assert out["ok"] is True
    assert out["phase"] == "vertical_takeoff"
    assert out["phase_throttle_qualification"] == "vertical_takeoff_command_mapping_only"
    assert out["simulation_only"] is True and out["not_field_evidence"] is True
    assert out["direct_pwm"] is False


def test_takeoff_bridge_rejects_phase_path_without_force_balance_provenance():
    a=_adapter()
    a._verification_model_certificate["phase_throttle_paths"]["vertical_takeoff"].pop("physics_basis")
    out=a._wingmpet_verification_mix([0.0,0.0,0.819,0.0],phase="vertical_takeoff")
    assert out["ok"] is False
    assert out["reason"] == "wingmpet_verification_vertical_takeoff_physics_basis_missing"


def test_takeoff_bridge_still_fails_closed_outside_bounded_neighborhood():
    a=_adapter()
    out=a._wingmpet_verification_mix([0.0,0.0,0.90,0.0],phase="vertical_takeoff")
    assert out["ok"] is False
    assert out["reason"] == "wingmpet_verification_vertical_takeoff_throttle_outside_qualified_neighborhood"


def test_online_takeoff_bridge_replays_commissioned_ardupilot_motor_map():
    a=_adapter(seed=0.82, legacy_ptlib_simulation=True)
    phoenix=a.engine.phoenix
    phoenix.runtime_certificate={"accepted":True,"kind":"ONLINE_SITL","certificate":{
        "schema":"PTLIB-Phoenix-control-commission-v2","ok":True,"mixer":{"validated":True}}}
    phoenix.runtime_mixer_matrix_rad=[[0.02,0.02],[-0.02,0.02]]
    phoenix.nominal_tracker_authorized=True
    phoenix.runtime_phase_throttle_affine={"vertical_takeoff":{
        "request_bounds_norm":[0.72,0.80],"operating_request_norm":0.756,
        "target_motor_bounds_norm":[0.78,0.86],"target_motor_operating_norm":0.82,
        "operating_left_motor_norm":0.82,"operating_right_motor_norm":0.82,
        "left_gain_motor_norm_per_request_norm":1.25,"right_gain_motor_norm_per_request_norm":1.25}}
    phoenix.to_wingmpet_surface_coordinates=lambda left,right:{"ok":True,"physical":[left,right]}
    out=a._wingmpet_verification_mix([0.0,0.0,0.756,0.0],phase="vertical_takeoff",
        operating_context={"attitude_rate_state":{"source":"PT_LIB/FMI2","simulation_only":True}})
    assert out["ok"] is True
    assert all(math.isclose(v,0.82,abs_tol=1e-12) for v in out["physical"][2:])
    assert out["throttle_mapping_source"] == "online_held_sitl_phase_range_ArduPilot_commissioning"


def test_online_takeoff_bridge_projects_only_quantized_endpoint_into_wingmpet_domain():
    a=_adapter(seed=0.8208448785331272, legacy_ptlib_simulation=True); phoenix=a.engine.phoenix
    phoenix.runtime_certificate={"accepted":True,"kind":"ONLINE_SITL","certificate":{
        "schema":"PTLIB-Phoenix-control-commission-v2","ok":True,"mixer":{"validated":True}}}
    phoenix.runtime_mixer_matrix_rad=[[0.02,0.02],[-0.02,0.02]]
    phoenix.nominal_tracker_authorized=True
    phoenix.runtime_phase_throttle_affine={"vertical_takeoff":{
        "request_bounds_norm":[0.7248759028265017,0.7888759028265018],
        "operating_request_norm":0.7568759028265017,
        "target_motor_bounds_norm":[0.7808448785331271,0.8608448785331272],
        "target_motor_operating_norm":0.8208448785331272,
        "operating_left_motor_norm":0.821,"operating_right_motor_norm":0.821,
        "left_gain_motor_norm_per_request_norm":1.25,"right_gain_motor_norm_per_request_norm":1.25,
        "motor_feedback_quantization_norm":0.001,"max_target_motor_error_norm":0.0001551214668729095,
        "max_affine_residual_norm":0.0}}
    phoenix.to_wingmpet_surface_coordinates=lambda left,right:{"ok":True,"physical":[left,right]}
    out=a._wingmpet_verification_mix([0.0,0.0,0.7888759028265018,0.0],phase="vertical_takeoff",
        operating_context={"attitude_rate_state":{"source":"PT_LIB/FMI2","simulation_only":True}})
    assert out["ok"] is True
    assert out["physical"][2:] == [0.8608448785331272,0.8608448785331272]
    assert out["measurement_tolerance_projection"]["applied"] is True
    assert out["measurement_tolerance_projection"]["runtime_command_modified"] is False
    assert out["measurement_tolerance_projection"]["wingmpet_domain_expanded"] is False


def test_online_takeoff_bridge_rejects_motor_error_beyond_commissioned_tolerance():
    a=_adapter(seed=0.82, legacy_ptlib_simulation=True); phoenix=a.engine.phoenix
    phoenix.runtime_certificate={"accepted":True,"kind":"ONLINE_SITL","certificate":{
        "schema":"PTLIB-Phoenix-control-commission-v2","ok":True,"mixer":{"validated":True}}}
    phoenix.runtime_mixer_matrix_rad=[[0.02,0.02],[-0.02,0.02]]
    phoenix.nominal_tracker_authorized=True
    phoenix.runtime_phase_throttle_affine={"vertical_takeoff":{
        "request_bounds_norm":[0.72,0.80],"operating_request_norm":0.756,
        "target_motor_bounds_norm":[0.78,0.86],"target_motor_operating_norm":0.82,
        "operating_left_motor_norm":0.823,"operating_right_motor_norm":0.823,
        "left_gain_motor_norm_per_request_norm":1.25,"right_gain_motor_norm_per_request_norm":1.25,
        "motor_feedback_quantization_norm":0.001,"max_target_motor_error_norm":0.0002,
        "max_affine_residual_norm":0.0}}
    phoenix.to_wingmpet_surface_coordinates=lambda left,right:{"ok":True,"physical":[left,right]}
    out=a._wingmpet_verification_mix([0.0,0.0,0.80,0.0],phase="vertical_takeoff",
        operating_context={"attitude_rate_state":{"source":"PT_LIB/FMI2","simulation_only":True}})
    assert out["ok"] is False
    assert out["reason"]=="wingmpet_verification_vertical_takeoff_effective_motor_outside_qualified_neighborhood"


def test_online_takeoff_bridge_fails_closed_without_phase_range_commissioning():
    a=_adapter(seed=0.82, legacy_ptlib_simulation=True)
    phoenix=a.engine.phoenix
    phoenix.runtime_certificate={"accepted":True,"kind":"ONLINE_SITL","certificate":{
        "schema":"PTLIB-Phoenix-control-commission-v2","ok":True,"mixer":{"validated":True}}}
    phoenix.runtime_mixer_matrix_rad=[[0.02,0.02],[-0.02,0.02]]
    phoenix.nominal_tracker_authorized=True
    phoenix.runtime_phase_throttle_affine={}
    phoenix.to_wingmpet_surface_coordinates=lambda left,right:{"ok":True,"physical":[left,right]}
    out=a._wingmpet_verification_mix([0.0,0.0,0.80,0.0],phase="vertical_takeoff",
        operating_context={"attitude_rate_state":{"source":"PT_LIB/FMI2","simulation_only":True}})
    assert out["ok"] is False
    assert out["reason"] == "wingmpet_verification_vertical_takeoff_online_execution_mapping_missing"


def test_online_takeoff_bridge_rejects_commissioned_target_outside_model_envelope():
    a=_adapter(seed=0.82, legacy_ptlib_simulation=True)
    phoenix=a.engine.phoenix
    phoenix.runtime_certificate={"accepted":True,"kind":"ONLINE_SITL","certificate":{
        "schema":"PTLIB-Phoenix-control-commission-v2","ok":True,"mixer":{"validated":True}}}
    phoenix.runtime_mixer_matrix_rad=[[0.02,0.02],[-0.02,0.02]]
    phoenix.nominal_tracker_authorized=True
    phoenix.runtime_phase_throttle_affine={"vertical_takeoff":{
        "request_bounds_norm":[0.72,0.80],"operating_request_norm":0.756,
        "target_motor_bounds_norm":[0.78,0.88],"target_motor_operating_norm":0.82,
        "operating_left_motor_norm":0.82,"operating_right_motor_norm":0.82,
        "left_gain_motor_norm_per_request_norm":1.25,"right_gain_motor_norm_per_request_norm":1.25}}
    phoenix.to_wingmpet_surface_coordinates=lambda left,right:{"ok":True,"physical":[left,right]}
    out=a._wingmpet_verification_mix([0.0,0.0,0.756,0.0],phase="vertical_takeoff",
        operating_context={"attitude_rate_state":{"source":"PT_LIB/FMI2","simulation_only":True}})
    assert out["ok"] is False
    assert out["reason"] == "wingmpet_verification_vertical_takeoff_online_target_outside_model_envelope"
