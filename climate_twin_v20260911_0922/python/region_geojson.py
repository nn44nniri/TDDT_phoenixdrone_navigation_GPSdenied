#!/usr/bin/env python3
"""Strict GeoJSON/WGS 84 region loading helpers for MicroMetTwin."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable

WGS84_CRS_NAMES = {
    "EPSG:4326", "urn:ogc:def:crs:OGC:1.3:CRS84", "CRS84", "WGS84", "WGS 84"
}


def _validate_position(position: Any) -> tuple[float, float]:
    if not isinstance(position, (list, tuple)) or len(position) < 2:
        raise ValueError(f"Invalid GeoJSON position: {position!r}")
    lon, lat = float(position[0]), float(position[1])
    if not (math.isfinite(lon) and math.isfinite(lat)):
        raise ValueError("GeoJSON coordinates must be finite numbers")
    if not -180.0 <= lon <= 180.0 or not -90.0 <= lat <= 90.0:
        raise ValueError(f"Coordinate is outside WGS 84 longitude/latitude range: [{lon}, {lat}]")
    return lon, lat


def _closed_ring(raw_ring: Any) -> list[tuple[float, float]]:
    if not isinstance(raw_ring, list) or len(raw_ring) < 4:
        raise ValueError("A GeoJSON Polygon exterior ring must contain at least four positions")
    ring = [_validate_position(p) for p in raw_ring]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    if len(set(ring[:-1])) < 3:
        raise ValueError("The region polygon must contain at least three distinct vertices")
    return ring


def _polygon_area_centroid(ring: list[tuple[float, float]]) -> tuple[float, float, float]:
    # Planar longitude/latitude centroid is sufficient for compact regions; it is
    # used only as a sampling/reference point, not for metric area calculation.
    twice_area = 0.0
    cx = 0.0
    cy = 0.0
    for (x0, y0), (x1, y1) in zip(ring[:-1], ring[1:]):
        cross = x0 * y1 - x1 * y0
        twice_area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(twice_area) < 1e-15:
        xs = [p[0] for p in ring[:-1]]
        ys = [p[1] for p in ring[:-1]]
        return 0.0, sum(xs) / len(xs), sum(ys) / len(ys)
    return abs(twice_area) / 2.0, cx / (3.0 * twice_area), cy / (3.0 * twice_area)


def _select_boundary_feature(document: dict[str, Any]) -> dict[str, Any]:
    doc_type = document.get("type")
    if doc_type == "FeatureCollection":
        features = document.get("features")
        if not isinstance(features, list):
            raise ValueError("GeoJSON FeatureCollection.features must be an array")
        polygon_features = [
            f for f in features
            if isinstance(f, dict) and isinstance(f.get("geometry"), dict)
            and f["geometry"].get("type") in {"Polygon", "MultiPolygon"}
        ]
        if not polygon_features:
            raise ValueError("GeoJSON contains no Polygon or MultiPolygon region feature")
        for feature in polygon_features:
            props = feature.get("properties") or {}
            if feature.get("id") == "region-boundary" or props.get("feature_role") == "region_boundary":
                return feature
        return polygon_features[0]
    if doc_type == "Feature":
        return document
    if doc_type in {"Polygon", "MultiPolygon"}:
        return {"type": "Feature", "properties": {}, "geometry": document}
    raise ValueError("Region must be a GeoJSON FeatureCollection, Feature, Polygon, or MultiPolygon")


def load_region_geojson(path: str | Path) -> dict[str, Any]:
    region_path = Path(path)
    document = json.loads(region_path.read_text(encoding="utf-8"))

    # RFC 7946 uses WGS 84 longitude/latitude and discourages custom CRS members.
    crs = document.get("crs")
    if crs:
        crs_name = str((crs.get("properties") or {}).get("name", "")) if isinstance(crs, dict) else str(crs)
        if crs_name and crs_name not in WGS84_CRS_NAMES and "4326" not in crs_name and "CRS84" not in crs_name:
            raise ValueError(f"Unsupported GeoJSON CRS {crs_name!r}; region coordinates must use WGS 84")

    feature = _select_boundary_feature(document)
    geometry = feature.get("geometry") or {}
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    rings: list[list[tuple[float, float]]] = []
    if geometry_type == "Polygon":
        if not isinstance(coordinates, list) or not coordinates:
            raise ValueError("GeoJSON Polygon coordinates are missing")
        rings = [_closed_ring(coordinates[0])]
    elif geometry_type == "MultiPolygon":
        if not isinstance(coordinates, list) or not coordinates:
            raise ValueError("GeoJSON MultiPolygon coordinates are missing")
        rings = [_closed_ring(poly[0]) for poly in coordinates if isinstance(poly, list) and poly]
        if not rings:
            raise ValueError("GeoJSON MultiPolygon contains no exterior rings")
    else:
        raise ValueError("Selected region feature is not a Polygon or MultiPolygon")

    all_points = [p for ring in rings for p in ring[:-1]]
    min_lon = min(p[0] for p in all_points)
    max_lon = max(p[0] for p in all_points)
    min_lat = min(p[1] for p in all_points)
    max_lat = max(p[1] for p in all_points)

    weighted = [_polygon_area_centroid(ring) for ring in rings]
    area_sum = sum(item[0] for item in weighted)
    if area_sum > 0:
        center_lon = sum(a * x for a, x, _ in weighted) / area_sum
        center_lat = sum(a * y for a, _, y in weighted) / area_sum
    else:
        center_lon = (min_lon + max_lon) / 2.0
        center_lat = (min_lat + max_lat) / 2.0

    return {
        "path": str(region_path),
        "document": document,
        "feature": feature,
        "geometry_type": geometry_type,
        "rings": rings,
        "exterior_ring": rings[0],
        "center": {"longitude": center_lon, "latitude": center_lat},
        "bbox": [min_lon, min_lat, max_lon, max_lat],
        "name": (feature.get("properties") or {}).get("name") or document.get("name") or region_path.stem,
        "crs": "WGS 84 (RFC 7946 / EPSG:4326 coordinate order longitude, latitude)",
    }


def sampling_points(region: dict[str, Any], max_points: int = 5) -> list[dict[str, float | str]]:
    if max_points < 1:
        raise ValueError("max_points must be at least 1")
    result: list[dict[str, float | str]] = [
        {"name": "center", **region["center"]}
    ]
    if max_points == 1:
        return result
    boundary = region["exterior_ring"][:-1]
    wanted = min(max_points - 1, len(boundary))
    selected_indices: list[int] = []
    for i in range(wanted):
        idx = int(round(i * len(boundary) / wanted)) % len(boundary)
        if idx not in selected_indices:
            selected_indices.append(idx)
    for n, idx in enumerate(selected_indices, start=1):
        lon, lat = boundary[idx]
        result.append({"name": f"boundary_{n}", "longitude": lon, "latitude": lat})
    return result


def geojson_vertices(region: dict[str, Any]) -> list[dict[str, float]]:
    return [{"longitude": lon, "latitude": lat} for lon, lat in region["exterior_ring"][:-1]]
