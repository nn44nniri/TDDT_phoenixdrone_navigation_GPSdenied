#!/usr/bin/env python3
"""
Build a real-data, multilayer regional map (about 9 km² by default).

Default region:
  Boulder, Colorado foothills, USA
  Center: 40.0150 N, -105.2920 E
  Projected CRS: EPSG:32613 (UTM zone 13N)
  Extent: 3000 m × 3000 m = 9.0 km²

Data sources:
  - OpenStreetMap via Overpass (through OSMnx): roads, buildings, water,
    cliffs, vegetation, towers, masts, power infrastructure, etc.
  - Copernicus DEM GLO-30 through Microsoft Planetary Computer STAC.
  - Open-Meteo Historical Weather API: temperature, relative humidity,
    wind speed and wind direction.
  - NOAA NCEI EMAG2 v3: crustal magnetic anomaly, estimated error and source code.

Outputs:
  terrain_obstacles.tif
  climate_contexts.tif
  magnetic_anomaly.tif
  vectors.gpkg
  climate_centroids.csv
  climate_samples.csv
  metadata.json
  README_generated.md

The program prints live, progressive status using Rich.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import urlsplit, urlunsplit

import numpy as np
import pandas as pd
import requests
import rasterio
from rasterio.features import rasterize, geometry_mask
from rasterio.transform import from_bounds
from rasterio.warp import reproject, Resampling, transform_bounds, transform_geom
from rasterio.windows import Window, from_bounds as window_from_bounds
from pyproj import CRS, Transformer
from shapely.geometry import box
from sklearn.cluster import MiniBatchKMeans
from sklearn.preprocessing import StandardScaler
from rich.console import Console
from rich.progress import (
    Progress, SpinnerColumn, TextColumn, BarColumn,
    TaskProgressColumn, TimeElapsedColumn, TimeRemainingColumn
)
from rich.table import Table

import geopandas as gpd
import osmnx as ox
from pystac_client import Client
import planetary_computer
from region_geojson import load_region_geojson, default_output_dir

CONSOLE = Console()

DEFAULT_OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api",
    "https://overpass.kumi.systems/api",
    "https://overpass.nchc.org.tw/api",
    "https://maps.mail.ru/osm/tools/overpass/api",
    "https://overpass.openstreetmap.ru/api",
]


def normalize_overpass_endpoint(url: str) -> str:
    """Normalize an Overpass URL to the base URL expected by OSMnx 2.x."""
    value = str(url).strip().rstrip("/")
    if not value:
        raise ValueError("Empty Overpass endpoint")
    if value.endswith("/interpreter"):
        value = value[:-len("/interpreter")].rstrip("/")
    if not (value.startswith("https://") or value.startswith("http://")):
        raise ValueError(f"Overpass endpoint must be HTTP(S): {url}")
    parts = urlsplit(value)
    # Host-only URLs are treated as standard Overpass deployments and receive /api.
    # Existing custom paths are preserved because OSMnx itself appends /interpreter.
    if parts.path in ("", "/"):
        value = urlunsplit((parts.scheme, parts.netloc, "/api", parts.query, parts.fragment)).rstrip("/")
    return value


def load_overpass_endpoints(config_path: str | Path | None = None,
                            explicit: str | None = None) -> list[str]:
    """Load a de-duplicated Overpass failover pool. Explicit CLI values win."""
    raw: list[str] = []
    if explicit:
        raw = [x.strip() for x in explicit.split(",") if x.strip()]
    elif config_path:
        cfg = Path(config_path)
        if cfg.exists():
            data = json.loads(cfg.read_text(encoding="utf-8"))
            values = data.get("endpoints", data) if isinstance(data, dict) else data
            if not isinstance(values, list):
                raise ValueError(f"Overpass config must contain a list of endpoints: {cfg}")
            for item in values:
                if isinstance(item, str):
                    raw.append(item)
                elif isinstance(item, dict) and item.get("url") and item.get("enabled", True):
                    raw.append(str(item["url"]))
    if not raw:
        raw = list(DEFAULT_OVERPASS_ENDPOINTS)
    result: list[str] = []
    for item in raw:
        value = normalize_overpass_endpoint(item)
        if value not in result:
            result.append(value)
    if not result:
        raise ValueError("At least one Overpass endpoint is required")
    return result

@dataclass(frozen=True)
class Region:
    center_lat: float
    center_lon: float
    size_m: float
    crs: CRS
    west: float
    south: float
    east: float
    north: float
    minx: float
    miny: float
    maxx: float
    maxy: float

    @property
    def area_km2(self) -> float:
        return ((self.maxx - self.minx) * (self.maxy - self.miny)) / 1_000_000.0


def progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=CONSOLE,
    )


def make_region(lat: float, lon: float, size_m: float, epsg: int) -> Region:
    crs = CRS.from_epsg(epsg)
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    cx, cy = fwd.transform(lon, lat)
    half = size_m / 2.0
    minx, miny, maxx, maxy = cx-half, cy-half, cx+half, cy+half
    west, south = inv.transform(minx, miny)
    east, north = inv.transform(maxx, maxy)
    return Region(lat, lon, size_m, crs, west, south, east, north,
                  minx, miny, maxx, maxy)



def make_region_from_geojson(region_file: str | Path, epsg: int) -> tuple[Region, dict, object]:
    spec = load_region_geojson(region_file)
    crs = CRS.from_epsg(epsg)
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    # Project the complete polygon, not only two corners.
    projected = transform_geom("EPSG:4326", crs.to_string(), spec.geometry, precision=15)
    coords = []
    def walk(v):
        if isinstance(v, (list, tuple)) and v and isinstance(v[0], (int, float)):
            coords.append((float(v[0]), float(v[1])))
        else:
            for q in v:
                walk(q)
    walk(projected["coordinates"])
    minx=min(x for x,_ in coords); maxx=max(x for x,_ in coords)
    miny=min(y for _,y in coords); maxy=max(y for _,y in coords)
    cx, cy = fwd.transform(spec.center_lon, spec.center_lat)
    size_m=max(maxx-minx, maxy-miny)
    region=Region(spec.center_lat, spec.center_lon, size_m, crs, spec.west, spec.south, spec.east, spec.north, minx, miny, maxx, maxy)
    return region, projected, spec

def valid_tiff_block_size(dimension: int, preferred: int = 256) -> int:
    """Return a legal tiled GeoTIFF block dimension (multiple of 16)."""
    if dimension < 16:
        return 16
    return max(16, min(preferred, (dimension // 16) * 16))

def apply_region_mask(stack: np.ndarray, projected_geometry: dict | None, transform) -> np.ndarray:
    if projected_geometry is None:
        return stack
    inside = geometry_mask([projected_geometry], out_shape=stack.shape[-2:], transform=transform, invert=True)
    out = stack.astype(np.float32, copy=True)
    out[:, ~inside] = np.nan
    return out


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file_with_retries(url: str, destination: Path, retries: int = 5,
                               timeout: tuple[int, int] = (30, 300)) -> Path:
    """Download atomically with exponential backoff and a reusable partial file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            with requests.get(url, stream=True, timeout=timeout) as response:
                response.raise_for_status()
                with partial.open("wb") as fh:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            fh.write(chunk)
            if partial.stat().st_size == 0:
                raise RuntimeError(f"Downloaded empty file from {url}")
            partial.replace(destination)
            return destination
        except Exception as exc:
            last_exc = exc
            if attempt == retries:
                break
            time.sleep(min(30.0, 2.0 ** (attempt - 1)))
    partial.unlink(missing_ok=True)
    raise RuntimeError(f"Failed to download {url} after {retries} attempts: {last_exc}")


def _empty_layer(region: Region) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(geometry=[], crs=region.crs)


def save_osm_layer_cache(name: str, gdf: gpd.GeoDataFrame, osm_dir: Path) -> dict:
    osm_dir.mkdir(parents=True, exist_ok=True)
    gpkg = osm_dir / f"{name}.gpkg"
    empty_marker = osm_dir / f"{name}.empty.json"
    gpkg.unlink(missing_ok=True)
    empty_marker.unlink(missing_ok=True)
    if gdf.empty:
        empty_marker.write_text(json.dumps({"layer": name, "empty": True}, indent=2), encoding="utf-8")
        return {"layer": name, "empty": True, "file": empty_marker.name, "record_count": 0}
    gdf.to_file(gpkg, layer=name, driver="GPKG")
    return {
        "layer": name, "empty": False, "file": gpkg.name,
        "record_count": int(len(gdf)), "sha256": sha256_file(gpkg),
    }


