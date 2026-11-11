#!/usr/bin/env python3
"""Shared RFC 7946 / WGS 84 GeoJSON region loader for all package scripts."""
from __future__ import annotations
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

DEFAULT_REGION_FILE = Path("region/region_v1.geojson")

@dataclass(frozen=True)
class GeoJSONRegion:
    path: Path
    geometry: dict[str, Any]
    west: float
    south: float
    east: float
    north: float
    center_lon: float
    center_lat: float
    origin: tuple[float, float] | None
    destination: tuple[float, float] | None
    name: str
    origin_properties: dict[str, Any]
    destination_properties: dict[str, Any]
    boundary_properties: dict[str, Any]
    sha256: str
    source_object: dict[str, Any]

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return self.west, self.south, self.east, self.north

    def boundary_rings(self) -> list[list[tuple[float, float]]]:
        return geometry_rings(self.geometry)

    def provenance(self) -> dict[str, Any]:
        return {
            "region_file": str(self.path),
            "region_file_sha256": self.sha256,
            "region_name": self.name,
            "crs": "OGC:CRS84 / WGS 84 longitude-latitude",
            "epsg": 4326,
            "boundary_geometry": self.geometry,
            "bounds_wgs84": list(self.bounds),
            "origin_wgs84": list(self.origin) if self.origin else None,
            "destination_wgs84": list(self.destination) if self.destination else None,
        }


def _walk_coords(coords):
    if isinstance(coords, (list, tuple)) and len(coords) >= 2 and isinstance(coords[0], (int, float)):
        yield float(coords[0]), float(coords[1])
    else:
        for item in coords or []:
            yield from _walk_coords(item)


def geometry_rings(geometry: dict[str, Any]) -> list[list[tuple[float, float]]]:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    rings: list[list[tuple[float, float]]] = []
    if gtype == "Polygon":
        polygons = [coords]
    elif gtype == "MultiPolygon":
        polygons = coords
    else:
        raise ValueError(f"Boundary geometry must be Polygon or MultiPolygon, got {gtype!r}.")
    for polygon in polygons:
        for ring in polygon:
            pts = [(float(p[0]), float(p[1])) for p in ring]
            if len(pts) < 4:
                raise ValueError("Each boundary ring must contain at least four positions.")
            if pts[0] != pts[-1]:
                pts.append(pts[0])
            rings.append(pts)
    return rings


def _normalise_role(properties: dict[str, Any]) -> str:
    values = [properties.get(k) for k in ("role", "type", "point_role", "marker_role", "name", "id")]
    text = " ".join(str(v).strip().lower() for v in values if v is not None)
    if any(token in text for token in ("origin", "start", "source", "departure", "مبدا", "مبدأ")):
        return "origin"
    if any(token in text for token in ("destination", "goal", "target", "finish", "end", "مقصد")):
        return "destination"
    if any(token in text for token in ("boundary", "region", "extent", "polygon", "area")):
        return "boundary"
    return ""


def _assert_crs84(obj: dict[str, Any]) -> None:
    crs = obj.get("crs")
    if not crs:
        return  # RFC 7946 default is WGS 84 longitude/latitude.
    name = str((crs.get("properties") or {}).get("name", "")).upper()
    accepted = ("CRS84", "EPSG:4326", "WGS84", "WGS 84")
    if not any(x in name for x in accepted):
        raise ValueError(f"Region GeoJSON must use WGS 84/CRS84; declared CRS is {name!r}.")


def load_region_geojson(path: str | Path = DEFAULT_REGION_FILE) -> GeoJSONRegion:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Region GeoJSON not found: {p}")
    raw = p.read_bytes()
    obj = json.loads(raw.decode("utf-8"))
    if obj.get("type") != "FeatureCollection":
        raise ValueError("Region file must be a GeoJSON FeatureCollection.")
    _assert_crs84(obj)

    boundary_feature = None
    point_candidates: list[tuple[str, tuple[float, float], dict[str, Any]]] = []
    for feature in obj.get("features", []):
        geom = feature.get("geometry") or {}
        props = dict(feature.get("properties") or {})
        role = _normalise_role(props)
        gtype = geom.get("type")
        if gtype in ("Polygon", "MultiPolygon"):
            if boundary_feature is None or role == "boundary":
                boundary_feature = feature
        elif gtype == "Point":
            coords = geom.get("coordinates") or []
            if len(coords) < 2:
                raise ValueError("Point feature has fewer than two coordinates.")
            pt = (float(coords[0]), float(coords[1]))
            if not (all(math.isfinite(v) for v in pt) and -180 <= pt[0] <= 180 and -90 <= pt[1] <= 90):
                raise ValueError("Point coordinate must be finite WGS84 longitude/latitude.")
            point_candidates.append((role, pt, props))

    if boundary_feature is None:
        raise ValueError("Region GeoJSON must contain a Polygon or MultiPolygon boundary feature.")
    boundary = boundary_feature["geometry"]
    rings = geometry_rings(boundary)
    pts = [p for ring in rings for p in ring]
    west = min(x for x, _ in pts); east = max(x for x, _ in pts)
    south = min(y for _, y in pts); north = max(y for _, y in pts)
    if not (-180 <= west <= 180 and -180 <= east <= 180 and -90 <= south <= 90 and -90 <= north <= 90):
        raise ValueError("Region coordinates are outside valid WGS 84 longitude/latitude ranges.")

    origin = destination = None
    origin_props: dict[str, Any] = {}
    destination_props: dict[str, Any] = {}
    for role, pt, props in point_candidates:
        if role == "origin" and origin is None:
            origin, origin_props = pt, props
        elif role == "destination" and destination is None:
            destination, destination_props = pt, props
    # Conservative fallback only when roles are absent: preserve file order, first point=start, second=end.
    unlabeled = [(pt, props) for role, pt, props in point_candidates if not role]
    if origin is None and unlabeled:
        origin, origin_props = unlabeled[0]
    if destination is None and len(unlabeled) >= 2:
        destination, destination_props = unlabeled[1]

    return GeoJSONRegion(
        p.resolve(), boundary, west, south, east, north,
        (west + east) / 2.0, (south + north) / 2.0,
        origin, destination, str(obj.get("name") or p.stem),
        origin_props, destination_props, dict(boundary_feature.get("properties") or {}),
        hashlib.sha256(raw).hexdigest(), obj,
    )


def default_output_dir(region: GeoJSONRegion) -> Path:
    return Path("outputs") / f"{region.path.stem}_map"


def default_dataset_paths(region: GeoJSONRegion):
    base = default_output_dir(region)
    return {
        "base": base,
        "terrain": base / "terrain_obstacles.tif",
        "climate": base / "climate_contexts.tif",
        "magnetic": base / "magnetic_anomaly.tif",
        "centroids": base / "climate_centroids.csv",
    }
