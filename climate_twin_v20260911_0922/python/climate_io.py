#!/usr/bin/env python3
"""Input parsing, temporal interpolation, and FMI input assembly."""
from __future__ import annotations

import csv
import math
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple



class ForecastTimeOutOfRangeError(ValueError):
    """Raised when a requested time cannot be interpolated from the offline forecast."""

    def __init__(self, *, requested: datetime, forecast_start: datetime, forecast_end: datetime,
                 affected_points: Sequence[str]):
        self.requested = requested
        self.forecast_start = forecast_start
        self.forecast_end = forecast_end
        self.affected_points = list(affected_points)
        super().__init__(
            f"Requested time {requested.isoformat()} is outside forecast range "
            f"[{forecast_start.isoformat()}, {forecast_end.isoformat()}]"
        )


NUMERIC_FIELDS = [
    "latitude", "longitude", "elevation_m", "temperature_2m_C",
    "relative_humidity_2m_pct", "precipitation_mm", "surface_pressure_hPa",
    "wind_speed_10m_ms", "wind_direction_10m_deg",
    "shortwave_radiation_Wm2",
]


def read_forecast(path: str | Path) -> List[dict]:
    rows: List[dict] = []
    with Path(path).open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            row = dict(row)
            row["time_dt"] = datetime.fromisoformat(row["time"])
            for key in NUMERIC_FIELDS:
                if key in row and row[key] not in (None, ""):
                    row[key] = float(row[key])
            # Optional sparse vertical wind levels use wind_speed_<AGL>m_ms / wind_direction_<AGL>m_deg.
            for key, value in list(row.items()):
                if value not in (None, "") and (re.fullmatch(r"wind_speed_\d+(?:\.\d+)?m_ms", key) or re.fullmatch(r"wind_direction_\d+(?:\.\d+)?m_deg", key)):
                    row[key] = float(value)
            row.setdefault("shortwave_radiation_Wm2", -1.0)
            rows.append(row)
    if not rows:
        raise ValueError(f"No forecast records found in {path}")
    return rows


def group_by_time(rows: Sequence[dict]) -> Dict[datetime, List[dict]]:
    out: Dict[datetime, List[dict]] = defaultdict(list)
    for row in rows:
        out[row["time_dt"]].append(row)
    return dict(out)


def _angle_lerp_deg(a: float, b: float, f: float) -> float:
    delta = ((b - a + 180.0) % 360.0) - 180.0
    return (a + f * delta) % 360.0


def sources_at_time(rows: Sequence[dict], requested: datetime) -> List[dict]:
    """Return source values at requested time, linearly interpolated per point."""
    by_point: Dict[str, List[dict]] = defaultdict(list)
    for row in rows:
        key = row.get("point") or f"{row['latitude']:.8f},{row['longitude']:.8f}"
        by_point[key].append(row)
    all_times = [row["time_dt"] for row in rows]
    forecast_start = min(all_times)
    forecast_end = max(all_times)
    if requested < forecast_start or requested > forecast_end:
        raise ForecastTimeOutOfRangeError(
            requested=requested,
            forecast_start=forecast_start,
            forecast_end=forecast_end,
            affected_points=sorted(by_point.keys()),
        )

    result: List[dict] = []
    for point, seq in by_point.items():
        seq = sorted(seq, key=lambda r: r["time_dt"])
        exact = next((r for r in seq if r["time_dt"] == requested), None)
        if exact is not None:
            result.append(dict(exact))
            continue
        before = [r for r in seq if r["time_dt"] < requested]
        after = [r for r in seq if r["time_dt"] > requested]
        if not before or not after:
            raise ForecastTimeOutOfRangeError(
                requested=requested,
                forecast_start=seq[0]["time_dt"],
                forecast_end=seq[-1]["time_dt"],
                affected_points=[point],
            )
        lo, hi = before[-1], after[0]
        span = (hi["time_dt"] - lo["time_dt"]).total_seconds()
        frac = (requested - lo["time_dt"]).total_seconds() / span
        rec = {"point": point, "time": requested.isoformat(), "time_dt": requested}
        for key in ("latitude", "longitude", "elevation_m"):
            rec[key] = lo[key]
        for key in (
            "temperature_2m_C", "relative_humidity_2m_pct", "precipitation_mm",
            "surface_pressure_hPa", "wind_speed_10m_ms", "shortwave_radiation_Wm2",
        ):
            rec[key] = lo[key] + frac * (hi[key] - lo[key])
        rec["wind_direction_10m_deg"] = _angle_lerp_deg(
            lo["wind_direction_10m_deg"], hi["wind_direction_10m_deg"], frac
        )
        # Interpolate optional sparse wind-level columns without requiring them in legacy datasets.
        for key in set(lo).intersection(hi):
            if re.fullmatch(r"wind_speed_\d+(?:\.\d+)?m_ms", key):
                rec[key] = lo[key] + frac * (hi[key] - lo[key])
            elif re.fullmatch(r"wind_direction_\d+(?:\.\d+)?m_deg", key):
                rec[key] = _angle_lerp_deg(lo[key], hi[key], frac)
        result.append(rec)
    return result


def detect_sparse_wind_levels(rows: Sequence[Mapping[str, object]]) -> List[float]:
    """Return AGL levels that provide both optional speed and direction columns."""
    levels=set()
    for row in rows[: min(len(rows), 20)]:
        for key in row:
            m=re.fullmatch(r"wind_speed_(\d+(?:\.\d+)?)m_ms", str(key))
            if m and f"wind_direction_{m.group(1)}m_deg" in row:
                levels.add(float(m.group(1)))
    return sorted(levels)


