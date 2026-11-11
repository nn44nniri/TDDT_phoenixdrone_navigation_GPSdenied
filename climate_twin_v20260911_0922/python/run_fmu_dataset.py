#!/usr/bin/env python3
"""Build every grid cell and evaluate MicroMetTwin for all times and altitude levels."""
from __future__ import annotations

import argparse
import csv
import json
import hashlib
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from climate_io import build_fmi_inputs, estimate_ground_elevation_m, group_by_time, read_forecast
from fmu_client import MicroMetFMU
from climate_errors import ALTITUDE_BELOW_GROUND, error_dict
from region_geojson import load_region_geojson
from terrain_truth import TerrainTruth, find_terrain_tif

EARTH_RADIUS_M = 6371008.8


def xy_from_latlon(lat: float, lon: float, lat0: float, lon0: float) -> Tuple[float, float]:
    x = math.radians(lon - lon0) * EARTH_RADIUS_M * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * EARTH_RADIUS_M
    return x, y


def latlon_from_xy(x: float, y: float, lat0: float, lon0: float) -> Tuple[float, float]:
    lat = lat0 + math.degrees(y / EARTH_RADIUS_M)
    lon = lon0 + math.degrees(x / (EARTH_RADIUS_M * math.cos(math.radians(lat0))))
    return lat, lon


def point_in_polygon(x: float, y: float, poly: Sequence[Tuple[float, float]]) -> bool:
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        intersects = ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-15) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def parse_elevations(text: str) -> List[float]:
    vals = sorted({float(x.strip()) for x in text.split(",") if x.strip()})
    if not vals:
        raise ValueError("At least one elevation level is required")
    return vals


