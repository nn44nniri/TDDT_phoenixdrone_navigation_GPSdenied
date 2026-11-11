#!/usr/bin/env python3
"""
Build a trans-domain Context Reference Pattern (CRP) dataset from the regional
terrain and climate rasters produced by build_regional_map.py.

The generated CRP records combine three domain groups without duplicating
indicator columns:
  Climate: temperature, RH, wind force, wind-from direction
  Terrain/infrastructure: slope, local relief, road, obstacle type,
                          artificial-obstacle height
  Time: UTC processing window

A default of 32 clusters is used (minimum 28). Clusters are data-driven using
one-hot encoded classified states and MiniBatchKMeans. Each cluster is
represented by a unique observed medoid signature, preventing duplicate CRP
rows across all domain-state columns.

Optionally, Plotly can generate one separate 3D interactive map per cluster,
showing the cluster footprint draped on the terrain.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from region_geojson import load_region_geojson, default_dataset_paths
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import transform as warp_transform, transform_geom
from rasterio.features import geometry_mask
from pyproj import Transformer
from shapely.geometry import shape as shapely_shape, box as shapely_box
from shapely.ops import transform as shapely_transform
from sklearn.cluster import MiniBatchKMeans
from sklearn.preprocessing import OneHotEncoder
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn

CONSOLE = Console()
TIME_BINS = ["00_06", "06_12", "12_18", "18_24"]
STATE_COLUMNS = [
    "temperature_state",
    "humidity_state",
    "wind_force_state",
    "wind_direction_state",
    "slope_state",
    "local_relief_state",
    "road_state",
    "obstacle_type_state",
    "artificial_height_state",
    "magnetic_anomaly_state",
    "magnetic_gradient_state",
    "time_state",
    "critical_level",
]

RUNTIME_FEATURE_ORDER = [
    "temperature_c",
    "relative_humidity_percent",
    "wind_speed_ms",
    "wind_direction_sin",
    "wind_direction_cos",
    "terrain_elevation_m",
    "terrain_slope_deg",
    "local_relief_m",
    "building_height_m",
    "magnetic_anomaly_nt",
    "magnetic_gradient_nt_per_m",
]


def runtime_feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Continuous CRP feature vector used for runtime Mahalanobis-like matching.

    Wind direction is represented by sine/cosine components so the 0/360 degree
    wrap does not create an artificial discontinuity. Spatial row/column/x/y
    coordinates are intentionally excluded: CRP statistics describe context,
    while spatial membership remains in crp_cell_assignments/crp_cluster_map.
    """
    direction_rad = np.deg2rad(df["wind_direction_from_deg"].to_numpy(np.float64) % 360.0)
    return pd.DataFrame({
        "temperature_c": df["temperature_c"].to_numpy(np.float64),
        "relative_humidity_percent": df["relative_humidity_percent"].to_numpy(np.float64),
        "wind_speed_ms": df["wind_speed_ms"].to_numpy(np.float64),
        "wind_direction_sin": np.sin(direction_rad),
        "wind_direction_cos": np.cos(direction_rad),
        "terrain_elevation_m": df["terrain_elevation_m"].to_numpy(np.float64),
        "terrain_slope_deg": df["terrain_slope_deg"].to_numpy(np.float64),
        "local_relief_m": df["local_relief_m"].to_numpy(np.float64),
        "building_height_m": df["building_height_m"].to_numpy(np.float64),
        "magnetic_anomaly_nt": df["magnetic_anomaly_nt"].to_numpy(np.float64),
        "magnetic_gradient_nt_per_m": df["magnetic_gradient_nt_per_m"].to_numpy(np.float64),
    }, index=df.index)


