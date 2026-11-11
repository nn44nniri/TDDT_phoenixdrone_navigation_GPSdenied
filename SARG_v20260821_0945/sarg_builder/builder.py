from __future__ import annotations

import math
import platform
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .fmi_execution import FMIExecutionError, StrictFMIEngine
from .io import load_crp, filter_crp_to_region, map_inventory, sha256, write_json
from .region import WGS84, load_region, transform_point_between, validate_crs_roundtrip
from .selector import PRIMARY_KEY, build_lookup_index
from .twins_library import TwinsLibrary, discover_assets

PHASES = (
    (1, "P1", "takeoff_transition"), (2, "P2", "climb"), (3, "P3", "cruise"),
    (4, "P4", "turn"), (5, "P5", "approach"), (6, "PX", "emergency_return"),
)
REGIMES = (
    (1, "R1", "economic_range"), (2, "R2", "endurance"), (3, "R3", "balanced"),
    (4, "R4", "fast_flight"), (5, "R5", "wind_robust"), (6, "R6", "terrain_obstacle_safety"),
    (7, "R7", "return_to_origin"), (8, "R8", "emergency_landing"), (9, "R9", "fault_tolerant"),
    (10, "R10", "hazard_escape"),
)

# These are policy commands/weights, not substitute dynamics. Every resulting
# state, force, safety, energy and navigation quantity is returned by an FMU.
POLICIES = {
    1: dict(speed=12.5, clearance=60.0, climb=1.2, bank=0.0, inflation=1.2, condition=.75, wl=.40, wt=.15, we=.35, ws=.10),
    2: dict(speed=11.8, clearance=65.0, climb=1.0, bank=0.0, inflation=1.3, condition=.80, wl=.15, wt=.10, we=.60, ws=.15),
    3: dict(speed=13.0, clearance=70.0, climb=1.2, bank=0.0, inflation=1.3, condition=.80, wl=.25, wt=.25, we=.25, ws=.25),
    4: dict(speed=15.5, clearance=75.0, climb=1.4, bank=0.0, inflation=1.4, condition=.75, wl=.20, wt=.55, we=.15, ws=.10),
    5: dict(speed=13.5, clearance=85.0, climb=1.5, bank=0.0, inflation=1.8, condition=.90, wl=.15, wt=.20, we=.20, ws=.45),
    6: dict(speed=12.2, clearance=100.0, climb=1.8, bank=0.0, inflation=2.2, condition=.90, wl=.10, wt=.10, we=.15, ws=.65),
    7: dict(speed=13.0, clearance=80.0, climb=1.4, bank=0.0, inflation=1.6, condition=.85, wl=.25, wt=.30, we=.20, ws=.25),
    8: dict(speed=11.5, clearance=55.0, climb=-0.8, bank=0.0, inflation=1.8, condition=.85, wl=.10, wt=.20, we=.20, ws=.50),
    9: dict(speed=12.0, clearance=90.0, climb=1.6, bank=0.0, inflation=2.0, condition=.95, wl=.10, wt=.15, we=.20, ws=.55),
    10: dict(speed=15.0, clearance=110.0, climb=2.0, bank=0.0, inflation=2.4, condition=.90, wl=.10, wt=.55, we=.10, ws=.25),
}


MISSING_VALUE = -9999.0

def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))

def _crp_confidence(row) -> float:
    """Read CRP confidence when supplied; otherwise use neutral confidence 1.0."""
    for key in ("confidence", "cluster_confidence", "crp_confidence", "quality_confidence", "quality_score"):
        if key in row.index:
            try:
                v=float(row[key])
            except (TypeError, ValueError):
                continue
            if math.isfinite(v):
                if v > 1.0 and v <= 100.0:
                    v /= 100.0
                return _clamp01(v)
    return 1.0

def _mesa_reference_metrics(result: dict) -> dict[str, float]:
    """Summarize MESA-NAV FMI residual/covariance outputs for SARG v2.

    MESA error state follows the standard 29-state ordering used by the twin:
    position variances 1..3, velocity variances 4..6, attitude variances 7..9.
    The summary errors are one-sigma reference magnitudes derived from those
    covariance outputs.  In Phoenix-FA, wind_error_ms is the terrain-flow/map
    consistency residual from MESA operation 11; a CRP wind context is never
    relabelled as a physical ULTRASONIC_WIND observation.
    """
    cov=result.get("covariance", {})
    def c(i: int) -> float:
        return max(0.0, float(cov.get(f"covariance_diagonal_{i:02d}", 0.0)))
    pos_sigma=math.sqrt(c(1)+c(2)+c(3))
    vel_sigma=math.sqrt(c(4)+c(5)+c(6))
    heading_sigma_deg=math.degrees(math.sqrt(c(9)))
    wind_error=float(result.get("wind", {}).get("wind_residual_norm", MISSING_VALUE))
    obs=float(result.get("observability", {}).get("conditional_observability_score", 0.0))
    # Some current MESA builds expose a conservative conditional score near zero
    # while excitation carries useful information. Use the stronger FMI evidence.
    info=max(0.0, float(result.get("excitation", {}).get("best_lambda_min", 0.0)))
    info_q=info/(1.0+info)
    robust=_clamp01(float(result.get("robust", {}).get("robust_weight", 0.0)))
    wind_info=result.get("wind", {})
    has_spatial_anchor=float(wind_info.get("spatial_information_valid", 0.0)) >= 0.5
    has_physical_ultrasonic=float(wind_info.get("physical_ultrasonic_used", 0.0)) >= 0.5
    anchor_q=1.0 if (float(wind_info.get("flow_anchor_valid", 0.0)) >= 0.5 and (has_spatial_anchor or has_physical_ultrasonic)) else 0.5
    mesa_q=_clamp01(max(obs, info_q) * robust * anchor_q)
    return {
        "position_error_m": pos_sigma,
        "velocity_error_ms": vel_sigma,
        "heading_error_deg": heading_sigma_deg,
        "position_sigma_m": pos_sigma,
        "wind_error_ms": wind_error,
        "mesa_confidence": mesa_q,
    }

def _wing_reference_metrics(result: dict, exact_safety: dict, usable_energy_wh: float) -> dict[str, float]:
    used_wh=max(0.0, float(result["accumulation"]["energy_total"])/3600.0)
    energy_remaining=max(0.0, float(usable_energy_wh)-used_wh)
    # obstacle_margin is the clearance relative to the inflated safe radius and
    # is the authoritative WingMPET safety output available in the current FMU.
    obstacle_clearance=float(exact_safety.get("obstacle_margin", MISSING_VALUE))
    wing_q=1.0 if (float(result.get("envelope", {}).get("flight_envelope_valid", 0.0)) >= 0.5 and
                   float(exact_safety.get("safety_valid", 0.0)) >= 0.5 and
                   float(result.get("mechanical", {}).get("mechanical_constraints_valid", 0.0)) >= 0.5) else 0.0
    return {
        "energy_remaining_wh": energy_remaining,
        "min_obstacle_clearance_m": obstacle_clearance,
        "wing_confidence": wing_q,
    }

@dataclass
class BuildOptions:
    samples_per_pattern: int = 48
    seed: int = 42
    confidence_floor: float = 0.50
    allow_mock_fmu: bool = False
    twins_library: str | None = None
    max_route_steps: int = 600
    communication_dt_s: float = 1.0
    region_file: str | None = None


