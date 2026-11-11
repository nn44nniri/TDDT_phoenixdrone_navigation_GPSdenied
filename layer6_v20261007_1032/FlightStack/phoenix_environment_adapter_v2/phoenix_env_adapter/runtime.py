from __future__ import annotations
import json, time
from datetime import datetime, timedelta, timezone
from .datasets import DatasetLayout
from .region import Region
from .terrain import TerrainProvider
from .climate import ClimateProvider


def _param_id_text(value):
    if isinstance(value, bytes):
        return value.split(b"\x00", 1)[0].decode("ascii", errors="ignore")
    return str(value).split("\x00", 1)[0]


def _set_param_verified(master, name, value, *, retries=3, timeout=1.0, tolerance=0.02):
    """Set one ArduPilot parameter and verify it from PARAM_VALUE.

    Returns the value echoed by ArduPilot. Raises RuntimeError if ArduPilot does
    not acknowledge the requested value after the requested number of retries.
    """
    target_system = int(getattr(master, "target_system", 0) or 1)
    target_component = int(getattr(master, "target_component", 0) or 1)
    requested = float(value)

    for attempt in range(1, int(retries) + 1):
        master.mav.param_set_send(
            target_system,
            target_component,
            name.encode("ascii"),
            requested,
            9,  # MAV_PARAM_TYPE_REAL32
        )
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            msg = master.recv_match(type="PARAM_VALUE", blocking=True, timeout=max(0.0, deadline-time.monotonic()))
            if msg is None:
                break
            if _param_id_text(getattr(msg, "param_id", "")) != name:
                continue
            echoed = float(msg.param_value)
            if abs(echoed - requested) <= max(float(tolerance), abs(requested) * 1e-4):
                return echoed
            break
    raise RuntimeError(f"ArduPilot did not apply {name}={requested} after {retries} attempts")


def run_runtime(dataset_tar, region_path, start_time, mavlink, cache_dir, update_hz=1.0,
                dry_run=False, verify_wind=True, param_retries=3, param_timeout=1.0):
    ds = DatasetLayout.from_tar(dataset_tar, cache_dir)
    meta = ds.validate()
    region = Region(region_path)
    terrain = TerrainProvider(meta["dem_path"])
    climate = ClimateProvider(ds.climate_dir)
    t0 = datetime.fromisoformat(start_time.replace("Z", "+00:00")).astimezone(timezone.utc)
    wall0 = time.monotonic()

    if not dry_run:
        try:
            from pymavlink import mavutil
        except ImportError as e:
            raise SystemExit("pymavlink is required for runtime mode: pip install -r requirements.txt") from e
        master = mavutil.mavlink_connection(mavlink)
        hb = master.wait_heartbeat(timeout=20)
        if hb is None:
            raise SystemExit(f"No ArduPilot heartbeat received on {mavlink}")
        # Be explicit: PARAM_SET must target the ArduPilot system/component that
        # actually produced the heartbeat, not a possibly-zero default target.
        master.target_system = hb.get_srcSystem() or master.target_system or 1
        master.target_component = hb.get_srcComponent() or master.target_component or 1
        print(json.dumps({
            "mavlink_connected": True,
            "endpoint": mavlink,
            "target_system": master.target_system,
            "target_component": master.target_component,
            "wind_param_verify": bool(verify_wind),
        }), flush=True)
    else:
        master = None

    lat, lon = region.start.lat, region.start.lon
    period = 1.0 / max(0.1, float(update_hz))
    last_send = 0.0

    while True:
        now = time.monotonic()
        sim_dt = t0 + timedelta(seconds=now - wall0)
        if master is not None:
            # Drain all currently queued position messages and use the newest.
            while True:
                msg = master.recv_match(type="GLOBAL_POSITION_INT", blocking=False)
                if msg is None:
                    break
                lat = msg.lat / 1e7
                lon = msg.lon / 1e7

        c = climate.sample(lat, lon, sim_dt)
        elev = terrain.elevation_msl(lat, lon)
        inside = region.contains(lat, lon)
        state = {
            "time": sim_dt.isoformat(),
            "lat": lat,
            "lon": lon,
            "terrain_msl_m": elev,
            "inside_region": inside,
            "temperature_c": c.temperature_c,
            "relative_humidity_pct": c.relative_humidity_pct,
            "wind_speed_m_s": c.wind_speed_m_s,
            "wind_direction_from_deg": c.wind_direction_from_deg,
        }

        if master is not None and now - last_send >= period:
            try:
                if verify_wind:
                    applied_spd = _set_param_verified(
                        master, "SIM_WIND_SPD", c.wind_speed_m_s,
                        retries=param_retries, timeout=param_timeout)
                    applied_dir = _set_param_verified(
                        master, "SIM_WIND_DIR", c.wind_direction_from_deg,
                        retries=param_retries, timeout=param_timeout)
                else:
                    for name, val in (("SIM_WIND_SPD", c.wind_speed_m_s),
                                      ("SIM_WIND_DIR", c.wind_direction_from_deg)):
                        master.mav.param_set_send(
                            master.target_system or 1,
                            master.target_component or 1,
                            name.encode("ascii"), float(val), 9)
                    applied_spd = None
                    applied_dir = None
                state["mavlink_wind"] = {
                    "status": "applied" if verify_wind else "sent_unverified",
                    "SIM_WIND_SPD": applied_spd,
                    "SIM_WIND_DIR": applied_dir,
                }
            except RuntimeError as e:
                state["mavlink_wind"] = {"status": "error", "error": str(e)}
                print(json.dumps(state), flush=True)
                raise SystemExit(
                    "Environment Adapter cannot verify SIM_WIND_* on ArduPilot. "
                    "Check the MAVLink endpoint and that it is bidirectional."
                ) from e
            last_send = now

        print(json.dumps(state), flush=True)
        time.sleep(period)
