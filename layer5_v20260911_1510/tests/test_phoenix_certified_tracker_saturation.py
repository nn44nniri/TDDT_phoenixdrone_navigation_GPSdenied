from layer5.phoenix import PhoenixSpecialization


def _state(airspeed):
    # Layer4 trajectory contract: airspeed, gamma and heading are indices 9..11.
    return [0.0, 0.0, -100.0, 13.0, 0.0, 0.0,
            1.0, 0.0, 0.0, float(airspeed), 0.0, 0.0]


def test_forward_tracker_saturates_to_accepted_certificate_neighborhood():
    phoenix = object.__new__(PhoenixSpecialization)
    phoenix.runtime_throttle_request_bounds = (0.20, 0.40)
    cfg = {
        "reference_airspeed_m_s": 13.0,
        "airspeed_gain_per_m_s": 0.035,
        "throttle_min_norm": 0.05,
        "throttle_max_norm": 0.75,
    }

    high, high_record = phoenix._forward_nominal_control(
        _state(13.0), _state(16.0), cfg=cfg, trim=(0.0, 0.30)
    )
    low, low_record = phoenix._forward_nominal_control(
        _state(13.0), _state(9.0), cfg=cfg, trim=(0.0, 0.30)
    )

    assert high[2] == 0.40 and low[2] == 0.20
    assert high_record["certificate_saturation_applied"] is True
    assert low_record["certificate_saturation_applied"] is True
    assert high_record["throttle_request_bounds_norm"] == [0.20, 0.40]
