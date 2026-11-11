from tddt_tdoc.adapters.layer3 import Layer3Adapter


def _state(**updates):
    state = {
        "released": False,
        "airspeed_m_s": 0.0,
        "air_velocity_ned_m_s": [0.0, 0.0, 0.0],
        # Canonical RunIC Vg includes configured wind; it is deliberately not used
        # as a ground-contact discriminator by the contract under test.
        "vn": 2.0,
        "ve": 1.0,
        "vd": 0.0,
    }
    state.update(updates)
    return state


def test_held_runic_wind_is_not_misclassified_as_ground_motion():
    evidence = Layer3Adapter._initial_ground_support_evidence(
        mode="ONLINE_JSBSIM", phase="vertical_takeoff", agl_m=0.24,
        agl_ready=True, physical_state=_state())
    assert evidence["asserted"] is True
    assert evidence["air_relative_speed_m_s"] == 0.0
    assert evidence["motion_speed_selected_m_s"] == 0.0


def test_released_aircraft_uses_live_ground_motion_not_wind_induced_airspeed():
    evidence = Layer3Adapter._initial_ground_support_evidence(
        mode="ONLINE_JSBSIM", phase="vertical_takeoff", agl_m=0.24,
        agl_ready=True, physical_state=_state(
            released=True, airspeed_m_s=2.235,
            vn=0.0006, ve=0.0006, vd=0.0048))
    assert evidence["asserted"] is True
    assert evidence["basis"].endswith("released_live_ground_speed")
    assert evidence["motion_speed_selected_m_s"] < 0.01


def test_ground_support_remains_fail_closed_outside_exact_bootstrap_boundary():
    cases = [
        dict(mode="OFFLINE_SE", phase="vertical_takeoff", agl_m=0.24,
             agl_ready=True, physical_state=_state()),
        dict(mode="ONLINE_JSBSIM", phase="cruise", agl_m=0.24,
             agl_ready=True, physical_state=_state()),
        dict(mode="ONLINE_JSBSIM", phase="vertical_takeoff", agl_m=1.01,
             agl_ready=True, physical_state=_state()),
        dict(mode="ONLINE_JSBSIM", phase="vertical_takeoff", agl_m=0.24,
             agl_ready=False, physical_state=_state()),
        dict(mode="ONLINE_JSBSIM", phase="vertical_takeoff", agl_m=0.24,
             agl_ready=True, physical_state=_state(released=True, vn=1.01, ve=0.0, vd=0.0)),
        dict(mode="ONLINE_JSBSIM", phase="vertical_takeoff", agl_m=0.24,
             agl_ready=True, physical_state=_state(airspeed_m_s=1.01)),
    ]
    assert all(
        Layer3Adapter._initial_ground_support_evidence(**case)["asserted"] is False
        for case in cases
    )