def load_osm_layer_cache(name: str, region: Region, osm_dir: Path) -> gpd.GeoDataFrame:
    gpkg = osm_dir / f"{name}.gpkg"
    empty_marker = osm_dir / f"{name}.empty.json"
    if gpkg.exists():
        gdf = gpd.read_file(gpkg, layer=name)
        if gdf.crs is None:
            raise RuntimeError(f"Cached OSM layer has no CRS: {gpkg}")
        return gdf.to_crs(region.crs)
    if empty_marker.exists():
        return _empty_layer(region)
    raise FileNotFoundError(f"Offline OSM source is missing for layer '{name}' in {osm_dir}")


def download_osm_layer_strict(tags: dict, region: Region, endpoints: list[str],
                              retries_per_endpoint: int = 2, timeout: int = 180,
                              endpoint_cooldown: float = 1.0) -> tuple[gpd.GeoDataFrame, dict]:
    """Download one OSM layer with round-robin Overpass failover."""
    if retries_per_endpoint < 1:
        raise ValueError("retries_per_endpoint must be >= 1")
    attempts: list[dict] = []
    last_exc = None
    original_url = getattr(ox.settings, "overpass_url", None)
    original_timeout = getattr(ox.settings, "requests_timeout", None)
    try:
        if hasattr(ox.settings, "requests_timeout"):
            ox.settings.requests_timeout = int(timeout)
        for retry_round in range(1, retries_per_endpoint + 1):
            for endpoint_index, endpoint in enumerate(endpoints, start=1):
                started = time.monotonic()
                try:
                    ox.settings.overpass_url = endpoint
                    CONSOLE.print(
                        f"[cyan]OSM Overpass endpoint {endpoint_index}/{len(endpoints)} "
                        f"round {retry_round}/{retries_per_endpoint}: {endpoint}[/]"
                    )
                    gdf = ox.features_from_bbox(
                        bbox=(region.west, region.south, region.east, region.north), tags=tags
                    )
                    elapsed = round(time.monotonic() - started, 3)
                    attempts.append({
                        "endpoint": endpoint, "round": retry_round, "status": "ok",
                        "elapsed_seconds": elapsed,
                    })
                    if gdf.empty:
                        return _empty_layer(region), {
                            "overpass_endpoint": endpoint,
                            "overpass_attempts": attempts,
                        }
                    gdf = gdf.reset_index()
                    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
                    return gdf.to_crs(region.crs), {
                        "overpass_endpoint": endpoint,
                        "overpass_attempts": attempts,
                    }
                except Exception as exc:
                    last_exc = exc
                    elapsed = round(time.monotonic() - started, 3)
                    attempts.append({
                        "endpoint": endpoint, "round": retry_round, "status": "failed",
                        "elapsed_seconds": elapsed, "error": str(exc)[:1500],
                    })
                    CONSOLE.print(
                        f"[yellow]OSM endpoint failed ({endpoint_index}/{len(endpoints)}, "
                        f"round {retry_round}/{retries_per_endpoint}) {endpoint}: {exc}[/]"
                    )
                    if endpoint_cooldown > 0:
                        time.sleep(float(endpoint_cooldown))
        summary = "; ".join(
            f"{a['endpoint']} round={a['round']}: {a.get('error', a['status'])}"
            for a in attempts if a["status"] == "failed"
        )
        raise RuntimeError(
            f"OSM download failed across {len(endpoints)} Overpass endpoints "
            f"after {retries_per_endpoint} round(s) each. Last error: {last_exc}. "
            f"Attempts: {summary}"
        )
    finally:
        if original_url is not None:
            ox.settings.overpass_url = original_url
        if original_timeout is not None and hasattr(ox.settings, "requests_timeout"):
            ox.settings.requests_timeout = original_timeout


def _bounds_match(saved, region: Region, tolerance: float = 1e-7) -> bool:
    requested = [region.west, region.south, region.east, region.north]
    return len(saved or []) == 4 and all(abs(float(a) - float(b)) <= tolerance for a, b in zip(saved, requested))


