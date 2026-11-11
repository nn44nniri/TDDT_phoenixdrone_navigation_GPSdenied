from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

WGS84 = "EPSG:4326"


def _norm(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _feature_role(feature: dict[str, Any]) -> str:
    props = feature.get("properties") or {}
    values = [
        props.get("role"), props.get("type"), props.get("kind"), props.get("name"),
        props.get("label"), props.get("id"), feature.get("id"),
    ]
    return " ".join(_norm(v) for v in values if v is not None)


def _coords_point(feature: dict[str, Any]) -> tuple[float, float] | None:
    geom = feature.get("geometry") or {}
    if geom.get("type") != "Point":
        return None
    c = geom.get("coordinates") or []
    if len(c) < 2:
        return None
    return float(c[0]), float(c[1])


def _iter_polygon_rings(geometry: dict[str, Any]) -> Iterable[list[list[float]]]:
    typ = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if typ == "Polygon":
        yield coords
    elif typ == "MultiPolygon":
        yield from coords


def _point_in_ring(x: float, y: float, ring: list[list[float]]) -> bool:
    inside = False
    if len(ring) < 3:
        return False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = float(ring[i][0]), float(ring[i][1])
        xj, yj = float(ring[j][0]), float(ring[j][1])
        # Boundary tolerance.
        dx, dy = xj - xi, yj - yi
        if abs(dx) + abs(dy) > 0:
            t = ((x - xi) * dx + (y - yi) * dy) / (dx * dx + dy * dy)
            if 0 <= t <= 1:
                px, py = xi + t * dx, yi + t * dy
                if math.hypot(x - px, y - py) <= 1e-8 * max(1.0, abs(x), abs(y)):
                    return True
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-300) + xi):
            inside = not inside
        j = i
    return inside


def point_in_geometry(x: float, y: float, geometry: dict[str, Any]) -> bool:
    for rings in _iter_polygon_rings(geometry):
        if not rings or not _point_in_ring(x, y, rings[0]):
            continue
        if any(_point_in_ring(x, y, hole) for hole in rings[1:]):
            continue
        return True
    return False


def geometry_bounds(geometry: dict[str, Any]) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for rings in _iter_polygon_rings(geometry):
        for ring in rings:
            for c in ring:
                xs.append(float(c[0])); ys.append(float(c[1]))
    if not xs:
        raise ValueError("GeoJSON region geometry contains no polygon coordinates")
    return min(xs), min(ys), max(xs), max(ys)


def transform_geometry(geometry: dict[str, Any], dst_crs: Any) -> dict[str, Any]:
    if not dst_crs or str(dst_crs).upper() in {"EPSG:4326", "OGC:CRS84", "WGS84"}:
        return geometry
    from rasterio.warp import transform_geom
    return transform_geom(WGS84, dst_crs, geometry, precision=15)


def transform_point_between(point: tuple[float, float], src_crs: Any, dst_crs: Any) -> tuple[float, float]:
    """Transform a 2-D point between explicit CRS definitions.

    GeoJSON/WGS84 uses (longitude, latitude); projected map CRS uses (x, y) in
    the linear unit declared by that CRS.
    """
    src = str(src_crs or WGS84)
    dst = str(dst_crs or WGS84)
    if src.upper() == dst.upper():
        return float(point[0]), float(point[1])
    from rasterio.warp import transform
    xx, yy = transform(src, dst, [float(point[0])], [float(point[1])])
    return float(xx[0]), float(yy[0])


def transform_point(point: tuple[float, float], dst_crs: Any) -> tuple[float, float]:
    return transform_point_between(point, WGS84, dst_crs)


def validate_crs_roundtrip(
    point_wgs84: tuple[float, float],
    point_map: tuple[float, float],
    map_crs: Any,
    *,
    map_tolerance_m: float = 0.05,
    geodetic_tolerance_deg: float = 1e-8,
) -> dict[str, float | bool]:
    """Validate WGS84↔map-CRS consistency for one coordinate pair.

    The projected tolerance is expressed in metres for the UTM/metric map CRS
    used by SARG. The reverse tolerance is expressed in degrees.
    """
    fwd = transform_point_between(point_wgs84, WGS84, map_crs)
    rev = transform_point_between(point_map, map_crs, WGS84)
    map_error = math.hypot(fwd[0] - float(point_map[0]), fwd[1] - float(point_map[1]))
    geo_error = math.hypot(rev[0] - float(point_wgs84[0]), rev[1] - float(point_wgs84[1]))
    return {
        "forward_error_m": float(map_error),
        "reverse_error_deg": float(geo_error),
        "map_tolerance_m": float(map_tolerance_m),
        "geodetic_tolerance_deg": float(geodetic_tolerance_deg),
        "valid": bool(map_error <= map_tolerance_m and geo_error <= geodetic_tolerance_deg),
    }