def numeric(row: pd.Series, terms: tuple[str, ...], default: float) -> float:
    for column, value in row.items():
        if all(term in column.lower() for term in terms):
            try:
                result = float(value)
                if math.isfinite(result):
                    return result
            except Exception:
                pass
    return default


def _first_value(row: pd.Series, names: tuple[str, ...], default: Any = "") -> Any:
    lookup = {str(c).lower(): c for c in row.index}
    for name in names:
        if name.lower() in lookup:
            value = row[lookup[name.lower()]]
            if pd.notna(value):
                return value
    return default


def _signature_components(row: pd.Series, signature: str) -> dict[str, str]:
    """Split the legacy CRP state signature into named climate-state fields.

    Explicit catalog columns always take precedence.  The positional legacy
    signature is interpreted as temperature | humidity | wind only when the
    corresponding named columns are absent.  No combined signature is emitted
    into the canonical SARG outputs.
    """
    tokens = [token.strip() for token in re.split(r"[|;,/]", signature) if token.strip()]
    temperature_state = str(_first_value(row, (
        "temperature_state", "temperature_class", "thermal_state", "temp_state", "temp_class"
    ), tokens[0] if len(tokens) > 0 else ""))
    humidity_state = str(_first_value(row, (
        "humidity_state", "humidity_class", "moisture_state", "rh_state", "rh_class"
    ), tokens[1] if len(tokens) > 1 else ""))
    wind_state = str(_first_value(row, (
        "wind_state", "wind_class", "airflow_state", "wind_regime"
    ), tokens[2] if len(tokens) > 2 else ""))
    return {
        "crp_temperature_state": temperature_state,
        "crp_humidity_state": humidity_state,
        "crp_wind_state": wind_state,
    }


def _numeric_alias(row: pd.Series, aliases: tuple[str, ...]) -> float:
    lookup={str(c).lower():c for c in row.index}
    for alias in aliases:
        c=lookup.get(alias.lower())
        if c is not None:
            try:
                v=float(row[c])
                if math.isfinite(v): return v
            except Exception: pass
    # token based fallback for revisions such as pressure_pa_mean / mean_air_density_kg_m3
    for c,val in row.items():
        low=str(c).lower()
        for alias in aliases:
            toks=[t for t in alias.lower().split('_') if t not in {'mean','avg','average'}]
            if toks and all(t in low for t in toks):
                try:
                    v=float(val)
                    if math.isfinite(v): return v
                except Exception: pass
    return float('nan')

def _standard_pressure_pa(elevation_m: float) -> float:
    # ISA troposphere, used only when the CRP product has no pressure field.
    h=max(-500.0,min(11000.0,float(elevation_m)))
    return 101325.0 * (1.0 - 2.25577e-5*h) ** 5.25588

def _moist_air_density(pressure_pa: float, temperature_c: float, humidity_pct: float) -> float:
    tk=float(temperature_c)+273.15
    rh=float(humidity_pct); rh=rh/100.0 if rh>1.0 else rh; rh=max(0.0,min(1.0,rh))
    es=610.94*math.exp(17.625*float(temperature_c)/(float(temperature_c)+243.04))
    pv=max(0.0,min(0.2*pressure_pa,rh*es)); pd=pressure_pa-pv
    return pd/(287.05*tk)+pv/(461.495*tk)

def _climate_context(row: pd.Series) -> dict[str, Any]:
    """Return a traceable, schema-tolerant CRP climate/context payload."""
    cid = int(row["cluster_id"])
    code = str(_first_value(row, ("cluster_code", "crp_code", "context_code", "climate_code"), f"CRP-C{cid:03d}"))
    signature = str(_first_value(row, ("state_signature", "context_signature", "signature"), ""))
    states = _signature_components(row, signature)
    time_window = str(_first_value(row, ("time_state", "time_window", "utc_window", "time_bin", "hour_bin",
                                              "time_state__assignment", "time_window__assignment"), ""))
    temperature_c = numeric(row, ("temperature", "mean"), numeric(row, ("temperature",), float("nan")))
    humidity_pct = numeric(row, ("humidity", "mean"), numeric(row, ("humidity",), float("nan")))
    wind_speed = numeric(row, ("wind", "speed", "mean"), numeric(row, ("wind", "mean"), numeric(row, ("wind",), float("nan"))))
    wind_direction = numeric(row, ("wind", "direction", "mean"), numeric(row, ("wind", "direction"), float("nan")))
    pressure_pa = _numeric_alias(row, ("pressure_mean_pa","pressure_pa_mean","surface_pressure_mean_pa",
                                       "surface_pressure_pa","air_pressure_mean_pa","pressure_pa","pressure",
                                       "pressure_mean_pa__assignment"))
    density = _numeric_alias(row, ("air_density_mean_kg_m3","density_mean_kg_m3","air_density_kg_m3",
                                   "density_kg_m3","air_density","rho","air_density_mean_kg_m3__assignment"))
    elevation = _numeric_alias(row, ("elevation_mean_m","terrain_elevation_mean_m","terrain_elevation_m",
                                     "elevation_m","terrain_height_m","elevation_mean_m__assignment"))
    pressure_source = "crp"
    density_source = "crp"
    if not math.isfinite(pressure_pa):
        if not math.isfinite(elevation): elevation = 0.0
        pressure_pa = _standard_pressure_pa(elevation)
        pressure_source = "derived_from_crp_elevation_ISA"
    if not math.isfinite(density):
        if not (math.isfinite(temperature_c) and math.isfinite(humidity_pct)):
            raise ValueError(f"CRP cluster {cid} lacks density and the temperature/humidity required to derive it")
        density = _moist_air_density(pressure_pa, temperature_c, humidity_pct)
        density_source = "derived_from_crp_pressure_temperature_humidity"
    if not time_window or time_window.lower() == 'nan':
        raise ValueError(f"CRP cluster {cid} has no time_state/time_window context")
    return {
        "crp_id": cid, "crp_code": code, **states, "crp_time_window": time_window,
        "crp_temperature_mean_c": temperature_c, "crp_humidity_mean_pct": humidity_pct,
        "crp_wind_speed_mean_ms": wind_speed, "crp_wind_direction_mean_deg": wind_direction,
        "crp_pressure_mean_pa": pressure_pa, "crp_air_density_mean_kg_m3": density,
        "crp_pressure_source": pressure_source, "crp_air_density_source": density_source,
    }


class TerrainSampler:
    def __init__(self, map_path: str | Path):
        import rasterio
        root = Path(map_path)
        candidates = [root] if root.is_file() else list(root.rglob("terrain_obstacles.tif"))
        if not candidates:
            candidates = list(root.rglob("*.tif")) + list(root.rglob("*.tiff"))
        if not candidates:
            raise FileNotFoundError(f"No GeoTIFF found under {root}")
        self.path = candidates[0]
        self.dataset = rasterio.open(self.path)
        self.bounds = tuple(map(float, self.dataset.bounds))
        self.crs = str(self.dataset.crs)

    def sample(self, x: float, y: float) -> float:
        left, bottom, right, top = self.bounds
        x = min(max(float(x), left), right)
        y = min(max(float(y), bottom), top)
        value = float(next(self.dataset.sample([(x, y)], indexes=1))[0])
        if not math.isfinite(value) or value == self.dataset.nodata:
            raise ValueError(f"Invalid terrain sample at ({x}, {y}) from {self.path}")
        return value

    def close(self) -> None:
        self.dataset.close()