def build_grid(region_path: Path, resolution_m: float, elevations: Sequence[float]) -> List[dict]:
    region = load_region_geojson(region_path)
    center = region["center"]
    lat0, lon0 = float(center["latitude"]), float(center["longitude"])
    rings_xy = []
    for ring in region["rings"]:
        rings_xy.append([xy_from_latlon(lat, lon, lat0, lon0) for lon, lat in ring[:-1]])
    all_xy = [point for ring in rings_xy for point in ring]
    xs = [p[0] for p in all_xy]
    ys = [p[1] for p in all_xy]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    nx = int(math.floor((maxx - minx) / resolution_m)) + 1
    ny = int(math.floor((maxy - miny) / resolution_m)) + 1
    cells: List[dict] = []
    cell_id = 0
    for iy in range(ny + 1):
        y = miny + iy * resolution_m
        for ix in range(nx + 1):
            x = minx + ix * resolution_m
            if not any(point_in_polygon(x, y, ring) for ring in rings_xy):
                continue
            lat, lon = latlon_from_xy(x, y, lat0, lon0)
            for iz, elevation in enumerate(elevations):
                cells.append({
                    "grid_cell_id": cell_id,
                    "grid_ix": ix,
                    "grid_iy": iy,
                    "grid_iz": iz,
                    "latitude": lat,
                    "longitude": lon,
                    "elevation_m": elevation,
                })
            cell_id += 1
    if not cells:
        raise RuntimeError("Selected grid resolution produced no cells inside the GeoJSON region")
    return cells


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Create the spatial grid and call the native FMU for every cell/time/altitude."
    )
    ap.add_argument("--fmu", default="fmu/MicroMetTwin.fmu")
    ap.add_argument("--input", default="climate_dataset/example_offline_input_spatial.csv")
    ap.add_argument("--region", default="region/region_v1.geojson",
                    help="RFC 7946 GeoJSON region in WGS 84")
    ap.add_argument("--output", default="climate_dataset/micromet_grid_24h.csv")
    ap.add_argument("--grid-output", default="climate_dataset/grid_cells.csv")
    ap.add_argument("--resolution-m", type=float, default=5000.0)
    vertical = ap.add_mutually_exclusive_group()
    vertical.add_argument("--elevation-sea-level-levels",
                          help="Comma-separated absolute MSL altitude levels, e.g. 100,500,1000")
    vertical.add_argument("--elevation-ground-level-levels",
                          help="Comma-separated AGL levels, e.g. 100,500,1000")
    ap.add_argument("--dem", default=None, help="Shared terrain_obstacles.tif")
    ap.add_argument("--dem-dir", default=None, help="Directory containing shared terrain_obstacles.tif")
    ap.add_argument("--terrain-policy", choices=("prefer-dem", "require-dem"), default="require-dem")
    ap.add_argument("--curvature-radius-m", type=float, default=1000.0)
    ap.add_argument("--slope-deg", type=float, default=0.0, help="Legacy fallback when no DEM is available")
    ap.add_argument("--aspect-deg", type=float, default=0.0, help="Legacy fallback when no DEM is available")
    ap.add_argument("--curvature", type=float, default=0.0, help="Legacy fallback when no DEM is available")
    ap.add_argument("--barnes-filter-km", type=float, default=20.0)
    args = ap.parse_args()

    # Default is explicit AGL semantics; the old ambiguous --elevation-levels is removed.
    if args.elevation_sea_level_levels:
        requested_levels = parse_elevations(args.elevation_sea_level_levels)
        vertical_mode = "MSL"
    else:
        requested_levels = parse_elevations(args.elevation_ground_level_levels or "100,500,1000")
        vertical_mode = "AGL"

    forecast = read_forecast(args.input)
    by_time = group_by_time(forecast)
    first_sources = by_time[sorted(by_time)[0]]

    # Build the candidate grid from the GeoJSON region, then restrict it to the
    # actual valid terrain-raster coverage. The common terrain TIFF is the spatial
    # truth for TDDT, so points outside it (or on NoData) must never reach the FMU.
    horizontal_candidates = build_grid(Path(args.region), args.resolution_m, [0.0])
    terrain_path = find_terrain_tif(args.dem, dem_dir=args.dem_dir)
    if terrain_path is None and args.terrain_policy == "require-dem":
        raise FileNotFoundError("Shared terrain truth is required; pass --dem or --dem-dir")
    terrain_reader = TerrainTruth(terrain_path, curvature_radius_m=args.curvature_radius_m) if terrain_path else None
    terrain_samples = {}
    horizontal = []
    skipped_outside_terrain = 0
    skipped_terrain_nodata = 0
    cells = []
    try:
      for base in horizontal_candidates:
        if terrain_reader is not None:
            if not terrain_reader.contains(base["longitude"], base["latitude"]):
                skipped_outside_terrain += 1
                continue
            try:
                ts = terrain_reader.sample(base["longitude"], base["latitude"])
            except ValueError as exc:
                if "NoData" in str(exc) or "finite" in str(exc):
                    skipped_terrain_nodata += 1
                    continue
                # A point that passed contains() but is rejected as outside can occur
                # only at numerical/raster-boundary precision. Treat it as outside.
                if "outside terrain" in str(exc) or "outside terrain raster" in str(exc):
                    skipped_outside_terrain += 1
                    continue
                raise
            # Renumber valid horizontal cells contiguously while preserving ix/iy.
            base = dict(base)
            base["grid_cell_id"] = len(horizontal)
            horizontal.append(base)
            terrain_samples[base["grid_cell_id"]] = ts
            ground = ts.ground_elevation_m
            slope_deg = ts.slope_deg; aspect_deg = ts.aspect_deg; curvature = ts.curvature_scaled
        else:
            ts = None
            base = dict(base)
            base["grid_cell_id"] = len(horizontal)
            horizontal.append(base)
            ground = estimate_ground_elevation_m(
                first_sources, latitude=base["latitude"], longitude=base["longitude"],
                barnes_filter_km=args.barnes_filter_km,
            )
            slope_deg = args.slope_deg; aspect_deg = args.aspect_deg; curvature = args.curvature
        for iz, level in enumerate(requested_levels):
            if vertical_mode == "MSL":
                altitude_msl = level
                altitude_agl = altitude_msl - ground
            else:
                altitude_agl = level
                altitude_msl = ground + altitude_agl
            cells.append({
                "grid_cell_id": base["grid_cell_id"],
                "grid_ix": base["grid_ix"],
                "grid_iy": base["grid_iy"],
                "grid_iz": iz,
                "latitude": base["latitude"],
                "longitude": base["longitude"],
                "ground_elevation_m": ground,
                "slope_deg": slope_deg,
                "aspect_deg": aspect_deg,
                "curvature_scaled": curvature,
                "curvature_raw": ts.curvature_raw if ts is not None else None,
                "terrain_source": ts.source_path if ts is not None else "legacy-fallback",
                "altitude_msl_m": altitude_msl,
                "altitude_agl_m": altitude_agl,
                "vertical_reference": vertical_mode,
                # Compatibility alias for the FMU terrain/air elevation input.
                "elevation_m": altitude_msl,
            })
    finally:
      if terrain_reader is not None:
        terrain_reader.close()

    if not horizontal:
        raise RuntimeError(
            "No grid points lie inside both the GeoJSON region and valid terrain raster coverage. "
            "Check --region, --dem-dir/--dem and their CRS/extent."
        )

    if vertical_mode == "MSL":
        invalid_by_level = {}
        for level in requested_levels:
            level_cells = [c for c in cells if abs(c["altitude_msl_m"] - level) < 1e-9]
            invalid = [c for c in level_cells if c["altitude_agl_m"] < 0.0]
            if len(invalid) == len(level_cells):
                invalid_by_level[str(level)] = {
                    "affected_cells": len(invalid),
                    "ground_min_msl_m": min(c["ground_elevation_m"] for c in invalid),
                    "ground_max_msl_m": max(c["ground_elevation_m"] for c in invalid),
                }
        if invalid_by_level:
            result = error_dict(
                ALTITUDE_BELOW_GROUND,
                "One or more requested MSL layers are below local terrain for every cell; dataset generation was stopped.",
                invalid_levels=invalid_by_level,
                requested_levels_m=requested_levels,
                vertical_reference="MSL",
                suggested_action="Use --elevation-ground-level-levels or choose higher MSL levels.",
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            raise SystemExit(2)

    grid_path = Path(args.grid_output)
    grid_path.parent.mkdir(parents=True, exist_ok=True)
    with grid_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(cells[0]))
        writer.writeheader()
        writer.writerows(cells)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    writer = None
    with output_path.open("w", newline="", encoding="utf-8") as fh, MicroMetFMU(args.fmu) as fmu:
        for time_index, requested in enumerate(sorted(by_time)):
            sources = by_time[requested]
            for cell in cells:
                fmi_inputs = build_fmi_inputs(
                    sources, latitude=cell["latitude"], longitude=cell["longitude"],
                    elevation_m=cell["altitude_msl_m"], requested_time=requested,
                    slope_deg=cell["slope_deg"], aspect_deg=cell["aspect_deg"],
                    curvature=cell["curvature_scaled"], barnes_filter_km=args.barnes_filter_km,
                )
                ts = terrain_samples.get(cell["grid_cell_id"])
                if ts is not None:
                    fmi_inputs.update(ts.fmi_overrides())
                outputs = fmu.step(fmi_inputs)
                record = {**cell, "time": requested.isoformat(), "time_index": time_index,
                          "resolution_m": args.resolution_m, **outputs}
                if writer is None:
                    writer = csv.DictWriter(fh, fieldnames=list(record))
                    writer.writeheader()
                writer.writerow(record)
                count += 1

    provenance = {
        "schema": "micromet_grid_dataset_provenance_v2_geojson",
        "dataset_file": str(args.output),
        "grid_file": str(args.grid_output),
        "forecast_input": str(args.input),
        "fmu": str(args.fmu),
        "region": str(args.region),
        "region_format": "GeoJSON RFC 7946 / WGS 84",
        "region_sha256": hashlib.sha256(Path(args.region).read_bytes()).hexdigest(),
        "region_bbox_wgs84": load_region_geojson(Path(args.region))["bbox"],
        "resolution_m": float(args.resolution_m),
        "vertical_reference": vertical_mode,
        "requested_levels_m": requested_levels,
        "time_steps": len(by_time),
        "records": count,
        "terrain_source": str(terrain_path) if terrain_path else "legacy-fallback",
        "terrain_policy": args.terrain_policy,
        "horizontal_candidate_cells": len(horizontal_candidates),
        "horizontal_valid_terrain_cells": len(horizontal),
        "skipped_outside_terrain": skipped_outside_terrain,
        "skipped_terrain_nodata": skipped_terrain_nodata,
        "spatial_domain_rule": "GeoJSON region intersect valid shared terrain raster",
    }
    provenance_path = Path(str(args.output) + ".meta.json")
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    provenance_path.write_text(json.dumps(provenance, indent=2), encoding="utf-8")

    print(json.dumps({
        "grid_cells_3d": len(cells),
        "horizontal_cells": len(horizontal),
        "horizontal_candidate_cells": len(horizontal_candidates),
        "skipped_outside_terrain": skipped_outside_terrain,
        "skipped_terrain_nodata": skipped_terrain_nodata,
        "vertical_reference": vertical_mode,
        "requested_levels_m": requested_levels,
        "time_steps": len(by_time),
        "records": count,
        "grid_file": str(grid_path),
        "dataset_file": str(output_path),
        "provenance_file": str(provenance_path),
        "ground_elevation_note": "shared terrain_obstacles.tif" if terrain_path else "Barnes-weighted source-elevation proxy (legacy fallback)",
        "terrain_source": str(terrain_path) if terrain_path else "legacy-fallback",
    }, indent=2))


if __name__ == "__main__":
    main()
