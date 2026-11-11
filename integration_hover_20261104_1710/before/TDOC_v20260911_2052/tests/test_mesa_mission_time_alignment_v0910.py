from tddt_tdoc.adapters.layer2 import Layer2Adapter


def _packet(ts=2.908):
    return {
        "twin_id": "MESA_NAV",
        "timestamp": ts,
        "state": {"velocity_n": 6.85, "velocity_e": 7.79, "velocity_d": 3.11},
        "provenance": {"source": "MESA_NAV.fmu"},
    }


def test_simulation_aligned_offline_mesa_uses_mission_reference_time():
    out = Layer2Adapter._bind_mesa_simulation_packet_to_mission_time(
        _packet(),
        reference_time_s=153.001,
        alignment={"applied": True, "schema": "TDOC-MESA-state-alignment-test"},
        learning_mode="OFFLINE_SE",
    )
    assert out["timestamp"] == 153.001
    rec = out["provenance"]["tdoc_mesa_mission_time_alignment"]
    assert rec["local_fmi_timestamp_s"] == 2.908
    assert rec["mission_reference_time_s"] == 153.001
    assert rec["time_authority"] == "TDOC.reference_time_s"
    assert rec["simulation_only"] is True
    assert rec["not_field_evidence"] is True


def test_simulation_aligned_online_jsbsim_mesa_uses_mission_reference_time():
    out = Layer2Adapter._bind_mesa_simulation_packet_to_mission_time(
        _packet(),
        reference_time_s=154.001,
        alignment={"applied": True},
        learning_mode="ONLINE_JSBSIM",
    )
    assert out["timestamp"] == 154.001


def test_unaligned_or_non_simulation_packet_keeps_fmi_timestamp():
    a = Layer2Adapter._bind_mesa_simulation_packet_to_mission_time(
        _packet(), reference_time_s=153.001, alignment={"applied": False}, learning_mode="OFFLINE_SE")
    b = Layer2Adapter._bind_mesa_simulation_packet_to_mission_time(
        _packet(), reference_time_s=153.001, alignment={"applied": True}, learning_mode="FIELD")
    assert a["timestamp"] == 2.908
    assert b["timestamp"] == 2.908
    assert "tdoc_mesa_mission_time_alignment" not in a["provenance"]
    assert "tdoc_mesa_mission_time_alignment" not in b["provenance"]
