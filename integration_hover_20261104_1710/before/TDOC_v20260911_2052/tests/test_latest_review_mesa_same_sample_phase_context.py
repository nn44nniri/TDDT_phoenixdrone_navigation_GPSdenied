from tddt_tdoc.adapters.layer2 import Layer2Adapter


def test_same_plant_sample_rebinds_phase_context_without_changing_state():
    cached = {
        "context_id": "boulder-demo",
        "stage_id": "hover",
        "context": {
            "flight_phase": "hover",
            "phase_target_agl_m": 12.24,
            "rtmo_phase_token": {"phase_name": "hover", "cycle": 59},
        },
        "state": {"pn": 1.0, "pe": 2.0, "pd": -3.0},
        "timestamp": 82.92,
        "provenance": {"producer": "MESA_NAV"},
    }
    current_context = {
        "flight_phase": "vertical_takeoff",
        "rtmo_phase_token": {"phase_name": "vertical_takeoff", "cycle": 60},
    }

    rebound = Layer2Adapter._rebind_cached_online_mesa_packet(
        cached,
        context=current_context,
        context_id="boulder-demo",
        stage_id="vertical_takeoff",
    )

    assert rebound["state"] == cached["state"]
    assert rebound["timestamp"] == cached["timestamp"]
    assert rebound["stage_id"] == "vertical_takeoff"
    assert rebound["context"]["rtmo_phase_token"]["phase_name"] == "vertical_takeoff"
    assert "phase_target_agl_m" not in rebound["context"]
    assert rebound["provenance"]["tdoc_same_sample_transaction_rebind"]["state_reintegrated"] is False
    assert cached["stage_id"] == "hover"
    assert cached["context"]["phase_target_agl_m"] == 12.24