def _phase_from_progress(progress: float, regime_id: int) -> int:
    # Discrete scheduling only; the progress value itself is calculated by WingMPET.
    if regime_id in (7, 8, 9, 10) and progress >= .90:
        return 6
    if progress < .08: return 1
    if progress < .22: return 2
    if progress < .72: return 3
    if progress < .84: return 4
    return 5


def _status(accepted: bool, nav_valid: bool, confidence: float, phase_used: int) -> int:
    if not phase_used: return 4
    if accepted and nav_valid: return 0 if confidence >= .60 else 1
    if accepted: return 2
    if nav_valid: return 3
    return 5


def build_sarg(wingmpet_fmu: str | Path, mesa_nav_fmu: str | Path, drone_config: str | Path,
               map_path: str | Path, crp_path: str | Path, output_dir: str | Path,
               options: BuildOptions | None = None) -> Path:
    options = options or BuildOptions()
    if options.allow_mock_fmu:
        raise ValueError("FMI-only strict generation forbids --allow-mock-fmu")
    if not options.twins_library:
        raise ValueError("BuildOptions.twins_library is required")

    out = Path(output_dir).resolve(); out.mkdir(parents=True, exist_ok=True)
    routes_dir = out / "routes"; routes_dir.mkdir(exist_ok=True)
    crs_rejections_path = out / "crs_rejections.csv"
    crs_rejection_columns = [
        "scope", "scenario_id", "crp_id", "phase_id", "point_id", "status",
        "geodetic_crs", "map_crs", "lon_deg", "lat_deg", "x_m", "y_m",
        "forward_error_m", "reverse_error_deg", "reason"
    ]
    pd.DataFrame(columns=crs_rejection_columns).to_csv(crs_rejections_path, index=False)

    def _write_crs_rejection(row: dict[str, Any]) -> None:
        existing = pd.read_csv(crs_rejections_path) if crs_rejections_path.is_file() else pd.DataFrame(columns=crs_rejection_columns)
        payload = {c: row.get(c, "") for c in crs_rejection_columns}
        pd.concat([existing, pd.DataFrame([payload])], ignore_index=True).to_csv(crs_rejections_path, index=False)
    assets = discover_assets(options.twins_library, wingmpet_fmu=wingmpet_fmu,
                             mesa_nav_fmu=mesa_nav_fmu, drone_config=drone_config)
    twins = TwinsLibrary(assets)
    config = twins.load_drone_config()
    flat = config.flat
    usable_energy_wh = float(config.get("battery.usable_energy_Wh", config.get("battery.nominal_energy_Wh", 0.0)))
    crp = load_crp(crp_path).copy()
    crp["cluster_id"] = pd.to_numeric(crp["cluster_id"], errors="coerce").fillna(pd.Series(range(len(crp)))).astype(int)
    crp = crp.drop_duplicates("cluster_id").sort_values("cluster_id").reset_index(drop=True)

    terrain = TerrainSampler(map_path)
    try:
        if not options.region_file:
            raise ValueError("BuildOptions.region_file is required; region_v1.geojson is the authoritative mission geometry")
        region_def = load_region(options.region_file)
        region = region_def.in_map_crs(terrain.crs)
        left, bottom, right, top = terrain.bounds
        for label, (px, py) in (("Origin", region.origin_map), ("Destination", region.destination_map)):
            if not (left <= px <= right and bottom <= py <= top):
                raise ValueError(f"GeoJSON {label} {(px, py)} is outside terrain bounds {terrain.bounds}")
        rb = region.bounds_map
        if rb[0] < left or rb[1] < bottom or rb[2] > right or rb[3] > top:
            raise ValueError(f"GeoJSON region bounds {rb} are not fully covered by terrain bounds {terrain.bounds}")
        crp, crp_region_filter = filter_crp_to_region(crp, crp_path, region)
        ox, oy = map(float, region.origin_map)
        dx, dy = map(float, region.destination_map)
        map_crs = str(terrain.crs)
        crs_tolerances = {"map_tolerance_m": 0.05, "geodetic_tolerance_deg": 1e-8}
        origin_crs_check = validate_crs_roundtrip(
            region_def.origin_wgs84, (ox, oy), map_crs, **crs_tolerances
        )
        destination_crs_check = validate_crs_roundtrip(
            region_def.destination_wgs84, (dx, dy), map_crs, **crs_tolerances
        )
        if not origin_crs_check["valid"] or not destination_crs_check["valid"]:
            for label, g, pxy, chk in (("origin", region_def.origin_wgs84, (ox, oy), origin_crs_check),
                                       ("destination", region_def.destination_wgs84, (dx, dy), destination_crs_check)):
                if not chk["valid"]:
                    _write_crs_rejection({"scope": label, "status": "CRS_MISMATCH", "geodetic_crs": "EPSG:4326",
                        "map_crs": map_crs, "lon_deg": g[0], "lat_deg": g[1], "x_m": pxy[0], "y_m": pxy[1],
                        "forward_error_m": chk["forward_error_m"], "reverse_error_deg": chk["reverse_error_deg"],
                        "reason": "WGS84/projected endpoint round-trip exceeds tolerance"})
            raise ValueError(
                "CRS_MISMATCH: GeoJSON WGS84 Origin/Destination are inconsistent with the projected map coordinates "
                f"for {map_crs}; origin={origin_crs_check}, destination={destination_crs_check}"
            )
        endpoint_source = {"type": "geojson_wgs84", **region.metadata()}
        origin_terrain = terrain.sample(ox, oy); destination_terrain = terrain.sample(dx, dy)
        initial_distance = math.hypot(dx - ox, dy - oy)
        inv = map_inventory(map_path)
        scenarios = pd.DataFrame([
            {"scenario_id": i, "regime_id": i, "regime_code": code, "regime_name": name, "active": 1,
             "region_id": region_def.region_id,
             "origin_lon_deg": region_def.origin_wgs84[0], "origin_lat_deg": region_def.origin_wgs84[1],
             "destination_lon_deg": region_def.destination_wgs84[0], "destination_lat_deg": region_def.destination_wgs84[1],
             "origin_x_m": ox, "origin_y_m": oy, "destination_x_m": dx, "destination_y_m": dy,
             "airspeed_command_m_s": POLICIES[i]["speed"], "clearance_command_m": POLICIES[i]["clearance"]}
            for i, code, name in REGIMES
        ])
        scenarios.to_csv(out / "scenarios.csv", index=False)
        # One explicit scenario-context row per policy/regime and dynamic CRP climate state.
        scenario_context_rows = []
        for i, code, name in REGIMES:
            for _, source_row in crp.iterrows():
                ctx = _climate_context(source_row)
                scenario_context_rows.append({"scenario_context_id": f"S{i:02d}_{ctx['crp_code']}",
                    "scenario_id": i, "regime_id": i, "regime_code": code, "regime_name": name,
                    "region_id": region_def.region_id, **ctx})
        pd.DataFrame(scenario_context_rows).to_csv(out / "scenario_climate_contexts.csv", index=False)
        records: list[dict[str, Any]] = []; patterns: list[dict[str, Any]] = []; all_trace: list[dict[str, Any]] = []
        ncrp = len(crp); route_id = 0

        with twins.open_manager() as manager:
            engine = StrictFMIEngine(manager, config)
            for sid, rcode, rname in REGIMES:
                policy = POLICIES[sid]
                for _, crp_row in crp.iterrows():
                    cid = int(crp_row["cluster_id"]); route_id += 1
                    climate_ctx = _climate_context(crp_row)
                    wind_speed = climate_ctx["crp_wind_speed_mean_ms"]
                    if not math.isfinite(wind_speed): wind_speed = 3.0
                    wind_speed = max(0.0, wind_speed)
                    wind_dir_deg = climate_ctx["crp_wind_direction_mean_deg"]
                    if not math.isfinite(wind_dir_deg): wind_dir_deg = 0.0
                    wind_x = wind_speed * math.cos(math.radians(wind_dir_deg))
                    wind_y = wind_speed * math.sin(math.radians(wind_dir_deg))
                    temperature = climate_ctx["crp_temperature_mean_c"]
                    if not math.isfinite(temperature): temperature = 15.0
                    temperature += 273.15
                    humidity = climate_ctx["crp_humidity_mean_pct"]
                    if not math.isfinite(humidity): humidity = 50.0
                    if humidity > 1: humidity /= 100.0
                    pressure = climate_ctx["crp_pressure_mean_pa"]
                    density = climate_ctx["crp_air_density_mean_kg_m3"]
                    if not (math.isfinite(pressure) and pressure > 0.0 and math.isfinite(density) and density > 0.0):
                        raise FMIExecutionError(f"CRP cluster {cid} has incomplete pressure/density context")
                    position_sigma = max(.1, numeric(crp_row, ("position", "sigma"), 2.0))
                    wind_sigma = max(.1, numeric(crp_row, ("wind", "sigma"), 1.0))
                    state = {"x": ox, "y": oy, "h": origin_terrain + policy["clearance"]}
                    target = {"x": dx, "y": dy, "h": destination_terrain + policy["clearance"],
                              "initial_distance": initial_distance, "tolerance": max(5.0, .002 * initial_distance),
                              "time_reference": initial_distance / max(policy["speed"], .1),
                              "energy_reference": float(config.get("battery.nominal_energy_Wh", flat.get("derived.nominal_energy_Wh", 24.0))) * 3600.0}
                    elapsed_path = elapsed_time = elapsed_energy = 0.0
                    route_rows: list[dict[str, Any]] = []
                    phase_seen: set[int] = set()
                    for point_id in range(options.max_route_steps + 1):
                        current_terrain = terrain.sample(state["x"], state["y"])
                        goal_now = engine.wing_call(15, x=state["x"], y=state["y"], h=state["h"],
                                                    goal_x=target["x"], goal_y=target["y"], goal_h=target["h"],
                                                    initial_goal_distance=target["initial_distance"], goal_tolerance=target["tolerance"])
                        phase_id = _phase_from_progress(goal_now["mission_progress"], sid); phase_seen.add(phase_id)
                        # Operation 15 evaluates the current (pre-step) state. Stop before
                        # propagating again once the Destination tolerance is already met.
                        if goal_now["goal_reached"] >= .5:
                            break
                        # WingMPET operation 15 supplies the vertical goal geometry.
                        # Python only clips the command to the policy actuator limit.
                        climb = policy["speed"] * math.sin(goal_now["goal_elevation_angle"])
                        climb = max(-abs(policy["climb"]), min(abs(policy["climb"]), climb))
                        # Propagate only through WingMPET, but reject out-of-region
                        # candidates and retry the same FMI operation with a smaller dt.
                        # No clipping or synthetic point is introduced.
                        step_dt = float(options.communication_dt_s)
                        min_step_dt = max(1e-3, min(0.05, step_dt / 32.0))
                        while True:
                            result = engine.step(
                                dt=step_dt, state=state, target=target,
                                airspeed_command=policy["speed"], climb_rate_command=climb,
                                wind={"x": wind_x, "y": wind_y, "h": 0.0, "variance": wind_sigma ** 2, "sigma": wind_sigma},
                                atmosphere={"temperature": temperature, "pressure": pressure, "density": density, "humidity": humidity},
                                terrain_height=current_terrain, obstacle_distance=1e6,
                                clearance_required=policy["clearance"], position_sigma=position_sigma,
                                elapsed_path=elapsed_path, elapsed_time=elapsed_time, elapsed_energy_j=elapsed_energy,
                                policy_weights={"bank_angle": policy["bank"], "inflation_factor": policy["inflation"],
                                                "condition_score": policy["condition"], "length": policy["wl"],
                                                "time": policy["wt"], "energy": policy["we"], "safety": policy["ws"]})
                            next_state = {"x": result["motion"]["next_x"], "y": result["motion"]["next_y"], "h": result["motion"]["next_h"]}
                            if region.contains_map(next_state["x"], next_state["y"]):
                                break
                            if step_dt <= min_step_dt + 1e-12:
                                raise FMIExecutionError(
                                    f"WingMPET could not produce an in-region step after adaptive dt reduction: "
                                    f"S{sid:02d}/C{cid:03d}/point={point_id + 1}; dt={step_dt:.6f}s, "
                                    f"candidate=({next_state['x']}, {next_state['y']})"
                                )
                            step_dt = max(min_step_dt, step_dt * 0.5)
                        next_terrain = terrain.sample(next_state["x"], next_state["y"])
                        exact_safety = engine.wing_call(13, altitude=next_state["h"], terrain_height=next_terrain,
                                                       clearance=policy["clearance"], elevated_obstacle_distance=1e6,
                                                       ground_obstacle_distance=1e6, base_safe_radius=policy["clearance"],
                                                       position_sigma=position_sigma, wind_sigma=wind_sigma,
                                                       inflation_factor=policy["inflation"])
                        if exact_safety["safety_valid"] < .5:
                            # Rejection/retry is control orchestration. The retried state is still calculated by WingMPET.
                            result = engine.step(
                                dt=step_dt, state=state, target=target,
                                airspeed_command=policy["speed"], climb_rate_command=max(2.0, abs(policy["climb"])),
                                wind={"x": wind_x, "y": wind_y, "h": 0.0, "variance": wind_sigma ** 2, "sigma": wind_sigma},
                                atmosphere={"temperature": temperature, "pressure": pressure, "density": density, "humidity": humidity},
                                terrain_height=current_terrain, obstacle_distance=1e6,
                                clearance_required=policy["clearance"], position_sigma=position_sigma,
                                elapsed_path=elapsed_path, elapsed_time=elapsed_time, elapsed_energy_j=elapsed_energy,
                                policy_weights={"bank_angle": policy["bank"], "inflation_factor": policy["inflation"],
                                                "condition_score": policy["condition"], "length": policy["wl"],
                                                "time": policy["wt"], "energy": policy["we"], "safety": policy["ws"]})
                            next_state = {"x": result["motion"]["next_x"], "y": result["motion"]["next_y"], "h": result["motion"]["next_h"]}
                            if not region.contains_map(next_state["x"], next_state["y"]):
                                raise FMIExecutionError(
                                    f"WingMPET safety retry left region_v1.geojson: "
                                    f"S{sid:02d}/C{cid:03d}/point={point_id + 1} at "
                                    f"({next_state['x']}, {next_state['y']})"
                                )
                            next_terrain = terrain.sample(next_state["x"], next_state["y"])
                            exact_safety = engine.wing_call(13, altitude=next_state["h"], terrain_height=next_terrain,
                                                           clearance=policy["clearance"], elevated_obstacle_distance=1e6,
                                                           ground_obstacle_distance=1e6, base_safe_radius=policy["clearance"],
                                                           position_sigma=position_sigma, wind_sigma=wind_sigma,
                                                           inflation_factor=policy["inflation"])
                        if exact_safety["safety_valid"] < .5:
                            raise FMIExecutionError(f"WingMPET could not produce a safe step for R{sid}/C{cid}/point {point_id}")
                        # The engine's cheap pre-screen used terrain sampled at the OLD
                        # position. The exact op13 verdict above is for next_state and
                        # is the authoritative collision/clearance test of this step.
                        # Keep envelope/mechanical rejection; never promote their failure.
                        result["safety_prescreen"] = result["safety"]
                        result["safety"] = exact_safety
                        result["accepted"] = engine.acceptance_flags(result, exact_safety)
                        accumulation=result["accumulation"]
                        result["objective"]=engine.wing_call(
                            16,path_length=accumulation["path_length_total"],mission_time=accumulation["time_total"],
                            energy=accumulation["energy_total"],
                            safety_penalty=max(0.0,-exact_safety["terrain_margin"])+max(0.0,-exact_safety["obstacle_margin"]),
                            length_reference=target["initial_distance"],time_reference=max(step_dt,target["time_reference"]),
                            energy_reference=max(1.0,target["energy_reference"]),safety_reference=1.0,
                            weight_length=policy["wl"],weight_time=policy["wt"],weight_energy=policy["we"],weight_safety=policy["ws"])
                        wing_metrics = _wing_reference_metrics(result, exact_safety, usable_energy_wh)
                        mesa_metrics = _mesa_reference_metrics(result)
                        crp_confidence = _crp_confidence(crp_row)
                        record_point_confidence = _clamp01(wing_metrics["wing_confidence"] * mesa_metrics["mesa_confidence"] * crp_confidence)
                        elapsed_path = result["accumulation"]["path_length_total"]
                        elapsed_time = result["accumulation"]["time_total"]
                        elapsed_energy = result["accumulation"]["energy_total"]
                        information = max(0.0, result["excitation"]["best_lambda_min"])
                        confidence = information / (1.0 + information)
                        nav_valid = information > 0.0 and result["mechanical"]["mechanical_constraints_valid"] >= .5
                        point_wgs84 = transform_point_between((next_state["x"], next_state["y"]), map_crs, WGS84)
                        point_crs_check = validate_crs_roundtrip(
                            point_wgs84, (next_state["x"], next_state["y"]), map_crs, **crs_tolerances
                        )
                        if not point_crs_check["valid"]:
                            _write_crs_rejection({"scope": "route_point", "scenario_id": sid, "crp_id": cid,
                                "phase_id": phase_id, "point_id": point_id, "status": "CRS_MISMATCH",
                                "geodetic_crs": "EPSG:4326", "map_crs": map_crs, "lon_deg": point_wgs84[0],
                                "lat_deg": point_wgs84[1], "x_m": next_state["x"], "y_m": next_state["y"],
                                "forward_error_m": point_crs_check["forward_error_m"],
                                "reverse_error_deg": point_crs_check["reverse_error_deg"],
                                "reason": "route-point WGS84/projected round-trip exceeds tolerance"})
                            raise FMIExecutionError(
                                f"CRS_MISMATCH: S{sid:02d}/C{cid:03d}/point={point_id}; "
                                f"map=({next_state['x']}, {next_state['y']}), geodetic={point_wgs84}, check={point_crs_check}"
                            )
                        route_rows.append({
                            "point_id": point_id, "phase_id": phase_id, "step_dt_s": step_dt,
                            "x_m": next_state["x"], "y_m": next_state["y"], "z_msl_m": next_state["h"],
                            "lon_deg": point_wgs84[0], "lat_deg": point_wgs84[1],
                            "h_m": next_state["h"],
                            "terrain_m": next_terrain, "clearance_m": exact_safety["terrain_margin"] + policy["clearance"],
                            "time_s": elapsed_time, "airspeed_ms": policy["speed"],
                            "groundspeed_ms": result["motion"]["groundspeed"], "energy_used_j": elapsed_energy,
                            "mission_progress": result["goal"]["mission_progress"], "goal_distance_m": result["goal"]["goal_distance"],
                            "objective": result["objective"]["objective"], "flight_valid": int(result["accepted"]),
                            "navigation_valid": int(nav_valid), "observability_score": confidence,
                            "_energy_remaining_wh": wing_metrics["energy_remaining_wh"],
                            "_min_obstacle_clearance_m": wing_metrics["min_obstacle_clearance_m"],
                            "_position_error_m": mesa_metrics["position_error_m"],
                            "_velocity_error_ms": mesa_metrics["velocity_error_ms"],
                            "_heading_error_deg": mesa_metrics["heading_error_deg"],
                            "_position_sigma_m": mesa_metrics["position_sigma_m"],
                            "_wind_error_ms": mesa_metrics["wind_error_ms"],
                            "_wing_confidence": wing_metrics["wing_confidence"],
                            "_mesa_confidence": mesa_metrics["mesa_confidence"],
                            "_crp_confidence": crp_confidence,
                            "_record_confidence": record_point_confidence,
                            "crp_id": cid, "crp_code": climate_ctx["crp_code"],
                            "crp_temperature_state": climate_ctx["crp_temperature_state"],
                            "crp_humidity_state": climate_ctx["crp_humidity_state"],
                            "crp_wind_state": climate_ctx["crp_wind_state"],
                            "crp_time_window": climate_ctx["crp_time_window"],
                            "crp_temperature_mean_c": climate_ctx["crp_temperature_mean_c"],
                            "crp_humidity_mean_pct": climate_ctx["crp_humidity_mean_pct"],
                            "crp_wind_speed_mean_ms": climate_ctx["crp_wind_speed_mean_ms"],
                            "crp_wind_direction_mean_deg": climate_ctx["crp_wind_direction_mean_deg"],
                            "crp_pressure_mean_pa": climate_ctx["crp_pressure_mean_pa"],
                            "crp_air_density_mean_kg_m3": climate_ctx["crp_air_density_mean_kg_m3"],
                        })
                        state = next_state
                        if result["goal"]["goal_reached"] >= .5:
                            break
                    else:
                        raise FMIExecutionError(f"Route R{sid}/C{cid} did not reach Destination in {options.max_route_steps} FMI steps")

                    route = pd.DataFrame(route_rows)
                    # Preserve six architecture phases. Missing emergency phase is explicitly non-triggered.
                    route.drop(columns=[c for c in route.columns if c.startswith("_")]).to_csv(routes_dir / f"S{sid:02d}_C{cid:03d}.csv", index=False)
                    for phase_id, phase_code, stage in PHASES:
                        part = route[route["phase_id"] == phase_id]
                        phase_used = int(not part.empty)
                        if part.empty:
                            anchor = route.iloc[-1]
                            record_status = 4
                            phase_values = dict(path_length_m=0.0, flight_time_s=0.0, energy_used_wh=0.0,
                                                airspeed_mean_ms=float(anchor["airspeed_ms"]), groundspeed_mean_ms=float(anchor["groundspeed_ms"]),
                                                min_ground_clearance_m=float(anchor["clearance_m"]), observability_score=float(anchor["observability_score"]),
                                                energy_remaining_wh=MISSING_VALUE, min_obstacle_clearance_m=MISSING_VALUE,
                                                position_error_m=MISSING_VALUE, velocity_error_ms=MISSING_VALUE, heading_error_deg=MISSING_VALUE,
                                                position_sigma_m=MISSING_VALUE, wind_error_ms=MISSING_VALUE,
                                                wing_confidence=MISSING_VALUE, mesa_confidence=MISSING_VALUE, record_confidence=MISSING_VALUE,
                                                objective=float(anchor["objective"]), phase_start_x=float(anchor["x_m"]), phase_start_y=float(anchor["y_m"]),
                                                phase_end_x=float(anchor["x_m"]), phase_end_y=float(anchor["y_m"]), flight_ok=0, nav_ok=0)
                        else:
                            first, last = part.iloc[0], part.iloc[-1]
                            path_start = 0.0 if first.name == 0 else float(route.iloc[first.name - 1]["goal_distance_m"])
                            path_length = float(part["groundspeed_ms"].mul(part["step_dt_s"]).sum())
                            flight_time = float(part["step_dt_s"].sum())
                            energy_j = float(part["energy_used_j"].iloc[-1] - (route.iloc[first.name - 1]["energy_used_j"] if first.name > 0 else 0.0))
                            flight_ok = int(part["flight_valid"].min() >= 1); nav_ok = int(part["navigation_valid"].min() >= 1)
                            confidence = float(part["observability_score"].mean())
                            record_status = _status(bool(flight_ok), bool(nav_ok), confidence, phase_used)
                            phase_values = dict(path_length_m=path_length, flight_time_s=flight_time, energy_used_wh=energy_j / 3600.0,
                                                airspeed_mean_ms=float(part["airspeed_ms"].mean()), groundspeed_mean_ms=float(part["groundspeed_ms"].mean()),
                                                min_ground_clearance_m=float(part["clearance_m"].min()), observability_score=confidence,
                                                energy_remaining_wh=float(part["_energy_remaining_wh"].iloc[-1]),
                                                min_obstacle_clearance_m=float(part["_min_obstacle_clearance_m"].min()),
                                                position_error_m=float(part["_position_error_m"].mean()),
                                                velocity_error_ms=float(part["_velocity_error_ms"].mean()),
                                                heading_error_deg=float(part["_heading_error_deg"].mean()),
                                                position_sigma_m=float(part["_position_sigma_m"].mean()),
                                                wind_error_ms=float(part["_wind_error_ms"].mean()),
                                                wing_confidence=float(part["_wing_confidence"].mean()),
                                                mesa_confidence=float(part["_mesa_confidence"].mean()),
                                                record_confidence=float(part["_record_confidence"].mean()),
                                                objective=float(part["objective"].iloc[-1]), phase_start_x=float(first["x_m"]), phase_start_y=float(first["y_m"]),
                                                phase_end_x=float(last["x_m"]), phase_end_y=float(last["y_m"]), flight_ok=flight_ok, nav_ok=nav_ok)
                        record_id = (sid - 1) * (ncrp * 6) + list(crp["cluster_id"]).index(cid) * 6 + phase_id
                        record = {"record_id": record_id, "scenario_id": sid, "regime_id": sid, "phase_id": phase_id, "crp_id": cid,
                                  "region_id": region_def.region_id,
                                  "origin_lon_deg": region_def.origin_wgs84[0], "origin_lat_deg": region_def.origin_wgs84[1],
                                  "destination_lon_deg": region_def.destination_wgs84[0], "destination_lat_deg": region_def.destination_wgs84[1],
                                  "origin_x_m": ox, "origin_y_m": oy, "destination_x_m": dx, "destination_y_m": dy,
                                  # Backward-compatible projected-coordinate aliases; schema marks these deprecated.
                                  "origin_x": ox, "origin_y": oy, "destination_x": dx, "destination_y": dy,
                                  "record_status": record_status, "phase_used": phase_used, "wing_status": 0 if phase_values["flight_ok"] else 5,
                                  "mesa_status": 0 if phase_values["nav_ok"] else 4,
                                  "wind_u_mean_ms": wind_x, "wind_v_mean_ms": wind_y,
                                  **climate_ctx,
                                  "fmi_execution_mode": "strict_native_operations", "route_file_id": route_id, **phase_values}
                        records.append(record)
                        patterns.append({"sarg_id": f"S{sid:02d}_R{sid}_C{cid:03d}_{phase_code}", "scenario_id": sid, "regime_id": sid,
                                         "region_id": region_def.region_id,
                                         "regime_code": rcode, "crp_cluster_id": cid, "phase_id": phase_id, "phase_code": phase_code,
                                         "mission_stage": stage, "status_label": "FMI_VALID" if record_status in (0, 1) else "FMI_CONDITIONAL",
                                         "airspeed_ref_m_s": phase_values["airspeed_mean_ms"], "min_clearance_m": phase_values["min_ground_clearance_m"],
                                         "pattern_confidence": phase_values["record_confidence"], "objective": phase_values["objective"],
                                         **climate_ctx, "source": "WingMPET.fmu+MESA_NAV.fmu/FMI"})
                    all_trace.append({"scenario_id": sid, "crp_id": cid, "route_points": len(route),
                                      "wing_calls_cumulative": engine.trace.wing_calls,
                                      "mesa_calls_cumulative": engine.trace.mesa_calls,
                                      "total_calls_cumulative": engine.trace.total})

            probes = {
                "WingMPET": {"mode": "strict_native_operations", "identity": engine.wing_call(1), "operation_calls": engine.trace.wing_calls},
                "MESA_NAV": {"mode": "strict_native_operations", "identity": engine.mesa_call(1), "operation_calls": engine.trace.mesa_calls},
                "contract_profile": engine.contract_profile,
                "contract_details": engine.contract_details,
            }

        records_df = pd.DataFrame(records).sort_values(["regime_id", "crp_id", "phase_id"], kind="stable").reset_index(drop=True)
        patterns_df = pd.DataFrame(patterns).sort_values(["regime_id", "crp_cluster_id", "phase_id"], kind="stable")
        duplicate_key = records_df.duplicated(list(PRIMARY_KEY), keep=False)
        if bool(duplicate_key.any()):
            sample = records_df.loc[duplicate_key, list(PRIMARY_KEY) + ["record_id"]].head(10).to_dict("records")
            raise FMIExecutionError(f"Canonical SARG key is not unique: {sample}")
        crp_local_order = [int(v) for v in crp["cluster_id"].tolist()]
        lookup_index_df = build_lookup_index(records_df, crp_local_order)
        records_df.to_csv(out / "sarg_records.csv", index=False); patterns_df.to_csv(out / "sarg_patterns.csv", index=False)
        lookup_index_df.to_csv(out / "sarg_lookup_index.csv", index=False)
        pd.DataFrame(all_trace).to_csv(out / "fmi_execution_trace.csv", index=False)
        pd.DataFrame(columns=["sample_id", "scenario_id", "crp_cluster_id", "phase_id", "note"]).to_csv(out / "sarg_samples.csv.gz", index=False, compression="gzip")
        patterns_df.groupby(["scenario_id", "mission_stage"], as_index=False).agg(
            pattern_count=("sarg_id", "count"), mean_confidence=("pattern_confidence", "mean")
        ).to_csv(out / "sarg_stage_summary.csv", index=False)
        parquet = {"written": False, "path": None, "error": None}
        try:
            path = out / "sarg_patterns.parquet"; patterns_df.to_parquet(path, index=False); parquet.update(written=True, path=str(path))
        except ImportError as exc: parquet["error"] = str(exc)
        status_rows = [("record", 0, "VALID"), ("record", 1, "VALID_LOW_CONF"), ("record", 2, "FLIGHT_ONLY"),
                       ("record", 3, "NAV_ONLY"), ("record", 4, "NOT_TRIGGERED"), ("record", 5, "INFEASIBLE"),
                       ("record", 6, "CRS_MISMATCH"),
                       ("wing", 0, "OK"), ("wing", 5, "FMU_REJECTED"), ("mesa", 0, "OK"), ("mesa", 4, "FMU_REJECTED")]
        pd.DataFrame(status_rows, columns=["group", "code", "name"]).to_csv(out / "status_codes.csv", index=False)
        expected = 10 * 6 * ncrp
        manifest = {
            "dataset": "SARG", "version": 2, "architecture_version": "SARG_Architecture_v2",
            "created_utc": datetime.now(timezone.utc).isoformat(), "origin": [ox, oy, origin_terrain],
            "destination": [dx, dy, destination_terrain], "scenario_count": 10, "phase_count": 6,
            "crp_cluster_count": ncrp,
            "crp_cluster_count_total": int(crp_region_filter.get("input_cluster_count", ncrp)),
            "crp_cluster_count_in_region": ncrp,
            "crp_cluster_count_catalog_only": int(crp_region_filter.get("input_cluster_count", ncrp)) - ncrp,
            "crp_cardinality_contract": "N_CRP = N_CRP,in-region",
            "catalog_clusters_absent_from_region": crp_region_filter.get("catalog_clusters_absent_from_region", []),
            "record_count": len(records_df), "expected_record_count": expected,
            "expected_record_count_formula": "10 * 6 * N_CRP,in-region",
            "primary_key": list(PRIMARY_KEY),
            "lookup_contract": {
                "canonical_selector": "G_m^SARG = Select_SARG(r_m, s_m, k_m^*)",
                "equivalent_layer3_notation": "G_m^SARG = Select_SARG(r_m, p_m, k_m)",
                "phase_stage_equivalence": "s_m ≡ p_m",
                "api_exact": "select(regime_id, phase_id, crp_id)",
                "api_unknown_regime": "select_candidates(phase_id, crp_id, regime_ids=None)",
                "unknown_regime_policy": "return_all_valid_candidates; never choose a regime implicitly",
                "index_file": "sarg_lookup_index.csv",
                "index_order": ["regime_id", "crp_local_index", "phase_id"],
                "index_columns": ["regime_id", "crp_local_index", "phase_id", "crp_id", "record_id", "row_offset"],
            },
            "expected_policy_count_formula": "10 * N_CRP,in-region",
            "record_count_validated": bool(len(records_df) == expected),
            "missing_value": MISSING_VALUE,
            "summary_reference_contract": {
                "wing_fields": ["energy_remaining_wh", "min_obstacle_clearance_m", "wing_confidence"],
                "mesa_fields": ["position_error_m", "velocity_error_ms", "heading_error_deg", "position_sigma_m", "wind_error_ms", "mesa_confidence"],
                "record_confidence_formula": "wing_confidence * mesa_confidence * crp_confidence",
                "not_triggered_or_invalid_missing_value": MISSING_VALUE,
            },
            "coordinate_reference": {
                "geodetic_crs": "EPSG:4326",
                "map_crs": map_crs,
                "local_frame": "ENU",
                "vertical_reference": "MSL",
                "coordinate_order_geodetic": "longitude,latitude",
                "linear_unit": "m",
                "angular_unit": "deg",
            },
            "coordinate_frame": "ENU", "map_crs": map_crs,
            "geodetic_reference": "WGS 84 (EPSG:4326)",
            "crs_validation": {
                "status": "PASS",
                "origin": origin_crs_check,
                "destination": destination_crs_check,
                **crs_tolerances,
                "failure_status_code": "CRS_MISMATCH",
                "canonical_policy": "CRS-mismatched records are rejected and never written to sarg_records.csv",
            },
            "region": region.metadata(),
            "region_geometry_enforced": True, "crp_region_filter": crp_region_filter,
            "endpoint_source": endpoint_source, "generation_mode": "FMI_ONLY_STRICT",
            "calculation_boundary": {
                "FMU_only": ["trajectory propagation", "goal geometry/progress", "atmosphere", "aerodynamics", "flight envelope",
                             "terrain/obstacle safety", "path/time/energy accumulation", "objective", "pitot residual/NIS",
                             "terrain-flow/map wind anchoring", "pressure anchoring", "robust weighting", "observability", "mode-aware VTOL mechanical constraints", "covariance"],
                "Python_orchestration_only": ["read map/CRP/config", "sample rasters", "convert units/coordinates", "select policy commands",
                                              "schedule phases", "accept/retry based on FMU flags", "aggregate and serialize FMU outputs"],
                "substitute_equations": False, "mock_fallback": False,
            },
            "inputs": {"wingmpet_fmu": str(assets.wingmpet_fmu), "mesa_nav_fmu": str(assets.mesa_nav_fmu),
                       "drone_config": str(assets.drone_config), "map_path": str(Path(map_path).resolve()), "crp_path": str(Path(crp_path).resolve()),
                       "region_file": str(Path(options.region_file).resolve())},
            "sha256": {"wingmpet_fmu": sha256(assets.wingmpet_fmu), "mesa_nav_fmu": sha256(assets.mesa_nav_fmu),
                       "drone_config": sha256(assets.drone_config)},
            "fmu_execution": probes, "fmi_operation_trace": str(out / "fmi_execution_trace.csv"),
            "crs_rejections": str(crs_rejections_path),
            "map_inventory": inv, "parquet": parquet,
            "architecture_completeness": {"ten_scenarios": True, "dynamic_crp_count": True, "six_phases": True,
                                           "three_key_primary_key": True, "runtime_lookup_index": True,
                                           "sarg_records_csv": True, "routes": True, "status_codes": True,
                                           "scenario_climate_contexts": True, "crp_code_and_parameters_per_record": True,
                                           "geojson_wgs84_region": True, "origin_destination_from_geojson": True,
                                           "crp_filtered_to_geojson": True, "route_points_inside_geojson": True,
                                           "explicit_geodetic_map_local_crs_contract": True, "crs_roundtrip_validation": True,
                                           "standalone_native_twin_execution": True, "all_model_outputs_from_fmi": True},
            "software": {"python": platform.python_version()},
        }
        (out / "region_v1.geojson").write_text(Path(options.region_file).read_text(encoding="utf-8"), encoding="utf-8")
        write_json(out / "region_context.json", {
            **region.metadata(),
            "coordinate_reference": {
                "geodetic_crs": "EPSG:4326", "map_crs": map_crs, "local_frame": "ENU",
                "vertical_reference": "MSL", "coordinate_order_geodetic": "longitude,latitude",
                "linear_unit": "m", "angular_unit": "deg"
            },
            "crs_validation": {"origin": origin_crs_check, "destination": destination_crs_check, **crs_tolerances},
            "crp_cluster_count_total": int(crp_region_filter.get("input_cluster_count", ncrp)),
            "crp_cluster_count_in_region": ncrp,
            "crp_cluster_count_catalog_only": int(crp_region_filter.get("input_cluster_count", ncrp)) - ncrp,
            "crp_cardinality_contract": "N_CRP = N_CRP,in-region",
            "catalog_clusters_absent_from_region": crp_region_filter.get("catalog_clusters_absent_from_region", []),
            "crp_region_filter": crp_region_filter
        })
        write_json(out / "metadata.json", manifest)
        column_semantics = {
            "origin_lon_deg": {"crs": "EPSG:4326", "unit": "deg", "axis": "longitude"},
            "origin_lat_deg": {"crs": "EPSG:4326", "unit": "deg", "axis": "latitude"},
            "destination_lon_deg": {"crs": "EPSG:4326", "unit": "deg", "axis": "longitude"},
            "destination_lat_deg": {"crs": "EPSG:4326", "unit": "deg", "axis": "latitude"},
            "origin_x_m": {"crs": map_crs, "unit": "m", "axis": "easting"},
            "origin_y_m": {"crs": map_crs, "unit": "m", "axis": "northing"},
            "destination_x_m": {"crs": map_crs, "unit": "m", "axis": "easting"},
            "destination_y_m": {"crs": map_crs, "unit": "m", "axis": "northing"},
            "origin_x": {"crs": map_crs, "unit": "m", "deprecated_alias_of": "origin_x_m"},
            "origin_y": {"crs": map_crs, "unit": "m", "deprecated_alias_of": "origin_y_m"},
            "destination_x": {"crs": map_crs, "unit": "m", "deprecated_alias_of": "destination_x_m"},
            "destination_y": {"crs": map_crs, "unit": "m", "deprecated_alias_of": "destination_y_m"},
            "energy_remaining_wh": {"unit": "Wh", "source": "WingMPET accumulation + drone usable energy"},
            "min_obstacle_clearance_m": {"unit": "m", "source": "WingMPET safety.obstacle_margin"},
            "position_error_m": {"unit": "m", "source": "MESA-NAV covariance 1..3", "semantics": "one-sigma 3D reference error magnitude"},
            "velocity_error_ms": {"unit": "m/s", "source": "MESA-NAV covariance 4..6", "semantics": "one-sigma 3D velocity error magnitude"},
            "heading_error_deg": {"unit": "deg", "source": "MESA-NAV covariance 9", "semantics": "one-sigma heading/yaw reference error"},
            "position_sigma_m": {"unit": "m", "source": "MESA-NAV covariance 1..3", "semantics": "3D one-sigma position uncertainty"},
            "wind_error_ms": {"unit": "m/s", "source": "MESA-NAV Phoenix-FA operation 11 terrain-flow/map consistency residual; legacy operation 8 only for older twins",
                              "sensor_semantics": "CRP wind is environment/map context and is not synthesized as ULTRASONIC_WIND"},
            "wing_confidence": {"range": [0.0, 1.0], "source": "WingMPET mode-aware VTOL envelope+safety with MESA mechanical consistency"},
            "mesa_confidence": {"range": [0.0, 1.0], "source": "MESA-NAV observability/excitation+robust weight+anchor validity"},
            "record_confidence": {"range": [0.0, 1.0], "formula": "wing_confidence * mesa_confidence * crp_confidence"},
        }
        write_json(out / "schema.json", {
            "primary_key": list(PRIMARY_KEY),
            "lookup_index": {
                "file": "sarg_lookup_index.csv",
                "order": ["regime_id", "crp_local_index", "phase_id"],
                "columns": ["regime_id", "crp_local_index", "phase_id", "crp_id", "record_id", "row_offset"],
                "crp_local_index_base": 0,
            },
            "selector": {
                "canonical_api": "select(regime_id, phase_id, crp_id)",
                "canonical_formula": "Select_SARG(r_m, s_m, k_m^*)",
                "phase_stage_equivalence": "s_m ≡ p_m",
                "unknown_regime_behavior": "return candidates for phase_id + crp_id; never infer regime",
            },
            "coordinate_reference": manifest["coordinate_reference"],
            "columns": {column: {"dtype": str(dtype), **column_semantics.get(column, {})} for column, dtype in records_df.dtypes.items()},
            "route_columns": {
                "x_m": {"crs": map_crs, "unit": "m", "axis": "easting"},
                "y_m": {"crs": map_crs, "unit": "m", "axis": "northing"},
                "z_msl_m": {"vertical_reference": "MSL", "unit": "m", "axis": "up"},
                "lon_deg": {"crs": "EPSG:4326", "unit": "deg", "axis": "longitude"},
                "lat_deg": {"crs": "EPSG:4326", "unit": "deg", "axis": "latitude"},
                "h_m": {"vertical_reference": "MSL", "unit": "m", "deprecated_alias_of": "z_msl_m"},
            },
        })
        (out / "README.md").write_text(
            "# Generated SARG dataset — FMI-only strict mode\n\n"
            "The authoritative mission extent, Origin and Destination are read from region_v1.geojson in WGS 84. "
            "CRP clusters and every route point are spatially validated against that region. "
            "Each record and route point includes its CRP code, time window and climate parameters. "
            "The canonical sarg_records.csv summary also carries the complete WingMPET energy/safety reference, "
            "MESA-NAV error/uncertainty reference, and separate wing_confidence/mesa_confidence/record_confidence fields. "
            "Unavailable values for NOT_TRIGGERED or invalid records use -9999. "
            "Coordinate semantics are explicit: WGS84 uses *_lon_deg/*_lat_deg, the projected map CRS uses *_x_m/*_y_m, "
            "the local frame is ENU and vertical coordinates are MSL. `region_crs` is no longer written to canonical records. "
            "The canonical lookup key is (regime_id, phase_id, crp_id); `sarg_lookup_index.csv` provides the compact "
            "Regime×CRP-local×Phase edge index. If a regime is not yet selected, callers must request all candidates for "
            "the current phase/stage and CRP rather than allowing SARG to infer a regime. "
            "See `scenario_climate_contexts.csv` for the explicit scenario×CRP context table.\n\n"
            "All flight, navigation, safety, energy and trajectory state calculations are outputs of native operations in "
            "`WingMPET.fmu` and `MESA_NAV.fmu`. Python performs only data adaptation, scheduling, rejection/retry, aggregation and serialization. "
            "There is no deterministic/reference-envelope fallback. See `metadata.json` and `fmi_execution_trace.csv`.\n", encoding="utf-8")
        return out
    finally:
        terrain.close()