def fetch_or_load_osm(region: Region, osm_dir: Path, offline: bool = False,
                      refresh: bool = False, endpoints: list[str] | None = None,
                      retries_per_endpoint: int = 2, timeout: int = 180,
                      endpoint_cooldown: float = 1.0) -> tuple[dict[str, gpd.GeoDataFrame], dict]:
    specs = [
        ("roads", {"highway": True}),
        ("buildings", {"building": True}),
        ("water", {"natural": ["water", "wetland"], "waterway": True,
                   "landuse": ["reservoir", "basin"]}),
        ("natural", {"natural": ["wood", "scrub", "cliff", "scree", "bare_rock"],
                     "landuse": ["forest"]}),
        ("artificial", {"man_made": ["tower", "mast", "chimney", "water_tower", "storage_tank"],
                        "power": ["tower", "pole", "line", "minor_line"]}),
    ]
    osm_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = osm_dir / "osm_sources_manifest.json"
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not _bounds_match(previous.get("bounds_wgs84"), region):
            if offline:
                raise RuntimeError(
                    "Persisted OSM sources belong to a different geographic region. "
                    "Run online with --refresh-osm for the new region."
                )
            refresh = True
    endpoint_pool = endpoints or list(DEFAULT_OVERPASS_ENDPOINTS)
    layers, entries = {}, []
    for name, tags in specs:
        cached = (osm_dir / f"{name}.gpkg").exists() or (osm_dir / f"{name}.empty.json").exists()
        if offline or (cached and not refresh):
            layers[name] = load_osm_layer_cache(name, region, osm_dir)
            entries.append({"layer": name, "mode": "offline-cache", "record_count": int(len(layers[name]))})
        else:
            layers[name], overpass_info = download_osm_layer_strict(
                tags, region, endpoint_pool,
                retries_per_endpoint=retries_per_endpoint, timeout=timeout,
                endpoint_cooldown=endpoint_cooldown,
            )
            entry = save_osm_layer_cache(name, layers[name], osm_dir)
            entry["mode"] = "downloaded"
            entry.update(overpass_info)
            entries.append(entry)
    manifest = {
        "source": "OpenStreetMap / Overpass via OSMnx",
        "bounds_wgs84": [region.west, region.south, region.east, region.north],
        "overpass_endpoint_pool": endpoint_pool,
        "overpass_retries_per_endpoint": retries_per_endpoint,
        "overpass_timeout_seconds": timeout,
        "layers": entries,
        "offline_ready": True,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return layers, manifest

def safe_gdf(tags: dict, region: Region) -> gpd.GeoDataFrame:
    """Legacy convenience wrapper using the same Overpass failover pool."""
    try:
        gdf, _ = download_osm_layer_strict(
            tags, region, list(DEFAULT_OVERPASS_ENDPOINTS), retries_per_endpoint=1
        )
        return gdf
    except Exception as exc:
        CONSOLE.print(f"[yellow]OSM layer warning:[/] {exc}")
        return gpd.GeoDataFrame(geometry=[], crs=region.crs)


def infer_building_height(row) -> float:
    for field in ("height", "building:height"):
        value = row.get(field)
        if value not in (None, "", np.nan):
            try:
                return max(2.5, float(str(value).split()[0]))
            except Exception:
                pass
    levels = row.get("building:levels")
    try:
        return max(3.0, float(levels) * 3.0)
    except Exception:
        return 7.5  # conservative default for unmapped low-rise buildings


ROAD_CODES = {
    "motorway": 8, "trunk": 7, "primary": 6, "secondary": 5,
    "tertiary": 4, "residential": 3, "service": 2,
    "track": 1, "path": 1, "footway": 1, "cycleway": 1,
}

NATURAL_CODES = {
    "water": 1,
    "wetland": 2,
    "wood": 3,
    "forest": 3,
    "scrub": 4,
    "cliff": 5,
    "scree": 6,
    "bare_rock": 7,
}

ARTIFICIAL_CODES = {
    "tower": 1,
    "mast": 2,
    "chimney": 3,
    "water_tower": 4,
    "storage_tank": 5,
    "power_tower": 6,
    "power_pole": 7,
    "power_line": 8,
}


def geom_shapes(gdf: gpd.GeoDataFrame, value_func, buffer_m: float = 0.0):
    shapes = []
    if gdf.empty:
        return shapes
    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        if buffer_m > 0 and geom.geom_type in ("LineString", "MultiLineString", "Point", "MultiPoint"):
            geom = geom.buffer(buffer_m)
        try:
            value = value_func(row)
            shapes.append((geom, value))
        except Exception:
            continue
    return shapes


def download_osm(region: Region) -> dict[str, gpd.GeoDataFrame]:
    layers = {}
    layers["roads"] = safe_gdf({"highway": True}, region)
    layers["buildings"] = safe_gdf({"building": True}, region)
    layers["water"] = safe_gdf({
        "natural": ["water", "wetland"],
        "waterway": True,
        "landuse": ["reservoir", "basin"],
    }, region)
    layers["natural"] = safe_gdf({
        "natural": ["wood", "scrub", "cliff", "scree", "bare_rock"],
        "landuse": ["forest"],
    }, region)
    layers["artificial"] = safe_gdf({
        "man_made": ["tower", "mast", "chimney", "water_tower", "storage_tank"],
        "power": ["tower", "pole", "line", "minor_line"],
    }, region)
    return layers


def _dem_manifest_path(dem_dir: Path) -> Path:
    return dem_dir / "dem_sources_manifest.json"


def acquire_dem_sources(region: Region, dem_dir: Path, offline: bool = False,
                        refresh: bool = False) -> tuple[list[Path], dict]:
    """Persist complete intersecting Copernicus source tiles for repeatable offline runs."""
    dem_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = _dem_manifest_path(dem_dir)
    if offline:
        if not manifest_path.exists():
            raise FileNotFoundError(
                f"DEM offline manifest is missing: {manifest_path}. Run once online first."
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not _bounds_match(manifest.get("requested_bounds_wgs84"), region):
            raise RuntimeError(
                "Persisted DEM sources belong to a different geographic region. "
                "Run online with --refresh-dem for the new region."
            )
        files = [dem_dir / item["file"] for item in manifest.get("tiles", [])]
        missing = [str(x) for x in files if not x.exists()]
        if missing:
            raise FileNotFoundError(f"DEM offline source files are missing: {missing}")
        return files, manifest

    if manifest_path.exists() and not refresh:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        same_region = _bounds_match(manifest.get("requested_bounds_wgs84"), region)
        files = [dem_dir / item["file"] for item in manifest.get("tiles", [])]
        if same_region and files and all(x.exists() for x in files):
            return files, manifest
        if not same_region:
            refresh = True

    catalog = Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )
    search = catalog.search(
        collections=["cop-dem-glo-30"],
        bbox=[region.west, region.south, region.east, region.north],
    )
    items = list(search.items())
    if not items:
        raise RuntimeError("No Copernicus DEM item intersects the requested region.")

    entries, files = [], []
    for item in items:
        href = item.assets["data"].href
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in item.id)
        local = dem_dir / f"{safe_id}.tif"
        if refresh or not local.exists():
            CONSOLE.print(f"[cyan]Downloading complete Copernicus DEM tile for offline use:[/] {item.id}")
            download_file_with_retries(href, local)
        with rasterio.open(local) as src:
            if src.crs is None:
                raise RuntimeError(f"Downloaded DEM tile has no CRS: {local}")
        files.append(local)
        entries.append({
            "item_id": item.id, "file": local.name, "size_bytes": local.stat().st_size,
            "sha256": sha256_file(local),
        })
    manifest = {
        "source": "Copernicus DEM GLO-30 via Microsoft Planetary Computer",
        "requested_bounds_wgs84": [region.west, region.south, region.east, region.north],
        "tiles": entries, "offline_ready": True,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return files, manifest


def fetch_dem(region: Region, width: int, height: int, transform,
              dem_dir: Path, offline: bool = False, refresh: bool = False) -> tuple[np.ndarray, dict]:
    files, manifest = acquire_dem_sources(region, dem_dir, offline=offline, refresh=refresh)
    destination = np.full((height, width), np.nan, dtype=np.float32)
    for local in files:
        with rasterio.open(local) as src:
            src_bounds_wgs84 = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
            overlap = (
                max(region.west, src_bounds_wgs84[0]),
                max(region.south, src_bounds_wgs84[1]),
                min(region.east, src_bounds_wgs84[2]),
                min(region.north, src_bounds_wgs84[3]),
            )
            if overlap[0] >= overlap[2] or overlap[1] >= overlap[3]:
                continue
            overlap_src = transform_bounds("EPSG:4326", src.crs, *overlap, densify_pts=21)
            win = window_from_bounds(*overlap_src, transform=src.transform).round_offsets().round_lengths()
            arr = src.read(1, window=win, boundless=True, masked=True)
            src_transform = src.window_transform(win)
            temp = np.full((height, width), np.nan, dtype=np.float32)
            reproject(
                source=np.ma.filled(arr, np.nan).astype(np.float32), destination=temp,
                src_transform=src_transform, src_crs=src.crs,
                dst_transform=transform, dst_crs=region.crs,
                src_nodata=np.nan, dst_nodata=np.nan, resampling=Resampling.bilinear,
            )
            mask = np.isfinite(temp)
            destination[mask] = temp[mask]
    if not np.isfinite(destination).any():
        raise RuntimeError("DEM source files contain no valid pixels for the requested region.")
    valid = np.isfinite(destination)
    if not valid.all():
        from scipy.ndimage import distance_transform_edt
        indices = distance_transform_edt(~valid, return_distances=False, return_indices=True)
        destination = destination[tuple(indices)]
    return destination, manifest


def terrain_derivatives(dem: np.ndarray, cell_size_m: float):
    gy, gx = np.gradient(dem.astype(np.float64), cell_size_m, cell_size_m)
    slope_deg = np.degrees(np.arctan(np.hypot(gx, gy))).astype(np.float32)
    aspect = (np.degrees(np.arctan2(-gx, gy)) + 360.0) % 360.0
    return slope_deg, aspect.astype(np.float32)


CLIMATE_COLUMNS = [
    "time", "temperature_2m", "relative_humidity_2m", "wind_speed_10m", "wind_direction_10m"
]


def annual_date_chunks(start_date: str, end_date: str) -> Iterator[tuple[str, str]]:
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    if start > end:
        raise ValueError("start-date must be earlier than or equal to end-date")
    cursor = start
    while cursor <= end:
        chunk_end = min(end, pd.Timestamp(year=cursor.year, month=12, day=31))
        yield cursor.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")
        cursor = chunk_end + pd.Timedelta(days=1)


def validate_climate_chunk(df: pd.DataFrame, start_date: str, end_date: str,
                           lat: float, lon: float) -> pd.DataFrame:
    required = set(CLIMATE_COLUMNS)
    missing_cols = sorted(required - set(df.columns))
    if missing_cols:
        raise RuntimeError(f"Climate response missing columns {missing_cols} for {lat},{lon}")
    out = df[CLIMATE_COLUMNS].copy()
    out["time"] = pd.to_datetime(out["time"], utc=True)
    expected = pd.date_range(
        pd.Timestamp(start_date, tz="UTC"),
        pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(hours=23),
        freq="h",
    )
    if out["time"].duplicated().any():
        raise RuntimeError(f"Duplicate climate timestamps for {lat},{lon}, {start_date}..{end_date}")
    actual = pd.DatetimeIndex(out["time"])
    missing_times = expected.difference(actual)
    extra_times = actual.difference(expected)
    value_cols = CLIMATE_COLUMNS[1:]
    null_counts = out[value_cols].isna().sum()
    if len(out) != len(expected) or len(missing_times) or len(extra_times) or int(null_counts.sum()) > 0:
        raise RuntimeError(
            f"Incomplete climate chunk for {lat},{lon}, {start_date}..{end_date}: "
            f"expected_hours={len(expected)}, actual_rows={len(out)}, "
            f"missing_hours={len(missing_times)}, extra_hours={len(extra_times)}, "
            f"null_values={int(null_counts.sum())}, "
            f"nulls_by_column={{{', '.join(f'{k}: {int(v)}' for k, v in null_counts.items() if int(v) > 0)}}}"
        )
    return out.sort_values("time").reset_index(drop=True)


def openmeteo_chunk(lat: float, lon: float, start_date: str, end_date: str,
                     retries: int = 5, timeout: int = 180,
                     model: str = "era5", fallback_model: str | None = "era5") -> tuple[pd.DataFrame, str]:
    """Download and validate one climate chunk.

    ERA5 is the default because all four required fields are consistently
    available for multi-decadal requests. If another model is selected and
    returns structurally complete timestamps but null required variables, the
    configured fallback model is tried before repeating transient HTTP retries.
    """
    url = "https://archive-api.open-meteo.com/v1/archive"
    models = []
    for candidate in (model, fallback_model):
        if candidate and candidate not in models:
            models.append(candidate)
    last_exc = None
    for selected_model in models:
        params = {
            "latitude": f"{lat:.6f}", "longitude": f"{lon:.6f}",
            "start_date": start_date, "end_date": end_date,
            "hourly": ",".join(CLIMATE_COLUMNS[1:]),
            "wind_speed_unit": "ms", "timezone": "UTC",
            "models": selected_model,
        }
        for attempt in range(1, retries + 1):
            try:
                response = requests.get(url, params=params, timeout=timeout)
                response.raise_for_status()
                payload = response.json()
                if payload.get("error"):
                    raise RuntimeError(payload.get("reason", "Open-Meteo API error"))
                hourly = payload.get("hourly", {})
                df = pd.DataFrame(hourly)
                if df.empty:
                    raise RuntimeError(f"No Open-Meteo data for {lat},{lon} using model={selected_model}")
                validated = validate_climate_chunk(df, start_date, end_date, lat, lon)
                return validated, selected_model
            except Exception as exc:
                last_exc = exc
                # Null-valued model fields are deterministic for a model/chunk;
                # switch immediately to the fallback rather than retrying it.
                deterministic_nulls = "null_values=" in str(exc) and "null_values=0" not in str(exc)
                if deterministic_nulls:
                    CONSOLE.print(
                        f"[yellow]Climate model {selected_model} returned unavailable/null required fields for "
                        f"{lat:.6f},{lon:.6f} {start_date}..{end_date}; trying fallback model.[/]"
                    )
                    break
                if attempt == retries:
                    break
                delay = min(60.0, 2.0 ** (attempt - 1))
                CONSOLE.print(
                    f"[yellow]Climate chunk retry {attempt}/{retries} using {selected_model} for "
                    f"{lat:.6f},{lon:.6f} {start_date}..{end_date}: {exc}[/]"
                )
                time.sleep(delay)
    raise RuntimeError(
        f"Climate download failed for all configured models {models} after up to {retries} attempts each "
        f"for {lat},{lon}, {start_date}..{end_date}: {last_exc}"
    )


def climate_cache_name(point_index: int, lat: float, lon: float,
                       start_date: str, end_date: str, model: str) -> str:
    safe_model = str(model).lower().replace("-", "_").replace(" ", "_")
    return (
        f"point_{point_index:03d}_lat_{lat:+.6f}_lon_{lon:+.6f}_"
        f"{start_date}_{end_date}_{safe_model}.csv.gz"
    ).replace("+", "p").replace("-", "m")


def fetch_climate_samples(region: Region, start_date: str, end_date: str,
                          climate_dir: Path, grid_n: int = 3,
                          offline: bool = False, refresh: bool = False,
                          retries: int = 5, timeout: int = 180,
                          model: str = "era5", fallback_model: str | None = "era5") -> tuple[pd.DataFrame, dict]:
    """Download annual, validated chunks and persist every raw subset for offline reuse."""
    climate_dir.mkdir(parents=True, exist_ok=True)
    lats = np.linspace(region.south, region.north, grid_n)
    lons = np.linspace(region.west, region.east, grid_n)
    chunks = list(annual_date_chunks(start_date, end_date))
    frames, entries = [], []
    point_index = 0
    for lat in lats:
        for lon in lons:
            point_frames = []
            for chunk_start, chunk_end in chunks:
                requested_name = climate_cache_name(
                    point_index, float(lat), float(lon), chunk_start, chunk_end, model
                )
                local = climate_dir / requested_name
                selected_model = model
                if local.exists() and not refresh:
                    chunk = pd.read_csv(local)
                    chunk = validate_climate_chunk(chunk, chunk_start, chunk_end, float(lat), float(lon))
                    mode = "offline-cache"
                else:
                    if offline:
                        # In offline mode, accept a cached fallback-model chunk as well.
                        fallback_local = climate_dir / climate_cache_name(
                            point_index, float(lat), float(lon), chunk_start, chunk_end,
                            fallback_model or model
                        )
                        if fallback_local.exists() and not refresh:
                            local = fallback_local
                            selected_model = fallback_model or model
                            chunk = pd.read_csv(local)
                            chunk = validate_climate_chunk(
                                chunk, chunk_start, chunk_end, float(lat), float(lon)
                            )
                            mode = "offline-cache"
                        else:
                            raise FileNotFoundError(
                                f"Climate offline chunk missing for model={model} (and fallback={fallback_model}): "
                                f"{local}. Run once online first."
                            )
                    else:
                        chunk, selected_model = openmeteo_chunk(
                            float(lat), float(lon), chunk_start, chunk_end,
                            retries=retries, timeout=timeout,
                            model=model, fallback_model=fallback_model,
                        )
                        local = climate_dir / climate_cache_name(
                            point_index, float(lat), float(lon), chunk_start, chunk_end, selected_model
                        )
                        chunk.to_csv(local, index=False, compression="gzip")
                        mode = "downloaded"
                entries.append({
                    "point_index": point_index, "latitude": float(lat), "longitude": float(lon),
                    "start_date": chunk_start, "end_date": chunk_end, "file": local.name,
                    "requested_model": model, "selected_model": selected_model,
                    "expected_hours": int(len(chunk)), "actual_hours": int(len(chunk)),
                    "size_bytes": local.stat().st_size, "sha256": sha256_file(local), "mode": mode,
                })
                point_frames.append(chunk)
                time.sleep(0.1 if mode == "downloaded" else 0.0)
            point_df = pd.concat(point_frames, ignore_index=True)
            point_df["sample_lat"] = float(lat)
            point_df["sample_lon"] = float(lon)
            theta = np.radians(point_df["wind_direction_10m"].astype(float))
            speed = point_df["wind_speed_10m"].astype(float)
            point_df["wind_u_east_ms"] = -speed * np.sin(theta)
            point_df["wind_v_north_ms"] = -speed * np.cos(theta)
            frames.append(point_df)
            point_index += 1
    combined = pd.concat(frames, ignore_index=True)
    expected_total = sum(x["expected_hours"] for x in entries)
    if len(combined) != expected_total:
        raise RuntimeError(
            f"Climate total validation failed: expected {expected_total} rows, got {len(combined)}"
        )
    manifest = {
        "source": "Open-Meteo Historical Weather API",
        "requested_model": model, "fallback_model": fallback_model,
        "models_used": sorted({x["selected_model"] for x in entries}),
        "timezone": "UTC", "chunking": "calendar-year",
        "requested_period": [start_date, end_date], "grid_n": int(grid_n),
        "point_count": int(grid_n * grid_n), "chunk_count": len(entries),
        "validated_total_hours": int(len(combined)), "chunks": entries,
        "offline_ready": True,
    }
    (climate_dir / "climate_sources_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return combined, manifest


def aggregate_time_bins(samples: pd.DataFrame) -> pd.DataFrame:
    bins = pd.cut(
        samples["time"].dt.hour,
        bins=[-1, 5, 11, 17, 23],
        labels=["00_06", "06_12", "12_18", "18_24"],
    )
    samples = samples.copy()
    samples["time_bin_utc"] = bins.astype(str)
    agg = samples.groupby(
        ["sample_lat", "sample_lon", "time_bin_utc"], observed=True
    ).agg(
        temperature_c=("temperature_2m", "mean"),
        relative_humidity_percent=("relative_humidity_2m", "mean"),
        wind_u_east_ms=("wind_u_east_ms", "mean"),
        wind_v_north_ms=("wind_v_north_ms", "mean"),
        wind_scalar_speed_ms=("wind_speed_10m", "mean"),
    ).reset_index()
    agg["wind_vector_speed_ms"] = np.hypot(
        agg["wind_u_east_ms"], agg["wind_v_north_ms"]
    )
    agg["wind_direction_from_deg"] = (
        np.degrees(np.arctan2(-agg["wind_u_east_ms"], -agg["wind_v_north_ms"])) + 360
    ) % 360
    return agg


def idw_grid(points_xy: np.ndarray, values: np.ndarray,
             grid_x: np.ndarray, grid_y: np.ndarray, power=2.0) -> np.ndarray:
    out = np.zeros_like(grid_x, dtype=np.float64)
    weight_sum = np.zeros_like(grid_x, dtype=np.float64)
    for (px, py), value in zip(points_xy, values):
        dist2 = (grid_x - px) ** 2 + (grid_y - py) ** 2
        weight = 1.0 / np.maximum(dist2, 1.0) ** (power / 2.0)
        out += weight * value
        weight_sum += weight
    return (out / weight_sum).astype(np.float32)


def create_climate_raster(agg: pd.DataFrame, region: Region, width: int, height: int,
                          transform, requested_clusters: int):
    transformer = Transformer.from_crs("EPSG:4326", region.crs, always_xy=True)
    xs, ys = transformer.transform(agg["sample_lon"].to_numpy(), agg["sample_lat"].to_numpy())
    agg = agg.copy()
    agg["x"] = xs
    agg["y"] = ys

    feature_cols = [
        "temperature_c", "relative_humidity_percent",
        "wind_u_east_ms", "wind_v_north_ms"
    ]
    scaler = StandardScaler()
    z = scaler.fit_transform(agg[feature_cols])
    n_clusters = max(1, min(requested_clusters, len(agg)))
    model = MiniBatchKMeans(
        n_clusters=n_clusters, random_state=42, n_init=20, batch_size=128
    )
    labels = model.fit_predict(z)
    distances = model.transform(z).min(axis=1)
    agg["cluster_id"] = labels
    agg["cluster_confidence"] = np.exp(-distances)

    centers = scaler.inverse_transform(model.cluster_centers_)
    centroid_df = pd.DataFrame(centers, columns=feature_cols)
    centroid_df.insert(0, "cluster_id", np.arange(n_clusters))
    centroid_df["wind_vector_speed_ms"] = np.hypot(
        centroid_df["wind_u_east_ms"], centroid_df["wind_v_north_ms"]
    )
    centroid_df["wind_direction_from_deg"] = (
        np.degrees(np.arctan2(
            -centroid_df["wind_u_east_ms"], -centroid_df["wind_v_north_ms"]
        )) + 360
    ) % 360

    cols = np.arange(width) + 0.5
    rows = np.arange(height) + 0.5
    gx = transform.c + cols * transform.a
    gy = transform.f + rows * transform.e
    grid_x, grid_y = np.meshgrid(gx, gy)

    time_bins = ["00_06", "06_12", "12_18", "18_24"]
    bands = []
    names = []
    for time_bin in time_bins:
        subset = agg[agg["time_bin_utc"] == time_bin]
        pts = subset[["x", "y"]].to_numpy()
        for col, name in [
            ("temperature_c", f"temperature_c_{time_bin}"),
            ("relative_humidity_percent", f"relative_humidity_percent_{time_bin}"),
            ("wind_u_east_ms", f"wind_u_east_ms_{time_bin}"),
            ("wind_v_north_ms", f"wind_v_north_ms_{time_bin}"),
            ("wind_vector_speed_ms", f"wind_vector_speed_ms_{time_bin}"),
            ("wind_direction_from_deg", f"wind_direction_from_deg_{time_bin}"),
        ]:
            bands.append(idw_grid(pts, subset[col].to_numpy(), grid_x, grid_y))
            names.append(name)

        # categorical cluster is assigned from nearest sample (IDW is unsuitable for IDs)
        labels_grid = np.empty((height, width), dtype=np.float32)
        conf_grid = np.empty((height, width), dtype=np.float32)
        px = pts[:, 0][:, None, None]
        py = pts[:, 1][:, None, None]
        d2 = (grid_x[None, :, :] - px) ** 2 + (grid_y[None, :, :] - py) ** 2
        nearest = np.argmin(d2, axis=0)
        labels_grid[:] = subset["cluster_id"].to_numpy()[nearest]
        conf_grid[:] = subset["cluster_confidence"].to_numpy()[nearest]
        bands += [labels_grid, conf_grid]
        names += [f"climate_cluster_id_{time_bin}", f"cluster_confidence_{time_bin}"]

    return np.stack(bands).astype(np.float32), names, centroid_df, agg


def write_cog(path: Path, array: np.ndarray, transform, crs, names: list[str],
              nodata=-9999.0):
    profile = {
        "driver": "GTiff",
        "height": array.shape[1],
        "width": array.shape[2],
        "count": array.shape[0],
        "dtype": "float32",
        "crs": crs,
        "transform": transform,
        "nodata": nodata,
        "compress": "DEFLATE",
        "predictor": 3,
        "tiled": True,
        "blockxsize": valid_tiff_block_size(array.shape[2]),
        "blockysize": valid_tiff_block_size(array.shape[1]),
        "BIGTIFF": "IF_SAFER",
    }
    with rasterio.open(path, "w", **profile) as dst:
        for i in range(array.shape[0]):
            band = np.where(np.isfinite(array[i]), array[i], nodata).astype(np.float32)
            dst.write(band, i + 1)
            dst.set_band_description(i + 1, names[i])
            dst.update_tags(i + 1, semantic_name=names[i])
        factors = [2, 4, 8, 16]
        factors = [f for f in factors if min(array.shape[1:]) // f >= 16]
        if factors:
            dst.build_overviews(factors, Resampling.nearest)
            dst.update_tags(ns="rio_overview", resampling="nearest")


def save_vectors(path: Path, layers: dict[str, gpd.GeoDataFrame]):
    if path.exists():
        path.unlink()
    for name, gdf in layers.items():
        if gdf.empty:
            continue
        # GeoPackage cannot reliably store arbitrary list/dict columns.
        safe = gdf.copy()
        safe = safe[[c for c in safe.columns if c == "geometry" or safe[c].dtype != "object"] +
                    [c for c in safe.columns if c != "geometry" and safe[c].dtype == "object"]]
        for col in safe.columns:
            if col != "geometry" and safe[col].dtype == "object":
                safe[col] = safe[col].astype(str).str.slice(0, 500)
        safe.to_file(path, layer=name, driver="GPKG")



EMAG2_ANOMALY_URL = "https://www.ngdc.noaa.gov/geomag/data/EMAG2/EMAG2_V3_UpCont_DataTiff.tif"
EMAG2_ERROR_URL = "https://www.ngdc.noaa.gov/geomag/data/EMAG2/EMAG2_V3_Error_DataTiff.tif"
EMAG2_CODE_URL = "https://www.ngdc.noaa.gov/geomag/data/EMAG2/EMAG2_V3_Code.tif"


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _materialize_magnetic_source(
    source: str | None,
    magnetic_dir: Path,
    *,
    offline: bool = False,
    refresh: bool = False,
) -> Path | None:
    """Create a persistent local copy of a magnetic source and return its path.

    HTTP(S) sources are always downloaded completely before Rasterio opens them.
    Consequently, all cropping and reprojection are performed from local files,
    and later runs can be fully offline. Local input files are copied into the
    magnetic directory as well, making the generated dataset self-contained.
    """
    if not source:
        return None

    magnetic_dir.mkdir(parents=True, exist_ok=True)
    local_candidate = Path(source).expanduser()
    if local_candidate.exists():
        target = magnetic_dir / local_candidate.name
        if local_candidate.resolve() != target.resolve():
            if refresh or not target.exists() or target.stat().st_size != local_candidate.stat().st_size:
                temp = target.with_suffix(target.suffix + ".part")
                temp.unlink(missing_ok=True)
                shutil.copy2(local_candidate, temp)
                temp.replace(target)
        else:
            target = local_candidate.resolve()
    else:
        filename = source.rsplit("/", 1)[-1] or "magnetic_source.tif"
        target = magnetic_dir / filename
        if refresh:
            target.unlink(missing_ok=True)
        if not target.exists() or target.stat().st_size == 0:
            if offline:
                raise FileNotFoundError(
                    f"Offline magnetic mode is enabled, but {target} does not exist. "
                    "Run once without --magnetic-offline to download the complete EMAG2 files."
                )
            temp = target.with_suffix(target.suffix + ".part")
            temp.unlink(missing_ok=True)
            CONSOLE.print(f"[cyan]Downloading complete magnetic source for offline use:[/] {source}")
            with requests.get(source, stream=True, timeout=(30, 1800)) as response:
                response.raise_for_status()
                expected = int(response.headers.get("content-length", 0) or 0)
                received = 0
                with temp.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                        if chunk:
                            handle.write(chunk)
                            received += len(chunk)
                if expected and received != expected:
                    temp.unlink(missing_ok=True)
                    raise IOError(
                        f"Incomplete magnetic download for {source}: expected {expected} bytes, received {received}."
                    )
            temp.replace(target)

    if not target.exists() or target.stat().st_size == 0:
        raise IOError(f"Magnetic source was not materialized correctly: {target}")
    try:
        with rasterio.open(target) as ds:
            if ds.width <= 0 or ds.height <= 0 or ds.count < 1:
                raise ValueError("invalid raster dimensions")
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise IOError(f"Downloaded magnetic source is not a readable raster: {target}: {exc}") from exc
    return target.resolve()


def _write_magnetic_manifest(
    magnetic_dir: Path,
    entries: list[tuple[str, str | None, Path | None]],
    source_crs_fallback: str,
) -> Path:
    records = []
    for role, source, local in entries:
        if local is None:
            continue
        records.append({
            "role": role,
            "source": source,
            "local_file": local.name,
            "absolute_path_at_build_time": str(local),
            "size_bytes": local.stat().st_size,
            "sha256": _sha256_file(local),
        })
    manifest = {
        "title": "Offline NOAA EMAG2 magnetic source bundle",
        "usage": "All magnetic processing is performed from these local files; network access is not used after materialization.",
        "source_crs_fallback": source_crs_fallback,
        "files": records,
    }
    path = magnetic_dir / "magnetic_sources_manifest.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def _read_local_magnetic_grid(
    local_path: Path, region: Region, width: int, height: int,
    dst_transform, dst_crs, resampling: Resampling,
    source_crs_fallback: str = "EPSG:4326",
) -> np.ndarray:
    with rasterio.open(local_path) as src:
        return _crop_reproject_magnetic(
            src, region, width, height, dst_transform, dst_crs, resampling,
            source_crs_fallback=source_crs_fallback
        )


def _crop_reproject_magnetic(src, region: Region, width: int, height: int, dst_transform, dst_crs,
                              resampling: Resampling, source_crs_fallback: str = "EPSG:4326") -> np.ndarray:
    """Crop and reproject an EMAG2-style grid.

    NOAA's EMAG2 GeoTIFF variants may contain a valid geographic affine
    transform but omit the GeoTIFF CRS tag. In that documented case the grid
    coordinates are longitude/latitude, so EPSG:4326 is used as an explicit
    fallback. The fallback is configurable and is never written back to the
    source file.
    """
    src_crs = src.crs
    if src_crs is None:
        if not source_crs_fallback:
            raise ValueError(
                f"Magnetic source {src.name!r} has no CRS metadata. "
                "Provide --magnetic-source-crs (normally EPSG:4326 for EMAG2)."
            )
        src_crs = CRS.from_user_input(source_crs_fallback)
        CONSOLE.print(
            f"[yellow]Magnetic source has no embedded CRS; using configured fallback {src_crs}.[/]"
        )

    overlap_wgs84 = [region.west, region.south, region.east, region.north]
    src_bounds = transform_bounds("EPSG:4326", src_crs, *overlap_wgs84, densify_pts=21)

    # EMAG2 is distributed on a 0..360 longitude grid. A WGS84 request normally
    # uses -180..180, so western-hemisphere longitudes must be wrapped before
    # calculating the source window. This is independent of the missing-CRS fix.
    source_uses_360 = False
    longitude_shift = 0.0
    if getattr(src_crs, "is_geographic", False):
        # EMAG2's first pixel edge is slightly negative (-1/60 degree), so a
        # test such as bounds.left >= 0 incorrectly classifies the grid as a
        # conventional -180..180 raster. Detect the longitude convention from
        # the eastern bound and the near-global span instead.
        source_span_x = float(src.bounds.right - src.bounds.left)
        source_uses_360 = (
            src.bounds.right > 180.0 and
            src.bounds.left > -1.0 and
            source_span_x > 300.0
        )
        if source_uses_360:
            west, south, east, north = src_bounds
            west = west % 360.0
            east = east % 360.0
            # Small regional requests do not cross the antimeridian. If modulo
            # wrapping reverses the order, retain a continuous interval.
            if east <= west:
                east += 360.0
            src_bounds = (west, south, east, north)
            # Rasterio/PROJ expects EPSG:4326 longitudes in -180..180 during
            # reprojection. The source window is read at 0..360 coordinates,
            # but its affine transform must be shifted back by 360 degrees for
            # western-hemisphere windows before reprojecting to the destination.
            if west > 180.0:
                longitude_shift = -360.0

    # Intersect requested bounds with the actual source extent. This prevents
    # invalid windows and produces a clear error for an unrelated source file.
    left = max(src_bounds[0], src.bounds.left)
    bottom = max(src_bounds[1], src.bounds.bottom)
    right = min(src_bounds[2], src.bounds.right)
    top = min(src_bounds[3], src.bounds.top)
    if left >= right or bottom >= top:
        raise ValueError(
            f"Requested region does not intersect magnetic source bounds. "
            f"requested_in_source_crs={src_bounds}, source_bounds={tuple(src.bounds)}"
        )

    raw_win = window_from_bounds(left, bottom, right, top, transform=src.transform)
    # A 3 km request can be smaller than one 2-arc-minute EMAG2 cell. Expand
    # to at least one cell and add a one-cell interpolation margin.
    col0 = max(0, int(math.floor(raw_win.col_off)) - 1)
    row0 = max(0, int(math.floor(raw_win.row_off)) - 1)
    col1 = min(src.width, int(math.ceil(raw_win.col_off + raw_win.width)) + 1)
    row1 = min(src.height, int(math.ceil(raw_win.row_off + raw_win.height)) + 1)
    if col1 <= col0:
        col1 = min(src.width, col0 + 1)
    if row1 <= row0:
        row1 = min(src.height, row0 + 1)
    win = Window(col0, row0, col1 - col0, row1 - row0)
    arr = src.read(1, window=win, boundless=False, masked=True)
    if arr.size == 0:
        raise ValueError("Magnetic source intersection produced an empty raster window.")
    src_transform = src.window_transform(win)
    if longitude_shift:
        src_transform = rasterio.Affine(
            src_transform.a, src_transform.b, src_transform.c + longitude_shift,
            src_transform.d, src_transform.e, src_transform.f,
        )
    src_nodata = src.nodata
    filled = np.ma.filled(arr, np.nan).astype(np.float32)
    if src_nodata is not None:
        filled[np.isclose(filled, src_nodata)] = np.nan
    filled[np.isclose(filled, 99999.0)] = np.nan
    destination = np.full((height, width), np.nan, dtype=np.float32)
    reproject(
        source=filled, destination=destination,
        src_transform=src_transform, src_crs=src_crs,
        dst_transform=dst_transform, dst_crs=dst_crs,
        src_nodata=np.nan, dst_nodata=np.nan, resampling=resampling,
    )
    if not np.isfinite(destination).any():
        raise ValueError(
            "Magnetic grid was read but no valid values reached the requested output region. "
            "Check the source CRS, nodata value and geographic coverage."
        )
    return destination


def fetch_magnetic_anomaly(
    region: Region, width: int, height: int, transform, magnetic_dir: Path,
    anomaly_url: str, error_url: str | None, code_url: str | None,
    source_crs_fallback: str = "EPSG:4326",
    offline: bool = False, refresh: bool = False,
):
    """Download/copy complete EMAG2 sources, then process strictly offline."""
    magnetic_dir.mkdir(parents=True, exist_ok=True)
    anomaly_local = _materialize_magnetic_source(
        anomaly_url, magnetic_dir, offline=offline, refresh=refresh
    )
    error_local = _materialize_magnetic_source(
        error_url, magnetic_dir, offline=offline, refresh=refresh
    ) if error_url else None
    code_local = _materialize_magnetic_source(
        code_url, magnetic_dir, offline=offline, refresh=refresh
    ) if code_url else None

    manifest_path = _write_magnetic_manifest(
        magnetic_dir,
        [
            ("anomaly", anomaly_url, anomaly_local),
            ("estimated_error", error_url, error_local),
            ("source_code", code_url, code_local),
        ],
        source_crs_fallback,
    )

    anomaly = _read_local_magnetic_grid(
        anomaly_local, region, width, height, transform, region.crs, Resampling.bilinear,
        source_crs_fallback=source_crs_fallback
    )
    error = np.full_like(anomaly, np.nan, dtype=np.float32)
    source_code = np.full_like(anomaly, np.nan, dtype=np.float32)
    if error_local:
        try:
            error = _read_local_magnetic_grid(
                error_local, region, width, height, transform, region.crs, Resampling.bilinear,
                source_crs_fallback=source_crs_fallback
            )
        except Exception as exc:
            CONSOLE.print(f"[yellow]Magnetic error grid unavailable:[/] {exc}")
    if code_local:
        try:
            source_code = _read_local_magnetic_grid(
                code_local, region, width, height, transform, region.crs, Resampling.nearest,
                source_crs_fallback=source_crs_fallback
            )
        except Exception as exc:
            CONSOLE.print(f"[yellow]Magnetic source-code grid unavailable:[/] {exc}")
    gy, gx = np.gradient(anomaly.astype(np.float64), abs(transform.e), abs(transform.a))
    gradient = np.hypot(gx, gy).astype(np.float32)
    stack = np.stack([anomaly, error, source_code, gradient]).astype(np.float32)
    names = [
        "magnetic_anomaly_nt",
        "magnetic_anomaly_error_nt",
        "magnetic_source_code",
        "magnetic_anomaly_gradient_nt_per_m",
    ]
    source_info = {
        "directory": str(magnetic_dir.resolve()),
        "manifest": str(manifest_path.resolve()),
        "anomaly": str(anomaly_local),
        "error": str(error_local) if error_local else None,
        "source_code": str(code_local) if code_local else None,
        "offline_processing": True,
    }
    return stack, names, source_info


def main():
    parser = argparse.ArgumentParser(description="Build a real-data multilayer 2.5D regional map.")
    parser.add_argument("--region-file", default="region/region_v1.geojson",
                        help="WGS 84 GeoJSON FeatureCollection containing boundary, origin and destination.")
    parser.add_argument("--output", default=None, help="Output directory; default: outputs/<region_stem>_map")
    parser.add_argument("--center-lat", type=float, default=None, help="Legacy explicit center override")
    parser.add_argument("--center-lon", type=float, default=None, help="Legacy explicit center override")
    parser.add_argument("--size-m", type=float, default=None,
                        help="Legacy explicit square edge override; otherwise GeoJSON boundary is used")
    parser.add_argument("--epsg", type=int, default=32613)
    parser.add_argument("--resolution-m", type=float, default=10.0)
    parser.add_argument("--start-date", default="2020-05-20")
    parser.add_argument("--end-date", default="2020-05-20")
    parser.add_argument("--climate-grid", type=int, default=3)
    parser.add_argument("--clusters", type=int, default=12)
    parser.add_argument("--offline", action="store_true",
                        help="Forbid all OSM, DEM, climate and magnetic network access; require persisted sources.")
    parser.add_argument("--source-dir", default=None,
                        help="Root for persistent offline source data; default: <output>/offline_sources.")
    parser.add_argument("--osm-offline", action="store_true", help="Use only persisted OSM source layers.")
    parser.add_argument("--overpass-config",
                        default=str(Path(__file__).resolve().parent / "config" / "overpass_endpoints.json"),
                        help="JSON file containing the ordered Overpass failover endpoint pool.")
    parser.add_argument("--overpass-endpoints", default=None,
                        help="Comma-separated Overpass endpoints; overrides --overpass-config.")
    parser.add_argument("--osm-retries-per-endpoint", type=int, default=2,
                        help="Number of round-robin attempts per Overpass endpoint (default 2).")
    parser.add_argument("--osm-timeout", type=int, default=180,
                        help="OSMnx/Overpass request timeout in seconds (default 180).")
    parser.add_argument("--osm-endpoint-cooldown", type=float, default=1.0,
                        help="Delay in seconds after a failed endpoint before failover (default 1).")
    parser.add_argument("--dem-offline", action="store_true", help="Use only persisted complete Copernicus DEM tiles.")
    parser.add_argument("--climate-offline", action="store_true", help="Use only persisted validated climate chunks.")
    parser.add_argument("--refresh-osm", action="store_true", help="Redownload and replace persisted OSM layers.")
    parser.add_argument("--refresh-dem", action="store_true", help="Redownload complete intersecting DEM tiles.")
    parser.add_argument("--refresh-climate", action="store_true", help="Redownload all climate chunks.")
    parser.add_argument("--climate-retries", type=int, default=5,
                        help="Retries for each annual Open-Meteo chunk (default 5).")
    parser.add_argument("--climate-timeout", type=int, default=180,
                        help="HTTP timeout seconds for each annual climate chunk (default 180).")
    parser.add_argument("--climate-model", default="era5",
                        help="Open-Meteo reanalysis model (default era5; complete required fields for long periods).")
    parser.add_argument("--climate-fallback-model", default="era5",
                        help="Fallback model when the requested model returns null required fields (default era5).")
    parser.add_argument("--skip-magnetic", action="store_true",
                        help="Do not collect the EMAG2 magnetic-anomaly layer.")
    parser.add_argument("--magnetic-anomaly-url", default=EMAG2_ANOMALY_URL,
                        help="EMAG2 v3 anomaly GeoTIFF URL or local GeoTIFF path.")
    parser.add_argument("--magnetic-error-url", default=EMAG2_ERROR_URL,
                        help="Optional EMAG2 estimated-error GeoTIFF URL or local path.")
    parser.add_argument("--magnetic-code-url", default=EMAG2_CODE_URL,
                        help="Optional EMAG2 source-code GeoTIFF URL or local path.")
    parser.add_argument("--magnetic-dir", default=None,
                        help="Persistent directory for complete offline EMAG2 source files; default: <output>/magnetic.")
    parser.add_argument("--magnetic-cache-dir", default=None,
                        help="Deprecated alias for --magnetic-dir, retained for backward compatibility.")
    parser.add_argument("--magnetic-offline", action="store_true",
                        help="Forbid network access and require complete EMAG2 source files in the magnetic directory.")
    parser.add_argument("--refresh-magnetic", action="store_true",
                        help="Redownload or recopy complete EMAG2 source files before processing.")
    parser.add_argument("--magnetic-source-crs", default="EPSG:4326",
                        help="Fallback CRS when an EMAG2 GeoTIFF omits CRS metadata (default: EPSG:4326).")
    args = parser.parse_args()

    if args.offline and (args.refresh_osm or args.refresh_dem or args.refresh_climate or args.refresh_magnetic):
        parser.error("--offline cannot be combined with any --refresh-* option")
    if args.osm_offline and args.refresh_osm:
        parser.error("--osm-offline cannot be combined with --refresh-osm")
    if args.dem_offline and args.refresh_dem:
        parser.error("--dem-offline cannot be combined with --refresh-dem")
    if args.climate_offline and args.refresh_climate:
        parser.error("--climate-offline cannot be combined with --refresh-climate")
    if args.magnetic_offline and args.refresh_magnetic:
        parser.error("--magnetic-offline cannot be combined with --refresh-magnetic")
    if args.osm_retries_per_endpoint < 1:
        parser.error("--osm-retries-per-endpoint must be >= 1")
    if args.osm_timeout < 1:
        parser.error("--osm-timeout must be >= 1")
    if args.osm_endpoint_cooldown < 0:
        parser.error("--osm-endpoint-cooldown must be >= 0")
    try:
        overpass_endpoints = load_overpass_endpoints(args.overpass_config, args.overpass_endpoints)
    except Exception as exc:
        parser.error(f"Invalid Overpass endpoint configuration: {exc}")

    region_spec = load_region_geojson(args.region_file)
    out = Path(args.output) if args.output else default_output_dir(region_spec)
    out.mkdir(parents=True, exist_ok=True)
    source_root = Path(args.source_dir) if args.source_dir else (out / "offline_sources")
    osm_dir = source_root / "osm"
    dem_dir = source_root / "dem"
    climate_dir = source_root / "climate"
    source_root.mkdir(parents=True, exist_ok=True)
    osm_offline = args.offline or args.osm_offline
    dem_offline = args.offline or args.dem_offline
    climate_offline = args.offline or args.climate_offline
    magnetic_offline = args.offline or args.magnetic_offline
    explicit_legacy = args.center_lat is not None or args.center_lon is not None or args.size_m is not None
    if explicit_legacy:
        if None in (args.center_lat, args.center_lon, args.size_m):
            parser.error("--center-lat, --center-lon and --size-m must be supplied together")
        region = make_region(args.center_lat, args.center_lon, args.size_m, args.epsg)
        projected_region_geometry = None
        region_spec = None
    else:
        region, projected_region_geometry, region_spec = make_region_from_geojson(args.region_file, args.epsg)
    width = int(math.ceil((region.maxx - region.minx) / args.resolution_m))
    height = int(math.ceil((region.maxy - region.miny) / args.resolution_m))
    transform = from_bounds(region.minx, region.miny, region.maxx, region.maxy, width, height)

    table = Table(title="Requested map")
    table.add_column("Property")
    table.add_column("Value")
    table.add_row("Center", f"{region.center_lat:.6f}, {region.center_lon:.6f}")
    table.add_row("WGS84 bounds", f"{region.west:.6f}, {region.south:.6f}, {region.east:.6f}, {region.north:.6f}")
    table.add_row("Projected bounds", f"{region.minx:.1f}, {region.miny:.1f}, {region.maxx:.1f}, {region.maxy:.1f}")
    table.add_row("Area", f"{region.area_km2:.3f} km²")
    table.add_row("Grid", f"{width} × {height} at {args.resolution_m:g} m")
    CONSOLE.print(table)

    with progress() as p:
        t_osm = p.add_task("Acquiring persistent OSM roads and obstacles", total=1)
        layers, osm_source_info = fetch_or_load_osm(
            region, osm_dir, offline=osm_offline, refresh=args.refresh_osm,
            endpoints=overpass_endpoints,
            retries_per_endpoint=args.osm_retries_per_endpoint,
            timeout=args.osm_timeout,
            endpoint_cooldown=args.osm_endpoint_cooldown,
        )
        p.advance(t_osm)

        t_dem = p.add_task("Acquiring complete Copernicus DEM source tiles", total=1)
        dem, dem_source_info = fetch_dem(
            region, width, height, transform, dem_dir,
            offline=dem_offline, refresh=args.refresh_dem
        )
        p.advance(t_dem)

        magnetic_names = []
        magnetic_source_info = None
        if not args.skip_magnetic:
            t_magnetic = p.add_task("Collecting NOAA EMAG2 magnetic anomaly data", total=1)
            magnetic_dir_arg = args.magnetic_dir or args.magnetic_cache_dir
            magnetic_dir = Path(magnetic_dir_arg) if magnetic_dir_arg else (out / "magnetic")
            magnetic_stack, magnetic_names, magnetic_source_info = fetch_magnetic_anomaly(
                region, width, height, transform, magnetic_dir,
                args.magnetic_anomaly_url, args.magnetic_error_url, args.magnetic_code_url,
                source_crs_fallback=args.magnetic_source_crs,
                offline=magnetic_offline, refresh=args.refresh_magnetic
            )
            magnetic_stack = apply_region_mask(magnetic_stack, projected_region_geometry, transform)
            write_cog(out / "magnetic_anomaly.tif", magnetic_stack, transform, region.crs, magnetic_names)
            p.advance(t_magnetic)

        t_terrain = p.add_task("Computing slope/aspect and rasterizing obstacles", total=7)
        slope, aspect = terrain_derivatives(dem, args.resolution_m)
        p.advance(t_terrain)

        roads = rasterize(
            geom_shapes(
                layers["roads"],
                lambda r: ROAD_CODES.get(str(r.get("highway")), 1),
                buffer_m=max(2.5, args.resolution_m / 2),
            ),
            out_shape=(height, width), transform=transform, fill=0, dtype="float32"
        )
        p.advance(t_terrain)

        building_mask = rasterize(
            geom_shapes(layers["buildings"], lambda r: 1),
            out_shape=(height, width), transform=transform, fill=0, dtype="float32"
        )
        p.advance(t_terrain)

        building_height = rasterize(
            geom_shapes(layers["buildings"], infer_building_height),
            out_shape=(height, width), transform=transform, fill=0, dtype="float32"
        )
        p.advance(t_terrain)

        natural_shapes = []
        for _, r in layers["water"].iterrows():
            natural_shapes.append((r.geometry, 1))
        for _, r in layers["natural"].iterrows():
            tag = str(r.get("natural", r.get("landuse", "")))
            natural_shapes.append((r.geometry, NATURAL_CODES.get(tag, 3)))
        natural_obstacle = rasterize(
            natural_shapes, out_shape=(height, width), transform=transform,
            fill=0, dtype="float32"
        )
        p.advance(t_terrain)

        artificial_shapes = []
        for _, r in layers["artificial"].iterrows():
            if str(r.get("power", "")) in ("tower", "pole"):
                val = ARTIFICIAL_CODES["power_tower" if str(r.get("power")) == "tower" else "power_pole"]
            elif str(r.get("power", "")) in ("line", "minor_line"):
                val = ARTIFICIAL_CODES["power_line"]
                geom = r.geometry.buffer(max(3.0, args.resolution_m / 2))
                artificial_shapes.append((geom, val))
                continue
            else:
                val = ARTIFICIAL_CODES.get(str(r.get("man_made", "")), 1)
            geom = r.geometry
            if geom.geom_type in ("Point", "MultiPoint", "LineString", "MultiLineString"):
                geom = geom.buffer(max(3.0, args.resolution_m / 2))
            artificial_shapes.append((geom, val))
        artificial_obstacle = rasterize(
            artificial_shapes, out_shape=(height, width), transform=transform,
            fill=0, dtype="float32"
        )
        p.advance(t_terrain)

        terrain_stack = np.stack([
            dem, slope, aspect, roads, building_mask, building_height,
            natural_obstacle, artificial_obstacle
        ]).astype(np.float32)
        terrain_names = [
            "terrain_elevation_m",
            "terrain_slope_deg",
            "terrain_aspect_deg",
            "road_class",
            "building_mask",
            "building_height_m",
            "natural_obstacle_class",
            "artificial_obstacle_class",
        ]
        terrain_stack = apply_region_mask(terrain_stack, projected_region_geometry, transform)
        write_cog(out / "terrain_obstacles.tif", terrain_stack, transform, region.crs, terrain_names)
        p.advance(t_terrain)

        t_climate = p.add_task("Downloading historical climate samples", total=2)
        samples, climate_source_info = fetch_climate_samples(
            region, args.start_date, args.end_date, climate_dir,
            grid_n=args.climate_grid, offline=climate_offline,
            refresh=args.refresh_climate, retries=args.climate_retries,
            timeout=args.climate_timeout,
            model=args.climate_model,
            fallback_model=args.climate_fallback_model or None,
        )
        p.advance(t_climate)
        agg = aggregate_time_bins(samples)
        climate_stack, climate_names, centroids, assignments = create_climate_raster(
            agg, region, width, height, transform, args.clusters
        )
        climate_stack = apply_region_mask(climate_stack, projected_region_geometry, transform)
        write_cog(out / "climate_contexts.tif", climate_stack, transform, region.crs, climate_names)
        p.advance(t_climate)

        t_write = p.add_task("Writing vectors, tables and metadata", total=4)
        save_vectors(out / "vectors.gpkg", layers)
        p.advance(t_write)
        samples.to_csv(out / "climate_samples.csv.gz", index=False, compression="gzip")
        assignments.to_csv(out / "climate_assignments.csv", index=False)
        centroids.to_csv(out / "climate_centroids.csv", index=False)
        p.advance(t_write)

        metadata = {
            "title": "Boulder foothills real-data multilayer 2.5D map",
            "center_wgs84": [region.center_lon, region.center_lat],
            "bounds_wgs84": [region.west, region.south, region.east, region.north],
            "bounds_projected": [region.minx, region.miny, region.maxx, region.maxy],
            "crs": region.crs.to_string(),
            "size_m": region.size_m,
            "area_km2": region.area_km2,
            "resolution_m": args.resolution_m,
            "width": width,
            "height": height,
            "historical_period_utc": [args.start_date, args.end_date],
            "sources": {
                "roads_and_obstacles": "OpenStreetMap / Overpass",
                "elevation": "Copernicus DEM GLO-30 via Microsoft Planetary Computer",
                "climate": "Open-Meteo Historical Weather API",
                "magnetic_anomaly": "NOAA NCEI EMAG2 v3 upward-continued anomaly grid (4 km altitude)",
            },
            "terrain_bands": terrain_names,
            "climate_bands": climate_names,
            "magnetic_bands": magnetic_names,
            "magnetic_offline_sources": magnetic_source_info,
            "osm_offline_sources": osm_source_info,
            "dem_offline_sources": dem_source_info,
            "climate_offline_sources": climate_source_info,
            "offline_source_root": str(source_root.resolve()),
            "limitations": [
                "OpenStreetMap completeness varies by location.",
                "Copernicus GLO-30 is about 30 m source resolution even if output is sampled to 10 m.",
                "Open-Meteo reanalysis resolution is coarser than this 3 km region; fine spatial climate differences are interpolated, not directly observed.",
                "Building heights may be inferred from levels or assigned a conservative default.",
                "EMAG2 has 2-arc-minute source resolution and is resampled to the output grid; it is not a local magnetometer survey.",
                "The EMAG2 upward-continued anomaly is referenced at 4 km above the WGS84 ellipsoid and NOAA marks the product as unsuitable for operational navigation.",
                "This package is not certified for safety-critical navigation.",
            ],
        }
        (out / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        p.advance(t_write)

        readme = f"""# Generated multilayer regional map

- Center: {region.center_lat:.6f} N, {region.center_lon:.6f} E
- WGS84 bounds: west={region.west:.6f}, south={region.south:.6f},
  east={region.east:.6f}, north={region.north:.6f}
- Projected CRS: {region.crs.to_string()}
- Nominal extent: {region.size_m:.0f} m × {region.size_m:.0f} m
- Area: {region.area_km2:.3f} km²
- Output grid: {width} × {height} cells at {args.resolution_m:g} m
- Historical climate period: {args.start_date} to {args.end_date} UTC

See metadata.json for band definitions and limitations. The optional `magnetic_anomaly.tif` contains EMAG2 anomaly, error, source code and horizontal gradient bands. Complete offline EMAG2 source files and their SHA-256 manifest are stored in the `magnetic/` directory. Persisted OSM layers, complete intersecting Copernicus DEM tiles, and validated annual Open-Meteo chunks are stored under `offline_sources/`, enabling a complete rerun with `--offline`.
"""
        (out / "README_generated.md").write_text(readme, encoding="utf-8")
        p.advance(t_write)

    CONSOLE.print("[bold green]Build completed.[/]")
    CONSOLE.print(f"Output directory: [cyan]{out.resolve()}[/]")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        CONSOLE.print("[red]Cancelled by user.[/]")
        sys.exit(130)
    except Exception as exc:
        CONSOLE.print_exception()
        CONSOLE.print(f"[bold red]Build failed:[/] {exc}")
        sys.exit(1)