def build_runtime_statistics(df: pd.DataFrame, catalog: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    """Build canonical C_k=(mu_k, Sigma_k, n_k, q_k) statistics for every CRP.

    Sigma_k is the empirical sample covariance computed from records assigned to
    the cluster. A lightly diagonal-regularized copy is also stored for consumers
    that require stable inversion. q_k is a deterministic [0,1] build-time score
    combining categorical compactness, quantitative separation and sample support.
    """
    features = runtime_feature_frame(df)
    x_all = features.to_numpy(np.float64)
    global_std = np.nanstd(x_all, axis=0, ddof=1)
    global_std = np.where(np.isfinite(global_std) & (global_std > 1e-12), global_std, 1.0)

    cluster_ids = sorted(int(x) for x in catalog["cluster_id"].unique())
    centroids = {}
    radii = {}
    empirical_covariances = {}
    regularized_covariances = {}
    sample_counts = {}
    compactness_scores = {}

    catalog_by_id = catalog.set_index("cluster_id")
    for cid in cluster_ids:
        mask = df["cluster_id"].to_numpy() == cid
        sub = features.loc[mask]
        x = sub.to_numpy(np.float64)
        n = int(x.shape[0])
        if n == 0:
            raise RuntimeError(f"CRP cluster {cid} has no assigned records.")
        mu = np.nanmean(x, axis=0)
        if n >= 2:
            cov = np.cov(x, rowvar=False, ddof=1)
        else:
            cov = np.zeros((x.shape[1], x.shape[1]), dtype=np.float64)
        cov = np.atleast_2d(np.asarray(cov, dtype=np.float64))
        cov = np.nan_to_num(cov, nan=0.0, posinf=0.0, neginf=0.0)
        diag_scale = float(np.nanmedian(np.diag(cov)[np.diag(cov) > 0])) if np.any(np.diag(cov) > 0) else 1.0
        epsilon = max(diag_scale * 1e-6, 1e-12)
        cov_reg = cov + np.eye(cov.shape[0], dtype=np.float64) * epsilon

        z = (x - mu) / global_std
        radius = float(np.sqrt(np.nanmean(np.sum(z * z, axis=1)))) if n else 0.0

        canonical_states = catalog_by_id.loc[cid, STATE_COLUMNS].astype(str)
        member_states = df.loc[mask, STATE_COLUMNS].astype(str)
        match_fraction = (member_states.to_numpy() == canonical_states.to_numpy()[None, :]).mean(axis=1)
        compactness = float(np.mean(match_fraction)) if len(match_fraction) else 0.0

        centroids[cid] = mu
        radii[cid] = radius
        empirical_covariances[cid] = cov
        regularized_covariances[cid] = (cov_reg, epsilon)
        sample_counts[cid] = n
        compactness_scores[cid] = compactness

    # Separation is measured between quantitative centroids in globally standardized space.
    standardized_centroids = {cid: centroids[cid] / global_std for cid in cluster_ids}
    median_n = float(np.median([sample_counts[cid] for cid in cluster_ids])) or 1.0
    stats_clusters = []
    quality_by_id = {}
    for cid in cluster_ids:
        other_distances = [
            float(np.linalg.norm(standardized_centroids[cid] - standardized_centroids[oid]))
            for oid in cluster_ids if oid != cid
        ]
        nearest = min(other_distances) if other_distances else 0.0
        radius = radii[cid]
        separation = float(nearest / (nearest + 2.0 * radius + 1e-12)) if nearest > 0 else 0.0
        support = float(min(1.0, sample_counts[cid] / median_n))
        compactness = compactness_scores[cid]
        quality = float(np.clip((compactness * max(separation, 1e-12) * max(support, 1e-12)) ** (1.0 / 3.0), 0.0, 1.0))
        quality_by_id[cid] = quality
        cov_reg, epsilon = regularized_covariances[cid]
        stats_clusters.append({
            "cluster_id": cid,
            "cluster_code": f"CRP_{cid:02d}",
            "centroid": [float(v) for v in centroids[cid]],
            "covariance": [[float(v) for v in row] for row in empirical_covariances[cid]],
            "covariance_regularized": [[float(v) for v in row] for row in cov_reg],
            "covariance_regularization_epsilon": float(epsilon),
            "sample_count": int(sample_counts[cid]),
            "quality_score": quality,
            "quality_components": {
                "categorical_compactness": compactness,
                "quantitative_separation": separation,
                "sample_support": support,
                "nearest_centroid_distance_standardized": nearest,
                "within_cluster_rms_radius_standardized": radius,
            },
        })

    runtime = {
        "schema": "CRP-runtime-statistics-v1",
        "canonical_cluster_definition": "C_k=(mu_k,Sigma_k,n_k,q_k)",
        "feature_order": list(RUNTIME_FEATURE_ORDER),
        "feature_notes": {
            "wind_direction_sin": "sin(wind_direction_from_deg); circular representation",
            "wind_direction_cos": "cos(wind_direction_from_deg); circular representation",
            "spatial_coordinates": "row/col/x/y intentionally excluded from Mahalanobis context vector",
        },
        "covariance_definition": "Empirical sample covariance (ddof=1) over actual crp_cell_assignments records in feature_order; zero matrix when n_k=1.",
        "regularized_covariance_definition": "covariance + epsilon*I, retained as a runtime-safe optional matrix for inversion; canonical covariance remains empirical.",
        "quality_score_definition": "Geometric mean of categorical_compactness, quantitative_separation, and sample_support; bounded to [0,1].",
        "quality_components_definition": {
            "categorical_compactness": "Mean fraction of STATE_COLUMNS matching the cluster canonical medoid signature.",
            "quantitative_separation": "nearest_centroid_distance/(nearest_centroid_distance + 2*within_cluster_rms_radius) in globally standardized runtime feature space.",
            "sample_support": "min(1, n_k/median_cluster_sample_count).",
        },
        "median_cluster_sample_count": median_n,
        "global_feature_std": [float(v) for v in global_std],
        "clusters": stats_clusters,
    }
    additions = pd.DataFrame({
        "cluster_id": cluster_ids,
        "sample_count": [sample_counts[cid] for cid in cluster_ids],
        "quality_score": [quality_by_id[cid] for cid in cluster_ids],
    })
    return runtime, additions


def progress() -> Progress:
    return Progress(
        SpinnerColumn(), TextColumn("[bold blue]{task.description}"), BarColumn(),
        TaskProgressColumn(), TimeElapsedColumn(), console=CONSOLE
    )


def band_index(ds: rasterio.io.DatasetReader, name: str) -> int:
    mapping = {(d or f"band_{i}"): i for i, d in enumerate(ds.descriptions, 1)}
    if name not in mapping:
        raise KeyError(f"Required band '{name}' is missing from {ds.name}")
    return mapping[name]


def read_band(ds, name: str) -> np.ndarray:
    a = ds.read(band_index(ds, name)).astype(np.float32)
    if ds.nodata is not None:
        a[np.isclose(a, ds.nodata)] = np.nan
    return a


def classify_temperature(x: np.ndarray) -> np.ndarray:
    labels = np.full(x.shape, "UNKNOWN", dtype=object)
    labels[x < 0] = "T0_FREEZING"
    labels[(x >= 0) & (x < 5)] = "T1_VERY_COLD"
    labels[(x >= 5) & (x < 10)] = "T2_COLD"
    labels[(x >= 10) & (x < 25)] = "T3_MILD"
    labels[(x >= 25) & (x < 30)] = "T4_WARM"
    labels[(x >= 30) & (x < 35)] = "T5_VERY_WARM"
    labels[x >= 35] = "T6_EXTREME_HEAT"
    return labels


def classify_humidity(x: np.ndarray) -> np.ndarray:
    labels = np.full(x.shape, "UNKNOWN", dtype=object)
    labels[x < 30] = "RH0_VERY_LOW"
    labels[(x >= 30) & (x < 50)] = "RH1_LOW"
    labels[(x >= 50) & (x < 70)] = "RH2_MODERATE"
    labels[(x >= 70) & (x < 85)] = "RH3_HIGH"
    labels[x >= 85] = "RH4_NEAR_SATURATION"
    return labels


def classify_wind_force(x: np.ndarray) -> np.ndarray:
    # Compact CRP grouping traceable to Beaufort 0, 1-2, 3-4, 5-6, 7-9, 10-12.
    labels = np.full(x.shape, "UNKNOWN", dtype=object)
    labels[x < 0.5] = "W0_CALM"
    labels[(x >= 0.5) & (x < 3.4)] = "W1_LIGHT"
    labels[(x >= 3.4) & (x < 8.0)] = "W2_MODERATE"
    labels[(x >= 8.0) & (x < 13.9)] = "W3_STRONG"
    labels[(x >= 13.9) & (x < 24.5)] = "W4_VERY_STRONG"
    labels[x >= 24.5] = "W5_STORM"
    return labels


def classify_wind_direction(direction: np.ndarray, speed: np.ndarray) -> np.ndarray:
    labels = np.full(direction.shape, "UNKNOWN", dtype=object)
    calm = speed < 0.5
    labels[calm] = "CALM"
    valid = (~calm) & np.isfinite(direction)
    idx = (np.floor(((direction[valid] % 360.0) + 22.5) / 45.0).astype(int) % 8)
    compass = np.array(["N", "NE", "E", "SE", "S", "SW", "W", "NW"], dtype=object)
    labels[valid] = compass[idx]
    return labels


def classify_slope(x: np.ndarray) -> np.ndarray:
    labels = np.full(x.shape, "UNKNOWN", dtype=object)
    labels[x < 7] = "S0_GENTLE"
    labels[(x >= 7) & (x < 15)] = "S1_MODERATE"
    labels[(x >= 15) & (x < 20)] = "S2_HIGH"
    labels[(x >= 20) & (x < 25)] = "S3_STEEP"
    labels[(x >= 25) & (x < 30)] = "S4_VERY_STEEP"
    labels[(x >= 30) & (x < 40)] = "S5_SEVERE"
    labels[x >= 40] = "S6_EXTREME"
    return labels


def classify_relief(x: np.ndarray) -> np.ndarray:
    labels = np.full(x.shape, "UNKNOWN", dtype=object)
    labels[x < 100] = "R0_LOW"
    labels[(x >= 100) & (x < 300)] = "R1_MODERATE"
    labels[x >= 300] = "R2_HIGH"
    return labels


def classify_road(x: np.ndarray) -> np.ndarray:
    # Raster road codes from build_regional_map.py.
    out = np.full(x.shape, "ROAD0_NONE", dtype=object)
    out[x == 1] = "ROAD1_LIGHT_PATH"
    out[x == 2] = "ROAD2_SERVICE"
    out[x == 3] = "ROAD3_LOCAL"
    out[x == 4] = "ROAD4_COLLECTOR"
    out[np.isin(x, [5, 6])] = "ROAD5_MAIN"
    out[np.isin(x, [7, 8])] = "ROAD6_HIGHWAY"
    return out


def classify_obstacle(natural: np.ndarray, artificial: np.ndarray, building: np.ndarray) -> np.ndarray:
    out = np.full(natural.shape, "O0_NONE", dtype=object)
    # Natural codes: 1 water, 2 wetland, 3 woodland, 4 scrub, 5 cliff, 6 scree, 7 bare rock.
    out[np.isin(natural, [1, 2])] = "O1_WATER_WETLAND"
    out[natural == 3] = "O2_WOOD_FOREST"
    out[natural == 4] = "O3_SCRUB"
    out[np.isin(natural, [6, 7])] = "O4_ROCK_SCREE"
    out[natural == 5] = "O5_CLIFF"
    out[building > 0] = "O6_BUILDING"
    # Artificial codes: 1 tower, 2 mast, 3 chimney, 4 water tower, 5 tank, 6 power tower, 7 pole, 8 line.
    out[np.isin(artificial, [1, 2, 3, 4, 5])] = "O7_TOWER_MAST_CHIMNEY"
    out[np.isin(artificial, [6, 7, 8])] = "O8_POWER_INFRASTRUCTURE"
    return out


def classify_artificial_height(building_height: np.ndarray, artificial: np.ndarray) -> np.ndarray:
    # For non-building point/line artificial obstacles without mapped height, height is unknown.
    out = np.full(building_height.shape, "H0_NONE", dtype=object)
    has_art = artificial > 0
    out[has_art] = "H1_UNKNOWN_PRESENT"
    h = building_height
    out[(h > 0) & (h < 15)] = "H2_SHORT"
    out[(h >= 15) & (h < 30)] = "H3_MEDIUM"
    out[(h >= 30) & (h < 61)] = "H4_TALL"
    out[(h >= 61) & (h < 152)] = "H5_AERONAUTICAL_SIGNIFICANT"
    out[h >= 152] = "H6_VERY_TALL"
    return out



def classify_magnetic_anomaly(x: np.ndarray) -> np.ndarray:
    """Operational fixed bins in nanoTesla; preserves polarity and anomaly magnitude."""
    labels = np.full(x.shape, "MAG_UNKNOWN", dtype=object)
    labels[x < -200] = "MAG0_STRONG_NEGATIVE"
    labels[(x >= -200) & (x < -50)] = "MAG1_NEGATIVE"
    labels[(x >= -50) & (x <= 50)] = "MAG2_NEUTRAL"
    labels[(x > 50) & (x <= 200)] = "MAG3_POSITIVE"
    labels[x > 200] = "MAG4_STRONG_POSITIVE"
    return labels


def classify_magnetic_gradient(x: np.ndarray) -> np.ndarray:
    """Horizontal anomaly-gradient classes in nT per metre."""
    labels = np.full(x.shape, "MGRAD_UNKNOWN", dtype=object)
    labels[x < 0.002] = "MGRAD0_LOW"
    labels[(x >= 0.002) & (x < 0.01)] = "MGRAD1_MODERATE"
    labels[(x >= 0.01) & (x < 0.05)] = "MGRAD2_HIGH"
    labels[x >= 0.05] = "MGRAD3_EXTREME"
    return labels


def critical_level(temp, rh, wind, slope, relief, obstacle, height, magnetic, magnetic_gradient) -> np.ndarray:
    """Project CRP composite level, not a universal standard hazard level."""
    major = np.zeros(temp.shape, dtype=np.int16)
    minor = np.zeros(temp.shape, dtype=np.int16)

    major += np.isin(temp, ["T0_FREEZING", "T6_EXTREME_HEAT"])
    minor += np.isin(temp, ["T1_VERY_COLD", "T5_VERY_WARM"])
    major += np.isin(wind, ["W4_VERY_STRONG", "W5_STORM"])
    minor += np.isin(wind, ["W3_STRONG"])
    major += np.isin(slope, ["S5_SEVERE", "S6_EXTREME"])
    minor += np.isin(slope, ["S3_STEEP", "S4_VERY_STEEP"])
    major += np.isin(relief, ["R2_HIGH"])
    minor += np.isin(relief, ["R1_MODERATE"])
    major += np.isin(obstacle, ["O5_CLIFF", "O7_TOWER_MAST_CHIMNEY", "O8_POWER_INFRASTRUCTURE"])
    minor += np.isin(obstacle, ["O1_WATER_WETLAND", "O2_WOOD_FOREST", "O4_ROCK_SCREE", "O6_BUILDING"])
    major += np.isin(height, ["H5_AERONAUTICAL_SIGNIFICANT", "H6_VERY_TALL"])
    minor += np.isin(height, ["H3_MEDIUM", "H4_TALL", "H1_UNKNOWN_PRESENT"])
    minor += np.isin(rh, ["RH0_VERY_LOW", "RH4_NEAR_SATURATION"])
    major += np.isin(magnetic_gradient, ["MGRAD3_EXTREME"])
    minor += np.isin(magnetic_gradient, ["MGRAD2_HIGH"])
    minor += np.isin(magnetic, ["MAG0_STRONG_NEGATIVE", "MAG4_STRONG_POSITIVE"])

    out = np.full(temp.shape, "L0_NO_FLAG", dtype=object)
    out[(major == 0) & (minor >= 1)] = "L1_ATTENTION"
    out[(major == 1) | ((major == 0) & (minor >= 2))] = "L2_WARNING"
    out[(major >= 2) | ((major >= 1) & (minor >= 2))] = "L3_CRITICAL"
    return out


def build_records(terrain_path: Path, climate_path: Path, magnetic_path: Path | None = None, region_spec=None) -> Tuple[pd.DataFrame, dict, rasterio.Affine, object, int, int]:
    with rasterio.open(terrain_path) as td, rasterio.open(climate_path) as cd:
        if td.crs != cd.crs or td.transform != cd.transform or td.shape != cd.shape:
            raise ValueError("Terrain and climate rasters must share CRS, transform, width and height.")
        dem = read_band(td, "terrain_elevation_m")
        slope = read_band(td, "terrain_slope_deg")
        road = np.nan_to_num(read_band(td, "road_class"), nan=0).astype(np.int16)
        building_mask = np.nan_to_num(read_band(td, "building_mask"), nan=0)
        building_height = np.nan_to_num(read_band(td, "building_height_m"), nan=0)
        natural = np.nan_to_num(read_band(td, "natural_obstacle_class"), nan=0).astype(np.int16)
        artificial = np.nan_to_num(read_band(td, "artificial_obstacle_class"), nan=0).astype(np.int16)

        magnetic_anomaly = np.zeros(td.shape, dtype=np.float32)
        magnetic_error = np.full(td.shape, np.nan, dtype=np.float32)
        magnetic_gradient = np.zeros(td.shape, dtype=np.float32)
        magnetic_source = None
        if magnetic_path is not None and magnetic_path.exists():
            with rasterio.open(magnetic_path) as md:
                if td.crs != md.crs or td.transform != md.transform or td.shape != md.shape:
                    raise ValueError("Magnetic, terrain and climate rasters must share CRS, transform and dimensions.")
                magnetic_anomaly = read_band(md, "magnetic_anomaly_nt")
                if "magnetic_anomaly_error_nt" in (md.descriptions or ()):
                    magnetic_error = read_band(md, "magnetic_anomaly_error_nt")
                if "magnetic_anomaly_gradient_nt_per_m" in (md.descriptions or ()):
                    magnetic_gradient = read_band(md, "magnetic_anomaly_gradient_nt_per_m")
                else:
                    gy, gx = np.gradient(magnetic_anomaly.astype(np.float64), abs(td.transform.e), abs(td.transform.a))
                    magnetic_gradient = np.hypot(gx, gy).astype(np.float32)
                magnetic_source = str(magnetic_path)

        valid_terrain = np.isfinite(dem) & np.isfinite(slope)

        # Apply the exact WGS 84 GeoJSON geometry at pixel-center precision.
        # This protects CRP generation when legacy/manual rasters cover only the
        # rectangular envelope but were not masked to the requested polygon.
        region_mask = np.ones(td.shape, dtype=bool)
        region_cells_total = int(region_mask.size)
        region_cells_inside = region_cells_total
        if region_spec is not None:
            geom_in_raster_crs = transform_geom(
                "EPSG:4326", td.crs, region_spec.geometry, precision=15
            )
            region_mask = geometry_mask(
                [geom_in_raster_crs], out_shape=td.shape, transform=td.transform,
                invert=True, all_touched=False
            )
            region_cells_inside = int(np.count_nonzero(region_mask))
            if region_cells_inside == 0:
                raise ValueError(
                    "region_v1.geojson does not intersect the terrain raster. "
                    "Rebuild the regional map with the same --region-file."
                )
            valid_terrain &= region_mask

        relief = dem - np.nanmin(np.where(region_mask, dem, np.nan))
        terrain_states = {
            "slope_state": classify_slope(slope),
            "local_relief_state": classify_relief(relief),
            "road_state": classify_road(road),
            "obstacle_type_state": classify_obstacle(natural, artificial, building_mask),
            "artificial_height_state": classify_artificial_height(building_height, artificial),
        }
        magnetic_state = classify_magnetic_anomaly(magnetic_anomaly)
        magnetic_gradient_state = classify_magnetic_gradient(magnetic_gradient)

        rows, cols = np.indices(td.shape)
        xs, ys = rasterio.transform.xy(td.transform, rows, cols, offset="center")
        xs = np.asarray(xs, dtype=np.float64)
        ys = np.asarray(ys, dtype=np.float64)

        frames = []
        for tb in TIME_BINS:
            temp = read_band(cd, f"temperature_c_{tb}")
            rh = read_band(cd, f"relative_humidity_percent_{tb}")
            wind_speed = read_band(cd, f"wind_vector_speed_ms_{tb}")
            wind_dir = read_band(cd, f"wind_direction_from_deg_{tb}")
            valid = valid_terrain & np.isfinite(temp) & np.isfinite(rh) & np.isfinite(wind_speed) & np.isfinite(wind_dir)
            if magnetic_source is not None:
                valid &= np.isfinite(magnetic_anomaly) & np.isfinite(magnetic_gradient)

            tstate = classify_temperature(temp)
            hstate = classify_humidity(rh)
            wstate = classify_wind_force(wind_speed)
            dstate = classify_wind_direction(wind_dir, wind_speed)
            cstate = critical_level(
                tstate, hstate, wstate, terrain_states["slope_state"], terrain_states["local_relief_state"],
                terrain_states["obstacle_type_state"], terrain_states["artificial_height_state"],
                magnetic_state, magnetic_gradient_state
            )
            idx = np.flatnonzero(valid.ravel())
            frame = pd.DataFrame({
                "time_state": f"UTC_{tb}",
                "row": rows.ravel()[idx],
                "col": cols.ravel()[idx],
                "x": xs.ravel()[idx],
                "y": ys.ravel()[idx],
                "temperature_c": temp.ravel()[idx],
                "relative_humidity_percent": rh.ravel()[idx],
                "wind_speed_ms": wind_speed.ravel()[idx],
                "wind_direction_from_deg": wind_dir.ravel()[idx],
                "terrain_elevation_m": dem.ravel()[idx],
                "terrain_slope_deg": slope.ravel()[idx],
                "local_relief_m": relief.ravel()[idx],
                "road_class_raw": road.ravel()[idx],
                "natural_obstacle_class_raw": natural.ravel()[idx],
                "artificial_obstacle_class_raw": artificial.ravel()[idx],
                "building_height_m": building_height.ravel()[idx],
                "magnetic_anomaly_nt": magnetic_anomaly.ravel()[idx],
                "magnetic_anomaly_error_nt": magnetic_error.ravel()[idx],
                "magnetic_gradient_nt_per_m": magnetic_gradient.ravel()[idx],
                "temperature_state": tstate.ravel()[idx],
                "humidity_state": hstate.ravel()[idx],
                "wind_force_state": wstate.ravel()[idx],
                "wind_direction_state": dstate.ravel()[idx],
                "slope_state": terrain_states["slope_state"].ravel()[idx],
                "local_relief_state": terrain_states["local_relief_state"].ravel()[idx],
                "road_state": terrain_states["road_state"].ravel()[idx],
                "obstacle_type_state": terrain_states["obstacle_type_state"].ravel()[idx],
                "artificial_height_state": terrain_states["artificial_height_state"].ravel()[idx],
                "magnetic_anomaly_state": magnetic_state.ravel()[idx],
                "magnetic_gradient_state": magnetic_gradient_state.ravel()[idx],
                "critical_level": cstate.ravel()[idx],
            })
            frames.append(frame)

        metadata = {
            "crs": str(td.crs), "transform": tuple(td.transform),
            "width": td.width, "height": td.height, "bounds": list(td.bounds),
            "terrain_source": str(terrain_path), "climate_source": str(climate_path),
            "magnetic_source": magnetic_source,
            "region_geojson": region_spec.provenance() if region_spec is not None else None,
            "region_mask": {
                "pixel_center_rule": "inside exact GeoJSON geometry; all_touched=false",
                "raster_cell_count": region_cells_total,
                "inside_geometry_cell_count": region_cells_inside,
                "inside_geometry_fraction": float(region_cells_inside / region_cells_total),
            },
        }
        return pd.concat(frames, ignore_index=True), metadata, td.transform, td.crs, td.width, td.height


def unique_medoid_catalog(df: pd.DataFrame, labels: np.ndarray, encoded, centers, requested_k: int) -> Tuple[pd.DataFrame, np.ndarray]:
    """Select one unique observed categorical signature per cluster and reassign records."""
    signatures = df[STATE_COLUMNS].astype(str).agg("|".join, axis=1)
    chosen_indices = []
    used_signatures = set()
    for cid in range(requested_k):
        members = np.flatnonzero(labels == cid)
        if len(members) == 0:
            continue
        center = centers[cid]
        sub = encoded[members]
        d2 = np.asarray(sub.multiply(sub).sum(axis=1)).ravel() - 2 * np.asarray(sub @ center).ravel() + np.dot(center, center)
        for pos in members[np.argsort(d2)]:
            sig = signatures.iloc[pos]
            if sig not in used_signatures:
                used_signatures.add(sig)
                chosen_indices.append(pos)
                break

    if len(chosen_indices) < requested_k:
        freq = signatures.value_counts()
        first_index = pd.Series(np.arange(len(df)), index=signatures).groupby(level=0).first()
        for sig in freq.index:
            if sig not in used_signatures:
                used_signatures.add(sig)
                chosen_indices.append(int(first_index.loc[sig]))
                if len(chosen_indices) == requested_k:
                    break
    if len(chosen_indices) < requested_k:
        raise RuntimeError(
            f"Only {len(chosen_indices)} unique trans-domain state signatures were observed; "
            f"cannot create {requested_k} non-duplicate CRP clusters."
        )

    medoid_encoded = encoded[chosen_indices]
    final_labels = np.empty(len(df), dtype=np.int16)
    batch = 50000
    medoid_norm = np.asarray(medoid_encoded.multiply(medoid_encoded).sum(axis=1)).ravel()
    for start in range(0, len(df), batch):
        sub = encoded[start:start+batch]
        sub_norm = np.asarray(sub.multiply(sub).sum(axis=1)).ravel()[:, None]
        dot = (sub @ medoid_encoded.T).toarray()
        d2 = sub_norm + medoid_norm[None, :] - 2 * dot
        final_labels[start:start+sub.shape[0]] = np.argmin(d2, axis=1)

    cat = df.iloc[chosen_indices][STATE_COLUMNS].copy().reset_index(drop=True)
    cat.insert(0, "cluster_id", np.arange(len(cat), dtype=int))
    cat.insert(1, "cluster_code", [f"CRP_{i:02d}" for i in range(len(cat))])
    cat["state_signature"] = cat[STATE_COLUMNS].astype(str).agg("|".join, axis=1)
    return cat, final_labels


def cluster_records(df: pd.DataFrame, requested_k: int, random_state: int):
    if requested_k < 28:
        raise ValueError("CRP cluster count must be at least 28.")
    unique_count = df[STATE_COLUMNS].drop_duplicates().shape[0]
    if unique_count < requested_k:
        raise RuntimeError(f"Dataset has only {unique_count} unique classified signatures; requested {requested_k}.")

    encoder = OneHotEncoder(handle_unknown="ignore", sparse_output=True, dtype=np.float32)
    encoded = encoder.fit_transform(df[STATE_COLUMNS])
    model = MiniBatchKMeans(
        n_clusters=requested_k, random_state=random_state, n_init=10,
        batch_size=8192, max_iter=200, reassignment_ratio=0.01
    )
    provisional = model.fit_predict(encoded)
    catalog, labels = unique_medoid_catalog(df, provisional, encoded, model.cluster_centers_, requested_k)
    return catalog, labels, encoder.categories_


def write_cluster_raster(path: Path, df: pd.DataFrame, transform, crs, width: int, height: int):
    profile = {
        "driver": "GTiff", "height": height, "width": width, "count": len(TIME_BINS),
        "dtype": "uint16", "crs": crs, "transform": transform, "nodata": 65535,
        "compress": "DEFLATE", "predictor": 2, "tiled": True,
        "blockxsize": max(16, min(256, (width // 16) * 16)), "blockysize": max(16, min(256, (height // 16) * 16)),
    }
    with rasterio.open(path, "w", **profile) as dst:
        for i, tb in enumerate(TIME_BINS, 1):
            arr = np.full((height, width), 65535, dtype=np.uint16)
            sub = df[df["time_state"] == f"UTC_{tb}"]
            arr[sub["row"].to_numpy(), sub["col"].to_numpy()] = sub["cluster_id"].to_numpy(np.uint16)
            dst.write(arr, i)
            dst.set_band_description(i, f"crp_cluster_id_{tb}")
        factors = [2, 4, 8]
        factors = [f for f in factors if min(width, height) // f >= 16]
        if factors:
            dst.build_overviews(factors, Resampling.nearest)


def markdown_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for row in df.itertuples(index=False, name=None):
        lines.append("| " + " | ".join(str(v).replace("|", "/") for v in row) + " |")
    return "\n".join(lines)


# -------- Plotly 3D cluster rendering helpers --------

def safe_json(obj):
    if isinstance(obj, dict):
        return {k: safe_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [safe_json(v) for v in obj]
    if isinstance(obj, tuple):
        return [safe_json(v) for v in obj]
    if isinstance(obj, np.generic):
        if np.issubdtype(type(obj), np.floating) and np.isnan(obj):
            return None
        return obj.item()
    if isinstance(obj, float) and math.isnan(obj):
        return None
    return obj


def downsample_step(height: int, width: int, max_grid: int) -> int:
    return max(1, int(math.ceil(max(height, width) / max_grid)))


def grid_from_transform(transform, height: int, width: int, step: int):
    rows = np.arange(0, height, step)
    cols = np.arange(0, width, step)
    xs = transform.c + (cols + 0.5) * transform.a
    ys = transform.f + (rows + 0.5) * transform.e
    return np.meshgrid(xs, ys)


def read_plot_surface(terrain_path: Path, max_grid: int):
    with rasterio.open(terrain_path) as ds:
        step = downsample_step(ds.height, ds.width, max_grid)
        dem = read_band(ds, "terrain_elevation_m")[::step, ::step]
        slope = read_band(ds, "terrain_slope_deg")[::step, ::step]
        building_mask = np.nan_to_num(read_band(ds, "building_mask"), nan=0.0)[::step, ::step]
        building_height = np.nan_to_num(read_band(ds, "building_height_m"), nan=0.0)[::step, ::step]
        xg, yg = grid_from_transform(ds.transform, ds.height, ds.width, step)
    building_top = np.where(building_mask > 0, dem + building_height, np.nan)
    return {
        "xg": xg,
        "yg": yg,
        "dem": dem,
        "slope": slope,
        "building_top": building_top,
        "step": step,
    }


def make_cluster_hover(sub: pd.DataFrame) -> np.ndarray:
    return np.stack([
        sub["time_state"].to_numpy(),
        sub["critical_level"].to_numpy(),
        sub["temperature_state"].to_numpy(),
        sub["humidity_state"].to_numpy(),
        sub["wind_force_state"].to_numpy(),
        sub["wind_direction_state"].to_numpy(),
        sub["slope_state"].to_numpy(),
        sub["local_relief_state"].to_numpy(),
        sub["road_state"].to_numpy(),
        sub["obstacle_type_state"].to_numpy(),
        sub["artificial_height_state"].to_numpy(),
        sub["magnetic_anomaly_state"].to_numpy(),
        sub["magnetic_gradient_state"].to_numpy(),
        sub["magnetic_anomaly_nt"].to_numpy(),
        sub["magnetic_gradient_nt_per_m"].to_numpy(),
    ], axis=-1)


def generate_cluster_plotly_views(
    terrain_path: Path,
    df: pd.DataFrame,
    catalog: pd.DataFrame,
    outdir: Path,
    max_grid: int = 140,
    max_points: int = 6000,
    region_spec=None,
) -> dict:
    try:
        import plotly.graph_objects as go
    except Exception as exc:
        raise RuntimeError("Plotly is required for --plot3d-clusters. Install it with: pip install plotly") from exc

    outdir.mkdir(parents=True, exist_ok=True)
    surface = read_plot_surface(terrain_path, max_grid=max_grid)
    xg = surface["xg"]
    yg = surface["yg"]
    dem = surface["dem"]
    slope = surface["slope"]
    building_top = surface["building_top"]

    projected_region_rings = []
    projected_region_markers = []
    if region_spec is not None:
        with rasterio.open(terrain_path) as ds:
            transformer = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True)
            projected_geometry = shapely_transform(transformer.transform, shapely_shape(region_spec.geometry))
            clipped_geometry = projected_geometry.intersection(shapely_box(*ds.bounds))

            def _iter_exterior_rings(geometry):
                if geometry.is_empty:
                    return
                if geometry.geom_type == "Polygon":
                    yield list(geometry.exterior.coords)
                elif geometry.geom_type == "MultiPolygon":
                    for polygon in geometry.geoms:
                        yield list(polygon.exterior.coords)
                elif geometry.geom_type == "GeometryCollection":
                    for part in geometry.geoms:
                        yield from _iter_exterior_rings(part)

            for ring in _iter_exterior_rings(clipped_geometry) or []:
                rx = [float(p[0]) for p in ring]
                ry = [float(p[1]) for p in ring]
                rz = []
                for x0, y0 in zip(rx, ry):
                    value = next(ds.sample([(x0, y0)], indexes=band_index(ds, "terrain_elevation_m")))[0]
                    if ds.nodata is not None and np.isclose(value, ds.nodata):
                        value = np.nanmedian(dem)
                    rz.append(float(value) + 25.0)
                projected_region_rings.append((rx, ry, rz))

            for name, point, color in (("Origin", region_spec.origin, "lime"), ("Destination", region_spec.destination, "red")):
                if point is None:
                    continue
                px, py = transformer.transform(point[0], point[1])
                if not (ds.bounds.left <= px <= ds.bounds.right and ds.bounds.bottom <= py <= ds.bounds.top):
                    continue
                value = next(ds.sample([(px, py)], indexes=band_index(ds, "terrain_elevation_m")))[0]
                if ds.nodata is not None and np.isclose(value, ds.nodata):
                    value = np.nanmedian(dem)
                projected_region_markers.append((name, px, py, float(value) + 35.0, color))

    cluster_files: List[dict] = []
    total_clusters = int(catalog["cluster_id"].nunique())
    colors = [
        "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b",
        "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"
    ]

    with progress() as p:
        task = p.add_task("Generating per-cluster Plotly 3D views", total=total_clusters)
        for cluster_row in catalog.sort_values("cluster_id").itertuples(index=False):
            cid = int(cluster_row.cluster_id)
            ccode = str(cluster_row.cluster_code)
            sub = df[df["cluster_id"] == cid].copy()
            if sub.empty:
                p.advance(task)
                continue
            if len(sub) > max_points:
                idx = np.linspace(0, len(sub) - 1, max_points).astype(int)
                sub = sub.iloc[idx].copy()

            z_cluster = sub["terrain_elevation_m"].to_numpy(np.float32) + np.maximum(sub["building_height_m"].to_numpy(np.float32), 0.0) + 4.0
            customdata = make_cluster_hover(sub)
            color = colors[cid % len(colors)]
            marker_size = max(2.0, 5.0 - min(3.0, len(sub) / 2500.0))

            fig = go.Figure()
            fig.add_trace(go.Surface(
                x=xg, y=yg, z=dem,
                surfacecolor=slope,
                colorscale="Earth",
                colorbar=dict(title="Slope (deg)"),
                opacity=0.96,
                name="Terrain",
                hovertemplate="X=%{x:.1f}<br>Y=%{y:.1f}<br>Terrain=%{z:.1f} m<br>Slope=%{surfacecolor:.1f}°<extra></extra>",
            ))
            fig.add_trace(go.Surface(
                x=xg, y=yg, z=building_top,
                surfacecolor=building_top - dem,
                colorscale="Greys",
                showscale=False,
                opacity=0.78,
                name="Buildings",
                hovertemplate="Building top=%{z:.1f} m<br>Building height=%{surfacecolor:.1f} m<extra></extra>",
            ))
            fig.add_trace(go.Scatter3d(
                x=sub["x"].to_numpy(np.float32),
                y=sub["y"].to_numpy(np.float32),
                z=z_cluster,
                mode="markers",
                marker=dict(
                    size=marker_size, color=sub["magnetic_anomaly_nt"].to_numpy(np.float32),
                    colorscale="RdBu", reversescale=True, opacity=0.9, symbol="circle",
                    colorbar=dict(title="Magnetic anomaly (nT)"),
                ),
                name=f"{ccode} footprint",
                customdata=customdata,
                hovertemplate=(
                    "Cluster=" + ccode + "<br>"
                    "Time=%{customdata[0]}<br>Critical=%{customdata[1]}<br>"
                    "Temp state=%{customdata[2]}<br>RH state=%{customdata[3]}<br>"
                    "Wind force=%{customdata[4]}<br>Wind dir=%{customdata[5]}<br>"
                    "Slope=%{customdata[6]}<br>Relief=%{customdata[7]}<br>"
                    "Road=%{customdata[8]}<br>Obstacle=%{customdata[9]}<br>Height=%{customdata[10]}<br>"
                    "Magnetic state=%{customdata[11]}<br>Gradient state=%{customdata[12]}<br>"
                    "Anomaly=%{customdata[13]:.2f} nT<br>Gradient=%{customdata[14]:.6f} nT/m<br>"
                    "Terrain z=%{z:.1f} m<extra></extra>"
                ),
            ))

            centroid_x = float(sub["x"].mean())
            centroid_y = float(sub["y"].mean())
            centroid_z = float(np.nanmean(z_cluster))
            fig.add_trace(go.Scatter3d(
                x=[centroid_x], y=[centroid_y], z=[centroid_z + 8.0],
                mode="markers+text",
                marker=dict(size=7, color="red", symbol="diamond"),
                text=[ccode], textposition="top center",
                name="Cluster center",
                hovertemplate=f"{ccode}<br>Cluster center<extra></extra>",
            ))

            for rx, ry, rz in projected_region_rings:
                fig.add_trace(go.Scatter3d(
                    x=rx, y=ry, z=rz, mode="lines", line=dict(color="cyan", width=7),
                    name="GeoJSON region boundary", hovertemplate="Exact region_v1.geojson boundary<extra></extra>"))
            for marker_name, mx, my, mz, marker_color in projected_region_markers:
                fig.add_trace(go.Scatter3d(
                    x=[mx], y=[my], z=[mz], mode="markers+text",
                    marker=dict(size=8, color=marker_color, symbol="diamond"),
                    text=[marker_name], textposition="top center", name=marker_name,
                    hovertemplate=f"{marker_name} from region_v1.geojson<extra></extra>"))

            title_parts = [
                f"{ccode} — cluster {cid:02d}",
                f"time={getattr(cluster_row, 'time_state')}",
                f"critical={getattr(cluster_row, 'critical_level')}",
                f"records={int(getattr(cluster_row, 'record_count', len(sub)))}",
            ]
            fig.update_layout(
                title=" | ".join(title_parts),
                scene=dict(
                    xaxis_title="Projected X (m)",
                    yaxis_title="Projected Y (m)",
                    zaxis_title="Elevation / structure height (m)",
                    aspectmode="data",
                ),
                legend=dict(orientation="h"),
                margin=dict(l=0, r=0, b=0, t=55),
            )

            filename = f"crp_cluster_{cid:02d}_3d.html"
            fig.write_html(outdir / filename, include_plotlyjs="cdn")
            cluster_files.append({
                "cluster_id": cid,
                "cluster_code": ccode,
                "record_count": int(getattr(cluster_row, "record_count", len(sub))),
                "time_state": str(getattr(cluster_row, "time_state")),
                "critical_level": str(getattr(cluster_row, "critical_level")),
                "file": filename,
            })
            p.advance(task)

    index_lines = [
        "<!DOCTYPE html>",
        "<html><head><meta charset='utf-8'><title>CRP 3D cluster views</title></head><body>",
        "<h1>CRP 3D cluster views</h1>",
        "<p>One interactive Plotly map per CRP cluster.</p>",
        "<ul>",
    ]
    for item in cluster_files:
        index_lines.append(
            f"<li><a href='{item['file']}'>{item['cluster_code']} (cluster {item['cluster_id']:02d})</a> — "
            f"time={item['time_state']}, critical={item['critical_level']}, records={item['record_count']}</li>"
        )
    index_lines.extend(["</ul>", "</body></html>"])
    (outdir / "crp_cluster_3d_index.html").write_text("\n".join(index_lines), encoding="utf-8")

    summary = {
        "cluster_count": total_clusters,
        "max_grid": int(max_grid),
        "max_points_per_cluster": int(max_points),
        "files": cluster_files,
        "index": "crp_cluster_3d_index.html",
        "region_geojson": region_spec.provenance() if region_spec is not None else None,
    }
    (outdir / "crp_cluster_3d_manifest.json").write_text(
        json.dumps(safe_json(summary), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return summary



def validate_region_raster_coverage(terrain_path: Path, region_spec) -> dict:
    """Require the terrain raster envelope to cover the complete GeoJSON geometry."""
    with rasterio.open(terrain_path) as ds:
        transformer = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True)
        geom = shapely_transform(transformer.transform, shapely_shape(region_spec.geometry))
        requested = tuple(float(v) for v in geom.bounds)
        available = tuple(float(v) for v in ds.bounds)
        tolerance_x = max(2.0 * abs(ds.transform.a), 1.0)
        tolerance_y = max(2.0 * abs(ds.transform.e), 1.0)
        if (requested[0] < available[0] - tolerance_x or requested[1] < available[1] - tolerance_y or
                requested[2] > available[2] + tolerance_x or requested[3] > available[3] + tolerance_y):
            raise ValueError(
                "The terrain dataset does not cover the complete region_v1.geojson geometry. "
                f"available={available}, requested={requested}. "
                "Run build_regional_map.py with the same --region-file before build_crp_dataset.py."
            )
        return {
            "available_bounds_projected": list(available),
            "requested_bounds_projected": list(requested),
            "coverage_verified": True,
        }

def main():
    ap = argparse.ArgumentParser(description="Create a classified trans-domain CRP dataset folder.")
    ap.add_argument("--region-file", default="region/region_v1.geojson", help="WGS 84 region GeoJSON used to locate default dataset outputs")
    ap.add_argument("--terrain", default=None, help="terrain_obstacles.tif; defaults from region file")
    ap.add_argument("--climate", default=None, help="climate_contexts.tif; defaults from region file")
    ap.add_argument("--magnetic", default=None,
                    help="magnetic_anomaly.tif; default auto-detects it beside terrain raster.")
    ap.add_argument("--output-root", default=None, help="Parent output directory; default is raster directory")
    ap.add_argument("--clusters", type=int, default=32, help="Number of CRP clusters, minimum 28 (default 32)")
    ap.add_argument("--random-state", type=int, default=42)
    ap.add_argument("--plot3d-clusters", action="store_true",
                    help="Generate one separate interactive Plotly 3D HTML map per CRP cluster.")
    ap.add_argument("--plot3d-dir", default=None,
                    help="Directory for cluster Plotly outputs; default is CRP/plotly_3d_clusters")
    ap.add_argument("--plot-max-grid", type=int, default=140,
                    help="Maximum terrain grid size used for Plotly downsampling (default 140)")
    ap.add_argument("--plot-max-points", type=int, default=6000,
                    help="Maximum plotted cluster cells per cluster graph (default 6000)")
    args = ap.parse_args()

    spec = load_region_geojson(args.region_file)
    defaults = default_dataset_paths(spec)
    terrain = Path(args.terrain or defaults["terrain"]).resolve()
    climate = Path(args.climate or defaults["climate"]).resolve()
    magnetic = Path(args.magnetic).resolve() if args.magnetic else terrain.parent / "magnetic_anomaly.tif"
    if not magnetic.exists():
        CONSOLE.print("[yellow]Magnetic raster not found; CRP will use neutral zero-anomaly placeholders for backward compatibility.[/]")
        magnetic = None
    coverage_info = validate_region_raster_coverage(terrain, spec)
    parent = Path(args.output_root).resolve() if args.output_root else terrain.parent
    out = parent / "CRP"
    out.mkdir(parents=True, exist_ok=True)

    plot_summary = None
    with progress() as p:
        task = p.add_task("Reading and classifying climate, terrain and time domains", total=6 if args.plot3d_clusters else 5)
        df, source_meta, transform, crs, width, height = build_records(terrain, climate, magnetic, region_spec=spec)
        p.advance(task)

        catalog, labels, encoder_categories = cluster_records(df, args.clusters, args.random_state)
        df["cluster_id"] = labels
        df["cluster_code"] = [f"CRP_{x:02d}" for x in labels]
        p.advance(task)

        numeric_cols = [
            "temperature_c", "relative_humidity_percent", "wind_speed_ms", "wind_direction_from_deg",
            "terrain_elevation_m", "terrain_slope_deg", "local_relief_m", "building_height_m",
            "magnetic_anomaly_nt", "magnetic_anomaly_error_nt", "magnetic_gradient_nt_per_m"
        ]
        numeric_summary = df.groupby("cluster_id")[numeric_cols].agg(["mean", "min", "max"])
        numeric_summary.columns = [f"{a}_{b}" for a, b in numeric_summary.columns]
        numeric_summary = numeric_summary.reset_index()
        counts = df.groupby("cluster_id").size().rename("record_count").reset_index()
        time_counts = pd.crosstab(df["cluster_id"], df["time_state"]).reset_index()
        catalog = catalog.merge(counts, on="cluster_id", how="left").merge(time_counts, on="cluster_id", how="left")
        catalog = catalog.merge(numeric_summary, on="cluster_id", how="left")
        runtime_statistics, runtime_additions = build_runtime_statistics(df, catalog)
        catalog = catalog.merge(runtime_additions, on="cluster_id", how="left")
        # Keep record_count for backward compatibility; sample_count is the canonical n_k.
        if not np.array_equal(catalog["record_count"].to_numpy(), catalog["sample_count"].to_numpy()):
            raise RuntimeError("Internal CRP count mismatch: record_count != sample_count")
        p.advance(task)

        catalog.to_csv(out / "crp_cluster_catalog.csv", index=False)
        (out / "crp_runtime_statistics.json").write_text(
            json.dumps(safe_json(runtime_statistics), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        df.to_csv(out / "crp_cell_assignments.csv.gz", index=False, compression="gzip")
        write_cluster_raster(out / "crp_cluster_map.tif", df, transform, crs, width, height)
        p.advance(task)

        if args.plot3d_clusters:
            plot_dir = Path(args.plot3d_dir).resolve() if args.plot3d_dir else out / "plotly_3d_clusters"
            plot_summary = generate_cluster_plotly_views(
                terrain_path=terrain,
                df=df,
                catalog=catalog,
                outdir=plot_dir,
                max_grid=args.plot_max_grid,
                max_points=args.plot_max_points,
                region_spec=spec,
            )
            p.advance(task)

        compact_cols = ["cluster_id", "cluster_code"] + STATE_COLUMNS + ["record_count", "sample_count", "quality_score"]
        (out / "crp_cluster_catalog.md").write_text(
            "# CRP cluster catalog\n\n" + markdown_table(catalog[compact_cols]) + "\n",
            encoding="utf-8"
        )
        files = [
            "crp_cluster_catalog.csv", "crp_cluster_catalog.md", "crp_cell_assignments.csv.gz",
            "crp_cluster_map.tif", "crp_runtime_statistics.json", "metadata.json", "README.md"
        ]
        if args.plot3d_clusters:
            files.append(str((Path(args.plot3d_dir).resolve() if args.plot3d_dir else out / "plotly_3d_clusters").name))
        metadata = {
            "title": "Classified trans-domain Context Reference Pattern dataset",
            "region_geojson": spec.provenance(),
            "cluster_count": int(args.clusters),
            "minimum_supported_cluster_count": 28,
            "algorithm": "OneHotEncoder + MiniBatchKMeans + unique observed medoid signatures",
            "duplicate_full_state_signatures": int(catalog["state_signature"].duplicated().sum()),
            "canonical_cluster_definition": "C_k=(mu_k,Sigma_k,n_k,q_k)",
            "runtime_statistics_file": "crp_runtime_statistics.json",
            "runtime_feature_order": RUNTIME_FEATURE_ORDER,
            "quality_score_range": [0.0, 1.0],
            "state_columns": STATE_COLUMNS,
            "time_bins_utc": TIME_BINS,
            "source": source_meta,
            "region_coverage_validation": coverage_info,
            "classification_notes": {
                "temperature": "ETCCDI/Climpact threshold-based operational adaptation",
                "humidity": "project CRP operational RH classes",
                "wind_force": "compact Beaufort groups",
                "wind_direction": "meteorological FROM direction, CALM + 8 compass sectors",
                "slope": "FAO degree-based classes",
                "local_relief": "local elevation above ROI minimum; 300 m boundary traceable to UNEP-WCMC concept",
                "roads": "OSM semantic road classes, not hazard levels",
                "artificial_height": "FAA-informed 61 m and 152 m boundaries; 15 m and 30 m are project operational divisions",
                "magnetic_anomaly": "fixed polarity/magnitude bins in nT derived from NOAA NCEI EMAG2 v3",
                "magnetic_gradient": "horizontal gradient bins in nT/m; high gradients contribute to the project critical flag",
                "critical_level": "project composite level, not a universal standard hazard level",
            },
            "encoder_categories": {col: list(map(str, cats)) for col, cats in zip(STATE_COLUMNS, encoder_categories)},
            "files": files,
            "plotly_3d_clusters": plot_summary,
        }
        (out / "metadata.json").write_text(json.dumps(safe_json(metadata), indent=2, ensure_ascii=False), encoding="utf-8")
        readme_lines = [
            "# CRP dataset\n",
            f"This folder contains {args.clusters} non-duplicate trans-domain CRP cluster signatures.\n",
            "Each record combines climate, terrain/infrastructure and UTC time-window indicators.",
            "Each cluster now has the canonical runtime form `C_k=(mu_k, Sigma_k, n_k, q_k)`.",
            "`crp_runtime_statistics.json` stores centroid, empirical covariance, sample_count, quality_score, feature_order, and a regularized covariance copy for stable runtime inversion.",
            "`crp_cluster_catalog.csv` retains all previous columns and adds explicit `sample_count` and `quality_score` columns.",
            "The cluster map contains one band per UTC time window. Cluster IDs use 0-based numbering.\n",
            "The critical level is a project composite flag and must be recalibrated using the actual UAV envelope, ",
            "clearance, battery-temperature limits and maximum allowable wind.\n",
        ]
        if args.plot3d_clusters:
            readme_lines.extend([
                "\n## Plotly 3D cluster views\n",
                "When `--plot3d-clusters` is enabled, one separate interactive Plotly HTML map is created for each CRP cluster.",
                "These files are written under `plotly_3d_clusters/` by default, together with an index page and a JSON manifest.\n",
            ])
        (out / "README.md").write_text("\n".join(readme_lines), encoding="utf-8")
        p.advance(task)

    CONSOLE.print(f"[bold green]CRP dataset created:[/] {out}")
    CONSOLE.print(f"Clusters: {len(catalog)}; duplicate full signatures: {catalog['state_signature'].duplicated().sum()}")
    if plot_summary is not None:
        CONSOLE.print(f"[bold green]3D cluster views created:[/] {(Path(args.plot3d_dir).resolve() if args.plot3d_dir else out / 'plotly_3d_clusters')}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        CONSOLE.print_exception()
        CONSOLE.print(f"[bold red]CRP build failed:[/] {exc}")
        sys.exit(1)
