from types import SimpleNamespace
from tddt_tdoc.adapters.layer5 import Layer5Adapter

BASE=0.8208448785
CTX={"air_density_kg_m3":0.958,"battery_soc":0.993,"environment_fingerprint":"epoch-A"}
STATE={"airspeed_m_s":4.5}

def adapter(*, schema="Layer5-Phoenix-hover-command-map-v2", fp="epoch-A", fmu="wing-A", profile="profile-A", airframe="airframe-A"):
    a=Layer5Adapter.__new__(Layer5Adapter); a.config=SimpleNamespace(get=lambda key, default=None: 'OFFLINE_SE' if key == 'layers.layer6.online.transport' else default); a._ensure=lambda: None
    a.engine=SimpleNamespace(phoenix=SimpleNamespace(elevon_max=0.53))
    a.phoenix_profile_sha256=lambda: "profile-A"
    a.phoenix_airframe_binding_sha256=lambda: "airframe-A"
    a.wingmpet_fmu_sha256=lambda: "wing-A"
    hp={"model_qualified":True,"certificate_schema":schema,"bound_phase":"hover",
        "request_bounds_norm":[BASE-.04,BASE+.04],"operating_request_norm":BASE,"operating_motor_norm":BASE,
        "gain_motor_norm_per_pilot_delta":1.0,"surface_trim_rad":[0.0,0.0],
        "phoenix_profile_sha256":profile,"airframe_binding_sha256":airframe,"wingmpet_fmu_sha256":fmu,
        "climate_fingerprint":fp,"state_envelope":{"air_density_kg_m3":[0.7,1.35],"battery_soc":[0.2,1.0],"airspeed_m_s":[0.0,9.0]},
        "action_envelope":{"pilot_throttle_norm":[BASE-.04,BASE+.04],"motor_norm":[BASE-.04,BASE+.04]},
        "operating_point_basis":"phoenix_source_motor_law_hover_force_balance","legacy_hover_operating_point_invalidated":True,
        "physics_basis":{"physics_derived":True,"source_motor_law":True,"hover_force_balance_valid":True,"air_density_context_valid":True,"battery_context_valid":True,
            "pilot_to_motor_mapping":{"schema":"Phoenix-pilot-to-motor-normalization-v1","motor_norm_gain":1.0,"motor_norm_offset":0.0}}}
    a._verification_model_certificate={"schema":"Layer5-Phoenix-offline-model-certificate-v4","ok":True,"simulation_only":True,
        "mixer":{"elevon_matrix_rad_per_norm":[[0.02,0.02],[-0.02,0.02]],"trim_rad":[-0.08,-0.08]},
        "trim":{"throttle_request_norm":0.3},"throttle_path":{"qualified_delta_from_operating_request_norm":[-.1,.1],"trim_norm":.3,"gain_motor_norm_per_pilot_delta":1.0},
        "phase_throttle_paths":{"hover":hp}}
    return a

def mix(a, t=BASE, ctx=CTX, state=STATE):
    return a._wingmpet_verification_mix([0,0,t,0],phase="hover",operating_context=ctx,state_context=state)

def test_solution7_bound_hover_certificate_accepts_matching_context():
    out=mix(adapter()); assert out["ok"] is True; assert out["physical"]==[0.0,0.0,BASE,BASE]

def test_solution7_legacy_hover_certificate_is_invalidated():
    out=mix(adapter(schema=None)); assert out["ok"] is False; assert out["reason"]=="wingmpet_verification_hover_certificate_legacy_or_unbound"

def test_solution7_old_005_operating_point_is_rejected():
    out=mix(adapter(),0.05); assert out["ok"] is False; assert "outside_qualified_neighborhood" in out["reason"]

def test_solution7_climate_fingerprint_mismatch_fails_closed():
    out=mix(adapter(),ctx={**CTX,"environment_fingerprint":"epoch-B"}); assert out["ok"] is False; assert out["reason"]=="wingmpet_verification_hover_climate_fingerprint_mismatch"

def test_solution7_state_envelope_mismatch_fails_closed():
    out=mix(adapter(),state={"airspeed_m_s":10.0}); assert out["ok"] is False; assert "outside_certificate_envelope" in out["reason"]

def test_solution7_model_and_airframe_hashes_are_enforced():
    assert mix(adapter(fmu="wing-B"))["reason"]=="wingmpet_verification_hover_wingmpet_fmu_hash_mismatch"
    assert mix(adapter(airframe="airframe-B"))["reason"]=="wingmpet_verification_hover_airframe_hash_mismatch"
