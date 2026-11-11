from demo_layer6 import _ground_safe


def test_ground_safe_prefers_explicit_on_ground():
    assert _ground_safe({"mavlink_connected": True, "landed_state": "ON_GROUND"}) is True


def test_ground_safe_rejects_explicit_in_air_even_if_kinematics_small():
    assert _ground_safe({
        "mavlink_connected": True,
        "landed_state": "IN_AIR",
        "groundspeed_m_s": 0.0,
        "relative_altitude_m": 0.0,
        "vertical_speed_m_s": 0.0,
    }) is False


def test_ground_safe_kinematic_fallback_when_landed_state_unavailable():
    assert _ground_safe({
        "mavlink_connected": True,
        "landed_state": None,
        "groundspeed_m_s": 0.2,
        "relative_altitude_m": 0.3,
        "vertical_speed_m_s": -0.1,
    }) is True