@dataclass(frozen=True)
class RegionDefinition:
    path: Path
    region_id: str
    geometry_wgs84: dict[str, Any]
    origin_wgs84: tuple[float, float]
    destination_wgs84: tuple[float, float]
    raw: dict[str, Any]

    @property
    def bounds_wgs84(self) -> tuple[float, float, float, float]:
        return geometry_bounds(self.geometry_wgs84)

    def in_map_crs(self, map_crs: Any) -> "ResolvedRegion":
        geom = transform_geometry(self.geometry_wgs84, map_crs)
        return ResolvedRegion(
            source=self,
            geometry_map=geom,
            origin_map=transform_point(self.origin_wgs84, map_crs),
            destination_map=transform_point(self.destination_wgs84, map_crs),
            map_crs=str(map_crs) if map_crs else WGS84,
        )


@dataclass(frozen=True)
class ResolvedRegion:
    source: RegionDefinition
    geometry_map: dict[str, Any]
    origin_map: tuple[float, float]
    destination_map: tuple[float, float]
    map_crs: str

    @property
    def bounds_map(self) -> tuple[float, float, float, float]:
        return geometry_bounds(self.geometry_map)

    def contains_map(self, x: float, y: float) -> bool:
        return point_in_geometry(float(x), float(y), self.geometry_map)

    def metadata(self) -> dict[str, Any]:
        return {
            "region_id": self.source.region_id,
            "source_file": str(self.source.path),
            "source_crs": WGS84,
            "map_crs": self.map_crs,
            "bounds_wgs84": list(self.source.bounds_wgs84),
            "bounds_map": list(self.bounds_map),
            "origin_wgs84": list(self.source.origin_wgs84),
            "destination_wgs84": list(self.source.destination_wgs84),
            "origin_map": list(self.origin_map),
            "destination_map": list(self.destination_map),
        }


def load_region(path: str | Path) -> RegionDefinition:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"GeoJSON region file not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("type") != "FeatureCollection":
        raise ValueError("region_v1.geojson must be a GeoJSON FeatureCollection")
    crs = data.get("crs")
    if crs:
        text = json.dumps(crs).lower()
        if not any(token in text for token in ("4326", "crs84", "wgs84", "wgs 84")):
            raise ValueError("region_v1.geojson must use WGS 84 / EPSG:4326")
    features = data.get("features") or []
    polygons = [f for f in features if (f.get("geometry") or {}).get("type") in {"Polygon", "MultiPolygon"}]
    if not polygons:
        raise ValueError("GeoJSON must contain a Polygon or MultiPolygon region feature")
    preferred = [f for f in polygons if any(k in _feature_role(f) for k in ("region", "boundary", "area", "mission"))]
    region_feature = preferred[0] if preferred else polygons[0]
    geometry = region_feature["geometry"]
    props = region_feature.get("properties") or {}
    region_id = str(props.get("region_id") or props.get("id") or props.get("name") or region_feature.get("id") or p.stem)

    points = [(f, _coords_point(f), _feature_role(f)) for f in features]
    points = [(f, q, r) for f, q, r in points if q is not None]
    origin = next((q for _, q, role in points if any(k in role for k in ("origin", "start", "source", "departure"))), None)
    destination = next((q for _, q, role in points if any(k in role for k in ("destination", "goal", "target", "end", "arrival"))), None)
    # A route LineString can provide endpoint fallback without inventing coordinates.
    if origin is None or destination is None:
        lines = [f for f in features if (f.get("geometry") or {}).get("type") in {"LineString", "MultiLineString"}]
        if lines:
            coords = lines[0]["geometry"].get("coordinates") or []
            if lines[0]["geometry"]["type"] == "MultiLineString":
                coords = [c for part in coords for c in part]
            if len(coords) >= 2:
                origin = origin or (float(coords[0][0]), float(coords[0][1]))
                destination = destination or (float(coords[-1][0]), float(coords[-1][1]))
    if origin is None or destination is None:
        raise ValueError("GeoJSON must identify Origin and Destination as Point features (or route endpoints)")
    for label, point in (("Origin", origin), ("Destination", destination)):
        if not (-180 <= point[0] <= 180 and -90 <= point[1] <= 90):
            raise ValueError(f"{label} is not a valid WGS 84 longitude/latitude: {point}")
        if not point_in_geometry(point[0], point[1], geometry):
            raise ValueError(f"{label} {point} lies outside the GeoJSON region polygon")
    return RegionDefinition(p, region_id, geometry, origin, destination, data)