def _barnes_scalar(sources: Sequence[Mapping[str, float]], key: str, latitude: float, longitude: float, radius_m: float) -> float:
    num = 0.0
    den = 0.0
    lat_rad = math.radians(latitude)
    for src in sources:
        value = float(src.get(key, -1.0))
        if value < 0.0 or not math.isfinite(value):
            continue
        dlat = math.radians(float(src["latitude"]) - latitude)
        dlon = math.radians(float(src["longitude"]) - longitude)
        x = 6371008.8 * dlon * math.cos(lat_rad)
        y = 6371008.8 * dlat
        w = math.exp(-(x*x+y*y)/max(radius_m*radius_m, 1.0))
        num += w*value
        den += w
    return num/den if den > 0.0 else -1.0


def build_fmi_inputs(
    sources: Sequence[Mapping[str, float]], *, latitude: float, longitude: float,
    elevation_m: float, requested_time: datetime, slope_deg: float = 0.0,
    aspect_deg: float = 0.0, curvature: float = 0.0,
    barnes_filter_km: float = 20.0, lapse_rate_c_per_m: float = 0.0065,
    vapor_coefficient_per_m: float = 0.00040, gamma_s: float = 0.58,
    gamma_c: float = 0.42, precipitation_chi_km: float = 0.25,
) -> Dict[str, float]:
    slope_rad = math.radians(slope_deg)
    aspect_rad = math.radians(aspect_deg)
    gradient_magnitude = math.tan(slope_rad)
    gradient_angle = 1.5 * math.pi - aspect_rad
    dzdx = gradient_magnitude * math.cos(gradient_angle)
    dzdy = gradient_magnitude * math.sin(gradient_angle)
    vals: Dict[str, float] = {
        "latitude": latitude,
        "longitude": longitude,
        "elevation": elevation_m,
        "slope_rad_input": slope_rad,
        "aspect_rad_input": aspect_rad,
        "terrain_dzdx": dzdx,
        "terrain_dzdy": dzdy,
        "derive_slope_aspect_from_gradients": 1.0,
        "barnes_second_pass_enabled": 1.0,
        "barnes_second_pass_radius_factor": 0.5,
        "curvature_scaled_input": curvature,
        "lapse_rate": lapse_rate_c_per_m,
        "vapor_coefficient": vapor_coefficient_per_m,
        "gamma_s": gamma_s,
        "gamma_c": gamma_c,
        "hour_local": requested_time.hour + requested_time.minute / 60.0 + requested_time.second / 3600.0,
        "day_of_year": requested_time.timetuple().tm_yday,
        "rh700": -1.0,
        "barnes_filter_m2": (barnes_filter_km * 1000.0) ** 2,
        "curvature_radius_m": 1000.0,
        "zW": elevation_m,
        "zE": elevation_m,
        "zS": elevation_m,
        "zN": elevation_m,
        "zSW": elevation_m,
        "zNE": elevation_m,
        "zNW": elevation_m,
        "zSE": elevation_m,
        "curvature_min": -0.5,
        "curvature_max": 0.5,
        "precipitation_chi_km": precipitation_chi_km,
        "use_dem_curvature": 0.0,
        "shortwave_reference_wm2": _barnes_scalar(sources, "shortwave_radiation_Wm2", latitude, longitude, barnes_filter_km * 1000.0),
        "shortwave_reference_blend": 0.75,
    }
    for i in range(5):
        vals[f"source{i}_enabled"] = 0.0
    for i, src in enumerate(sources[:5]):
        vals.update({
            f"source{i}_enabled": 1.0,
            f"source{i}_latitude": float(src["latitude"]),
            f"source{i}_longitude": float(src["longitude"]),
            f"source{i}_elevation": float(src["elevation_m"]),
            f"source{i}_temperature": float(src["temperature_2m_C"]),
            f"source{i}_relative_humidity": float(src["relative_humidity_2m_pct"]),
            f"source{i}_wind_speed": float(src["wind_speed_10m_ms"]),
            f"source{i}_wind_direction": float(src["wind_direction_10m_deg"]),
            f"source{i}_precipitation": float(src["precipitation_mm"]),
            f"source{i}_surface_pressure_hpa": float(src["surface_pressure_hPa"]),
        })
    return vals


def estimate_ground_elevation_m(
    sources: Sequence[Mapping[str, float]], *, latitude: float, longitude: float,
    barnes_filter_km: float = 20.0,
) -> float:
    """Estimate local ground elevation from source-point elevations.

    This is a lightweight fallback when no DEM is bundled. It uses the same
    Gaussian distance weighting family as Barnes analysis. A real DEM-derived
    terrain elevation should replace this proxy whenever available.
    """
    if not sources:
        raise ValueError("At least one source point is required to estimate ground elevation")
    radius_m = max(barnes_filter_km * 1000.0, 1.0)
    denom_scale = radius_m * radius_m
    num = 0.0
    den = 0.0
    lat_rad = math.radians(latitude)
    for src in sources:
        dlat = math.radians(float(src["latitude"]) - latitude)
        dlon = math.radians(float(src["longitude"]) - longitude)
        x = 6371008.8 * dlon * math.cos(lat_rad)
        y = 6371008.8 * dlat
        r2 = x * x + y * y
        w = math.exp(-r2 / denom_scale)
        num += w * float(src["elevation_m"])
        den += w
    if den <= 0.0:
        return float(sources[0]["elevation_m"])
    return num / den
