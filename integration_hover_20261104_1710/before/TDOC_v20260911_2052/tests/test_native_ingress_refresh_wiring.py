"""Exercise composition and the real post-MicroMet ingress boundary.

Wire packets and canonical IMU validation are real. Small twin stand-ins only
record transport inputs; these tests make no physics or flight readiness claim.
"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from layer6.status import project_status
from layer6.telemetry_source import native_source_contract
from test_r5_01_telemetry_receivers import wire_message
from test_r5_04_gui_status import make_link
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.adapters.layer2 import Layer2Adapter
from tddt_tdoc.online_imu import canonical_imu

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("profile", ["closed_loop_sitl", None, "qualification", "CLOSED_LOOP_SITL"])
def test_real_orchestrator_constructor_wires_only_explicit_profile(tmp_path, monkeypatch, profile):
    import tddt_tdoc.orchestrator as production

    class Layer4Startup:
        def __init__(self, *args):
            pass

        def prepare_critical_path(self):
            return {"ok": True}

        def close(self):
            pass

    class Layer5Binding:
        def __init__(self, *args):
            self.binding = None

        def bind_native_mission(self, **kwargs):
            self.binding = kwargs

        def close(self):
            pass

    class Layer6Source:
        def __init__(self, *args):
            pass

        def online_runtime_ingress(self):
            return {"source": "read-only-test-boundary"}

        def online_history_ingress(self, **kwargs):
            raise AssertionError('constructor must not start or read retained telemetry')

        def _status(self):
            raise AssertionError("constructor must not acquire telemetry")

        def current_online_status(self):
            raise AssertionError("constructor must not start or acquire Native telemetry")

        def _environment_ready(self, status):
            raise AssertionError("constructor must not validate live telemetry")

        def validated_environment_status(self, status):
            raise AssertionError("constructor must not validate live telemetry")

        def close(self):
            pass

    monkeypatch.setattr(production, "Layer4Adapter", Layer4Startup)
    monkeypatch.setattr(production, "Layer5Adapter", Layer5Binding)
    monkeypatch.setattr(production, "Layer6OnlineAdapter", Layer6Source)
    cfg = TDOCConfig.from_yaml(ROOT / "configs/tdoc.yaml")
    cfg.data["runtime"]["root"] = str(tmp_path / "runtime")
    cfg.data["tdoc"]["rtmo"]["persistence"]["hot_path_sync_heavy_writes"] = True
    online = cfg.data["layers"]["layer6"]["online"]
    if profile is None:
        online.pop("execution_profile", None)
    else:
        online["execution_profile"] = profile
    with production.TDOCOrchestrator(cfg) as orchestrator:
        actual_l2 = orchestrator.adapters[2]
        actual_l6 = orchestrator.adapters[6]
        assert type(actual_l2) is Layer2Adapter
        callback = actual_l2.native_observation_source
        if profile == "closed_loop_sitl":
            assert callback.__self__ is actual_l6
            assert callback.__func__ is Layer6Source.online_runtime_ingress
            assert callback() == {"source": "read-only-test-boundary"}
            history_callback=actual_l2.native_history_observation_source
            assert history_callback.__self__ is actual_l6
            assert history_callback.__func__ is Layer6Source.online_history_ingress
            assert orchestrator.adapters[5].binding["status_source"].__self__ is actual_l6
            witness = orchestrator.adapters[5].binding["environment_validator"]
            assert witness.__self__ is actual_l6
            assert witness.__func__ is Layer6Source.validated_environment_status
        else:
            assert callback is None
            assert actual_l2.native_history_observation_source is None
            assert orchestrator.adapters[5].binding is None


def native_ingress(boot_ms, *, expired=False):
    messages = [wire_message("HEARTBEAT", changes={"autopilot": 3, "type": 1, "custom_mode": 18}),
        wire_message("ATTITUDE", changes={"time_boot_ms": boot_ms}),
        wire_message("SCALED_IMU", changes={"time_boot_ms": boot_ms, "xacc": 1000}),
        wire_message("ATTITUDE_QUATERNION", changes={"time_boot_ms": boot_ms}),
        wire_message("GLOBAL_POSITION_INT", changes={"time_boot_ms": boot_ms}),
        wire_message("VFR_HUD"), wire_message("SYS_STATUS"), wire_message("SERVO_OUTPUT_RAW")]
    projected = project_status(make_link(native_source_contract(), messages), include_imu_samples=True)
    gui = {**projected, "schema": "Layer6-GUI-status-v1", "schema_version": 1, "ok": True,
        "mavlink_connected": True, "target_system": 1, "target_component": 1,
        "monotonic_domain_id": projected["domain"], "telemetry_metadata": projected["metadata"]}
    if expired:
        # Preserve the reported age/receipt relationship while proving that
        # genuine elapsed transport/work cannot keep an old packet fresh.
        for metadata in gui["telemetry_metadata"].values():
            metadata["received_monotonic"] -= 1.
            metadata["freshness"]["evaluated_monotonic"] -= 1.
    imu = projected["imu"]
    return {"ok": True, "source": "Layer6/GUI/MAVLink", "released": True,
        "gui_state": gui, "imu_samples": projected["imu_samples"],
        "state": {"runtime_ready": 1., "imu_time_s": imu["plant_time_s"],
            "imu_frame": imu["frame"], "imu_source": imu["source"],
            **{key: imu[key] for key in ("accel_x_m_s2", "accel_y_m_s2", "accel_z_m_s2",
                "gyro_x_rad_s", "gyro_y_rad_s", "gyro_z_rad_s")}}}


def layer2_case(monkeypatch, *, fresh_boot_ms=4200, expired_refresh=False):
    cfg = TDOCConfig.from_yaml(ROOT / "configs/tdoc.yaml")
    adapter = Layer2Adapter(cfg)
    events, measurements = [], []
    old = native_ingress(4000, expired=True)
    climate_time = "2026-10-28T16:00:04"
    climate = {"timestamp": climate_time, "state": {"wind_u_3dw": 0., "wind_v_3dw": 0.,
        "diagnostic_vertical_wind": 0.}, "provenance": {"actual_query_time": climate_time}}

    class Twin:
        def __init__(self, name):
            self.name = name
            self.fmu = SimpleNamespace(set=lambda **values: measurements.append(values))

        def evaluate_packet(self, operation, **kwargs):
            events.append(self.name)
            if self.name == "mesa":
                return {"timestamp": .001, "state": {"position_n": 10., "position_e": 20.,
                    "position_d": -30., "velocity_n": 0., "velocity_e": 0., "velocity_d": 0.}}
            return {"timestamp": .001, "state": {}}

    def estimate(_args):
        events.append("climate")
        with pytest.raises(ValueError, match="Native Online IMU observations rejected"):
            canonical_imu(old)
        return {"ok": True}

    def refresh():
        events.append("refresh")
        assert events[-2] == "climate"
        return native_ingress(fresh_boot_ms, expired=expired_refresh)

    adapter.mesa, adapter.wing = Twin("mesa"), Twin("wing")
    monkeypatch.setattr(adapter, "_ensure", lambda: None)
    monkeypatch.setattr(adapter, "_climate_args", lambda request: SimpleNamespace(operation_code=1))
    adapter._estimate = estimate
    adapter.climate_twin = SimpleNamespace(packet_from_estimate=lambda *args, **kwargs: deepcopy(climate))
    adapter.contracts = SimpleNamespace(
        normalize_operation_inputs=lambda *args, **kwargs: SimpleNamespace(values={}, records=[], envelopes=[]),
        climate_output_envelopes=lambda packet: {}, output_envelopes=lambda *args, **kwargs: {},
        tdoc=SimpleNamespace(flush_health=lambda: None))
    adapter.native_observation_source = refresh
    request = {"learning_mode": "ONLINE_JSBSIM", "stage": "vertical_takeoff", "reference_time_s": 4.,
        "context": {"rtmo_data_authority": {"navigation": "MESA_STD"},
            "online_state_ingress": {"ptlib_ingress": old}}}
    return adapter, request, events, measurements, climate


def test_real_layer2_refreshes_after_climate_without_retiming_climate(monkeypatch):
    adapter, request, events, measurements, climate = layer2_case(monkeypatch)
    result = adapter.invoke(request)
    assert result["ok"] and events == ["climate", "refresh", "mesa", "wing"]
    assert request["reference_time_s"] == 4.2
    assert request["context"]["native_current_observation_refresh"] == {
        "previous_source_time_s": 4., "current_source_time_s": 4.2,
        "climate_source_timestamp": climate["timestamp"], "clock_rebased": False,
        "archived_observation_retimed": False}
    packets = result["packets"]
    assert packets["MicroMetTwin"]["timestamp"] == climate["timestamp"]
    assert packets["MicroMetTwin"]["provenance"] == climate["provenance"]
    assert packets["MESA_NAV"]["timestamp"] == packets["WingMPET"]["timestamp"] == 4.2
    assert measurements[0]["imu_ax"] == 9.80665
    ingress = result["layer3_runtime_meta"]["mesa_online_sensor_ingress"]
    assert ingress["plant_time_s"] == 4.2 and ingress["measured_simulated_imu"]
    assert ingress["gnss_used"] is False and ingress["truth_position_used"] is False


@pytest.mark.parametrize("boot_ms,expired,reason", [
    (3900, False, "refresh reversed source clock"),
    (4200, True, "Native Online IMU observations rejected"),
])
def test_refresh_cannot_repair_reversed_or_expired_observations(monkeypatch, boot_ms, expired, reason):
    adapter, request, events, measurements, _ = layer2_case(monkeypatch, fresh_boot_ms=boot_ms, expired_refresh=expired)
    with pytest.raises(ValueError, match=reason):
        adapter.invoke(request)
    assert events == ["climate", "refresh"]
    assert measurements == []
    assert request["reference_time_s"] == 4.


@pytest.mark.parametrize("mode,navigation", [("OFFLINE_SE", "MESA_STD"), ("ONLINE_JSBSIM", "OTHER")])
def test_layer2_callback_is_not_used_by_offline_or_nonmesa_inputs(monkeypatch, mode, navigation):
    adapter, request, events, _, climate = layer2_case(monkeypatch)
    request["learning_mode"] = mode
    request["context"]["rtmo_data_authority"]["navigation"] = navigation
    adapter.native_observation_source = lambda: pytest.fail("this lane has no Native IMU acquisition authority")
    result = adapter.invoke(request)
    assert result["ok"] and events == ["climate", "mesa", "wing"]
    assert request["reference_time_s"] == 4.
    assert "native_current_observation_refresh" not in request["context"]
    assert result["packets"]["MicroMetTwin"]["timestamp"] == climate["timestamp"]
