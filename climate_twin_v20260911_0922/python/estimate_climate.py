#!/usr/bin/env python3
"""Estimate all MicroMet FMU outputs instantly for one UAV position and time.

Altitude semantics are explicit:
- --elevation-sea-level: absolute altitude above mean sea level (MSL).
- --elevation-ground-level: altitude above local ground (AGL).

The legacy ambiguous --elevation option is intentionally not supported.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from climate_io import (
    ForecastTimeOutOfRangeError, build_fmi_inputs, estimate_ground_elevation_m,
    read_forecast, sources_at_time,
)
from fmu_client import MicroMetFMU
from fmi_semantics import annotate_values
from climate_errors import below_ground_error, time_outside_forecast_error
from terrain_truth import TerrainTruth, TerrainSample, find_terrain_tif


def _terrain_sample(args: argparse.Namespace) -> TerrainSample | None:
    dem = find_terrain_tif(getattr(args, "dem", None), dem_dir=getattr(args, "dem_dir", None))
    policy = str(getattr(args, "terrain_policy", "prefer-dem"))
    if dem is None:
        if policy == "require-dem":
            raise FileNotFoundError(
                "Shared terrain truth is required but terrain_obstacles.tif was not found. "
                "Pass --dem /path/to/terrain_obstacles.tif or --dem-dir /path/to/region_v1_map."
            )
        return None
    with TerrainTruth(dem, curvature_radius_m=float(getattr(args, "curvature_radius_m", 1000.0))) as terrain:
        lon, lat = float(args.longitude), float(args.latitude)
        if not terrain.contains(lon, lat):
            west, south, east, north = terrain.bounds_wgs84()
            near_lon, near_lat = terrain.nearest_covered_lonlat(lon, lat)
            err = ValueError("requested point lies outside terrain raster coverage")
            err.terrain_diagnostic = {
                "terrain_source": str(dem),
                "requested_longitude": lon,
                "requested_latitude": lat,
                "terrain_bounds_wgs84": {"west": west, "south": south, "east": east, "north": north},
                "nearest_point_inside_terrain": {"longitude": near_lon, "latitude": near_lat},
            }
            raise err
        return terrain.sample(lon, lat)


def resolve_altitude(args: argparse.Namespace, sources: list[dict], terrain: TerrainSample | None = None) -> tuple[float, float, float, str]:
    """Return (MSL, AGL, ground, reference_mode).

    Precedence is shared DEM terrain -> explicit legacy ground override -> Barnes proxy.
    This preserves old callers while allowing TDDT to enforce one terrain truth.
    """
    if terrain is not None:
        ground = float(terrain.ground_elevation_m)
        ground_source = f"shared-DEM:{terrain.source_path}"
    elif getattr(args, "ground_elevation_m", None) is not None:
        ground = float(args.ground_elevation_m)
        ground_source = "user-provided legacy override"
    else:
        ground = estimate_ground_elevation_m(
            sources, latitude=args.latitude, longitude=args.longitude,
            barnes_filter_km=args.barnes_filter_km,
        )
        ground_source = "Barnes-weighted source-elevation proxy (legacy fallback)"

    if args.elevation_sea_level is not None:
        msl = float(args.elevation_sea_level)
        agl = msl - ground
        mode = "sea-level (MSL)"
    else:
        agl = float(args.elevation_ground_level)
        msl = ground + agl
        mode = "ground-level (AGL)"
    return msl, agl, ground, f"{mode}; ground={ground_source}"


def _time_bracket(rows: list[dict], requested: datetime) -> tuple[datetime, datetime, float]:
    times = sorted({r["time_dt"] for r in rows})
    if requested in times:
        idx = times.index(requested)
        if idx + 1 < len(times):
            return requested, times[idx + 1], 0.0
        if idx > 0:
            return times[idx - 1], requested, 1.0
        return requested, requested, 0.0
    lo = max(t for t in times if t < requested)
    hi = min(t for t in times if t > requested)
    alpha = (requested - lo).total_seconds() / (hi - lo).total_seconds()
    return lo, hi, alpha


def _endpoint_wind_inputs(fmu: MicroMetFMU, rows: list[dict], requested: datetime,
                          *, latitude: float, longitude: float, altitude_msl_m: float,
                          slope_deg: float, aspect_deg: float, curvature: float,
                          barnes_filter_km: float) -> dict[str, float]:
    t0, t1, alpha = _time_bracket(rows, requested)
    def one(t: datetime) -> tuple[float, float]:
        ss = sources_at_time(rows, t)
        ii = build_fmi_inputs(ss, latitude=latitude, longitude=longitude, elevation_m=altitude_msl_m,
                              requested_time=t, slope_deg=slope_deg, aspect_deg=aspect_deg,
                              curvature=curvature, barnes_filter_km=barnes_filter_km)
        ii["wind_3dw_enabled"] = 0.0
        oo = fmu.step(ii)
        return oo["wind_u_terrain"], oo["wind_v_terrain"]
    u0, v0 = one(t0)
    u1, v1 = one(t1)
    return {
        "temporal_t0_u10": u0, "temporal_t0_v10": v0,
        "temporal_t1_u10": u1, "temporal_t1_v10": v1,
        "temporal_interval_s": max(0.0, (t1 - t0).total_seconds()),
        "temporal_alpha": alpha,
    }

def _sparse_profile_inputs(fmu: MicroMetFMU, rows: list[dict], requested: datetime,
                           *, latitude: float, longitude: float, altitude_msl_m: float,
                           slope_deg: float, aspect_deg: float, curvature: float,
                           barnes_filter_km: float) -> dict[str, float]:
    """Downscale up to five optional sparse AGL wind levels at the two bracketing snapshots."""
    sample = rows[0] if rows else {}
    levels: list[tuple[float, str, str]] = []
    for key in sample:
        m = re.fullmatch(r"wind_speed_(\d+(?:\.\d+)?)m_ms", key)
        if not m:
            continue
        dkey = f"wind_direction_{m.group(1)}m_deg"
        if dkey in sample:
            levels.append((float(m.group(1)), key, dkey))
    levels = sorted(levels)[:5]
    if len(levels) < 2:
        return {"profile_level_count": 0.0}
    t0, t1, _ = _time_bracket(rows, requested)
    result: dict[str, float] = {"profile_level_count": float(len(levels))}
    for i, (agl, skey, dkey) in enumerate(levels):
        result[f"profile{i}_agl_m"] = agl
        for tag, tt in (("t0", t0), ("t1", t1)):
            ss = [dict(x) for x in sources_at_time(rows, tt)]
            for src in ss:
                src["wind_speed_10m_ms"] = float(src[skey])
                src["wind_direction_10m_deg"] = float(src[dkey])
            ii = build_fmi_inputs(ss, latitude=latitude, longitude=longitude, elevation_m=altitude_msl_m,
                                  requested_time=tt, slope_deg=slope_deg, aspect_deg=aspect_deg,
                                  curvature=curvature, barnes_filter_km=barnes_filter_km)
            ii["wind_3dw_enabled"] = 0.0
            oo = fmu.step(ii)
            result[f"profile{i}_u_{tag}"] = oo["wind_u_terrain"]
            result[f"profile{i}_v_{tag}"] = oo["wind_v_terrain"]
    return result

def estimate(args: argparse.Namespace) -> dict:
    requested = datetime.fromisoformat(args.time)
    rows = read_forecast(args.input)
    try:
        sources = sources_at_time(rows, requested)
    except ForecastTimeOutOfRangeError as exc:
        return time_outside_forecast_error(
            requested_time=exc.requested.isoformat(),
            forecast_start=exc.forecast_start.isoformat(),
            forecast_end=exc.forecast_end.isoformat(),
            input_file=str(args.input),
            affected_points=exc.affected_points,
            context="estimate_climate.py",
        )
    try:
        terrain = _terrain_sample(args)
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "error": "shared_terrain_not_found",
            "message": str(exc),
            "request": {"longitude": float(args.longitude), "latitude": float(args.latitude)},
        }
    except ValueError as exc:
        diagnostic = getattr(exc, "terrain_diagnostic", {})
        return {
            "ok": False,
            "error": "terrain_point_outside_coverage" if diagnostic else "terrain_sampling_error",
            "message": str(exc),
            "request": {"longitude": float(args.longitude), "latitude": float(args.latitude)},
            **diagnostic,
            "policy": "No coordinate clamping or terrain extrapolation is performed; use a point inside the shared terrain TIFF.",
        }
    altitude_msl_m, altitude_agl_m, ground_elevation_m, reference = resolve_altitude(args, sources, terrain)
    slope_deg = float(terrain.slope_deg) if terrain is not None else float(getattr(args, "slope_deg", 0.0))
    aspect_deg = float(terrain.aspect_deg) if terrain is not None else float(getattr(args, "aspect_deg", 0.0))
    curvature = float(terrain.curvature_scaled) if terrain is not None else float(getattr(args, "curvature", 0.0))
    if altitude_agl_m < 0.0:
        return below_ground_error(
            requested_msl_m=altitude_msl_m,
            ground_msl_m=ground_elevation_m,
            latitude=args.latitude,
            longitude=args.longitude,
            context="estimate_climate.py",
        )

    inputs = build_fmi_inputs(
        sources,
        latitude=args.latitude,
        longitude=args.longitude,
        elevation_m=altitude_msl_m,
        requested_time=requested,
        slope_deg=slope_deg,
        aspect_deg=aspect_deg,
        curvature=curvature,
        barnes_filter_km=args.barnes_filter_km,
    )
    with MicroMetFMU(args.fmu) as fmu:
        inputs.update(_endpoint_wind_inputs(
            fmu, rows, requested, latitude=args.latitude, longitude=args.longitude,
            altitude_msl_m=altitude_msl_m, slope_deg=slope_deg, aspect_deg=aspect_deg,
            curvature=curvature, barnes_filter_km=args.barnes_filter_km,
        ))
        inputs.update(_sparse_profile_inputs(
            fmu, rows, requested, latitude=args.latitude, longitude=args.longitude,
            altitude_msl_m=altitude_msl_m, slope_deg=slope_deg, aspect_deg=aspect_deg,
            curvature=curvature, barnes_filter_km=args.barnes_filter_km,
        ))
        # Keep estimate() backward-compatible with callers that construct a
        # lightweight argparse/SimpleNamespace predating the 3DW extension.
        # CLI users still receive the same explicit defaults declared in main().
        wind_3dw_dx_m = float(getattr(args, "wind_3dw_dx_m", 100.0))
        wind_3dw_dy_m = float(getattr(args, "wind_3dw_dy_m", 100.0))
        wind_3dw_dz_m = float(getattr(args, "wind_3dw_dz_m", 10.0))
        wind_profile_power = float(getattr(args, "wind_profile_power", 0.143))
        if terrain is not None:
            inputs.update(terrain.fmi_overrides())
        inputs.update({
            "wind_3dw_enabled": 1.0,
            "altitude_agl_m": altitude_agl_m,
            "fd_dx_m": wind_3dw_dx_m, "fd_dy_m": wind_3dw_dy_m, "fd_dz_m": wind_3dw_dz_m,
            "wind_profile_power": wind_profile_power,
        })
        outputs = fmu.step(inputs)
    result = {
        "request": {
            "time": requested.isoformat(),
            "latitude": args.latitude,
            "longitude": args.longitude,
            "altitude_msl_m": altitude_msl_m,
            "altitude_agl_m": altitude_agl_m,
            "ground_elevation_m": ground_elevation_m,
            "vertical_reference": reference,
            "slope_deg": slope_deg,
            "aspect_deg": aspect_deg,
            "curvature_scaled": curvature,
            "curvature_raw": float(terrain.curvature_raw) if terrain is not None else None,
            "terrain_source": terrain.source_path if terrain is not None else "legacy-fallback",
            "terrain_elevation_band": terrain.elevation_band if terrain is not None else None,
            "terrain_slope_band": terrain.slope_band if terrain is not None else None,
            "terrain_aspect_band": terrain.aspect_band if terrain is not None else None,
        },
        "source_count": len(sources),
        "fmu_outputs": outputs,
    }
    if bool(getattr(args, "with_metadata", False)):
        result["fmu_outputs_semantic"] = annotate_values(
            outputs,
            requested.isoformat(),
            formula_valid=outputs.get("formula_valid"),
            wind_3dw_valid=outputs.get("wind_3dw_valid"),
        )
    if bool(getattr(args, "tddt_layer2", False)):
        from tddt_layer2 import ClimateDomainTwin
        result["tddt_layer2_packet"] = ClimateDomainTwin().packet_from_estimate(
            result,
            context_id=str(getattr(args, "context_id", "UNSPECIFIED")),
            stage_id=str(getattr(args, "stage_id", "UNSPECIFIED")),
            operation_code=str(getattr(args, "operation_code", "climate_estimate")),
            evidence_log=getattr(args, "evidence_log", None),
        )
    return result


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Call MicroMetTwin.fmu through FMI and print all outputs as one dictionary."
    )
    ap.add_argument("--fmu", default="fmu/MicroMetTwin.fmu")
    ap.add_argument("--input", default="climate_dataset/example_offline_input_spatial.csv")
    ap.add_argument("--time", required=True, help="ISO local time inside the offline forecast range")
    ap.add_argument("--latitude", required=True, type=float)
    ap.add_argument("--longitude", required=True, type=float)
    vertical = ap.add_mutually_exclusive_group(required=True)
    vertical.add_argument("--elevation-sea-level", type=float,
                          help="UAV altitude above mean sea level (MSL), metres")
    vertical.add_argument("--elevation-ground-level", type=float,
                          help="UAV altitude above local ground (AGL), metres")
    ap.add_argument("--dem", default=None,
                    help="Shared terrain GeoTIFF, normally terrain_obstacles.tif")
    ap.add_argument("--dem-dir", default=None,
                    help="Directory containing the shared terrain_obstacles.tif")
    ap.add_argument("--terrain-policy", choices=("prefer-dem", "require-dem"), default="require-dem",
                    help="prefer-dem keeps the legacy fallback; require-dem enforces shared TDDT terrain truth")
    ap.add_argument("--curvature-radius-m", type=float, default=1000.0,
                    help="MicroMet terrain-curvature sampling radius on the shared DEM")
    ap.add_argument("--ground-elevation-m", type=float, default=None,
                    help="Legacy explicit ground override used only when no shared DEM is resolved")
    ap.add_argument("--slope-deg", type=float, default=0.0)
    ap.add_argument("--aspect-deg", type=float, default=0.0)
    ap.add_argument("--curvature", type=float, default=0.0)
    ap.add_argument("--barnes-filter-km", type=float, default=20.0)
    ap.add_argument("--wind-3dw-dx-m", type=float, default=100.0, help="3DW finite-difference east-west spacing in metres")
    ap.add_argument("--wind-3dw-dy-m", type=float, default=100.0, help="3DW finite-difference north-south spacing in metres")
    ap.add_argument("--wind-3dw-dz-m", type=float, default=10.0, help="3DW finite-difference vertical spacing in metres")
    ap.add_argument("--wind-profile-power", type=float, default=0.143, help="Fallback 10 m vertical-profile exponent when sparse levels are unavailable")
    ap.add_argument("--with-metadata", action="store_true", help="Add semantic FMI records: value, unit, frame, timestamp, source, quality, uncertainty, validity")
    ap.add_argument("--tddt-layer2", action="store_true", help="Add Dynamic N-Twin Layer-2 packet: state/U/Q/V/R/context/timestamp/provenance")
    ap.add_argument("--context-id", default="UNSPECIFIED", help="Optional CRP/context routing identifier; no Layer-3 lookup is performed here")
    ap.add_argument("--stage-id", default="UNSPECIFIED", help="Optional stage routing identifier; no SARG decision is performed here")
    ap.add_argument("--operation-code", default="climate_estimate")
    ap.add_argument("--evidence-log", default=None, help="Optional append-only JSONL Layer-2 evidence log for later Layer-7 review")
    ap.add_argument("--compact", action="store_true")
    args = ap.parse_args()
    result = estimate(args)
    print(json.dumps(result, ensure_ascii=False, indent=None if args.compact else 2, sort_keys=True))
    if result.get("ok") is False:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
