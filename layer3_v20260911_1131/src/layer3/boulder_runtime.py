from __future__ import annotations

import csv
import gzip
import importlib
import math
import sys
from pathlib import Path
from typing import Any

from .config import Layer3Config


class BoulderRuntimeAdapter:
    """Runtime consumer of the public Boulder multilayer-map/CRP products.

    MicroMetTwin remains authoritative for *live* climate.  Boulder terrain,
    magnetic, gridded climate and CRP products are consumed as spatial/reference
    knowledge.  `crp_cell_assignments.csv.gz` and `crp_cluster_map.tif` are both
    used to make spatial membership explicit and auditable.
    """

    def __init__(self, config: Layer3Config):
        self.config = config
        root = config.boulder_root
        if str(root) in sys.path:
            sys.path.remove(str(root))
        sys.path.insert(0, str(root))
        # Climate and Boulder contain a few same-named top-level helpers.
        saved = {}
        for name in ("region_geojson", "map_mosaic_query"):
            mod = sys.modules.get(name)
            origin = str(getattr(mod, "__file__", "")) if mod is not None else ""
            if mod is not None and str(root) not in origin:
                saved[name] = mod
                del sys.modules[name]
        try:
            self.module = importlib.import_module("map_mosaic_query")
        finally:
            for name, mod in saved.items():
                if name != "map_mosaic_query":
                    sys.modules[name] = mod
        self._terrain_min_m = self._terrain_minimum()
        self._assignment_cache: dict[tuple[int, int, str], dict[str, Any] | None] = {}

    def _terrain_minimum(self) -> float | None:
        try:
            import numpy as np
            import rasterio
            path = self.config.map_root / "terrain_obstacles.tif"
            with rasterio.open(path) as ds:
                band = self.module.band_map(ds)["terrain_elevation_m"]
                arr = ds.read(band, masked=True)
                if arr.count() == 0:
                    return None
                return float(np.ma.min(arr))
        except Exception:
            return None

    def _pixel_index(self, longitude: float, latitude: float) -> tuple[int, int] | None:
        try:
            import rasterio
            from rasterio.warp import transform
            with rasterio.open(self.config.map_root / "terrain_obstacles.tif") as ds:
                xs, ys = transform("EPSG:4326", ds.crs, [float(longitude)], [float(latitude)])
                row, col = ds.index(xs[0], ys[0])
                return int(row), int(col)
        except Exception:
            return None

    @staticmethod
    def _coerce(text: Any) -> Any:
        if text is None:
            return None
        value = str(text).strip()
        if value == "":
            return None
        try:
            if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
                return int(value)
            x = float(value)
            return x if math.isfinite(x) else value
        except Exception:
            return value

    def _assignment_at(self, row: int, col: int, bin_name: str) -> dict[str, Any] | None:
        key = (int(row), int(col), str(bin_name))
        if key in self._assignment_cache:
            return self._assignment_cache[key]
        path = self.config.crp_root / "crp_cell_assignments.csv.gz"
        wanted_state = f"UTC_{bin_name}"
        found = None
        with gzip.open(path, "rt", newline="", encoding="utf-8") as f:
            for rec in csv.DictReader(f):
                if rec.get("time_state") != wanted_state:
                    continue
                if int(rec.get("row", -1)) == int(row) and int(rec.get("col", -1)) == int(col):
                    found = {k: self._coerce(v) for k, v in rec.items()}
                    break
        self._assignment_cache[key] = found
        return found

    def sample(self, longitude: float, latitude: float, *, hour: int | None = None) -> dict[str, Any]:
        h = int(hour if hour is not None else 12)
        map_root = self.config.map_root
        terrain = self.module.sample_file(str(map_root / "terrain_obstacles.tif"), float(longitude), float(latitude)) or {}
        climate = self.module.sample_file(str(map_root / "climate_contexts.tif"), float(longitude), float(latitude)) or {}
        magnetic = self.module.sample_file(str(map_root / "magnetic_anomaly.tif"), float(longitude), float(latitude)) or {}
        crp = self.module.sample_file(str(self.config.crp_root / "crp_cluster_map.tif"), float(longitude), float(latitude)) or {}
        bin_name = self.module.time_bin(h)
        crp_key = f"crp_cluster_id_{bin_name}"
        pixel = self._pixel_index(longitude, latitude)
        assignment = self._assignment_at(pixel[0], pixel[1], bin_name) if pixel else None

        elev = terrain.get("terrain_elevation_m")
        relief = None
        if elev is not None and self._terrain_min_m is not None:
            try:
                relief = max(0.0, float(elev) - float(self._terrain_min_m))
            except Exception:
                relief = None

        reference_context = {
            "temperature_c": climate.get(f"temperature_c_{bin_name}"),
            "relative_humidity_percent": climate.get(f"relative_humidity_percent_{bin_name}"),
            "wind_speed_ms": climate.get(f"wind_vector_speed_ms_{bin_name}"),
            "wind_direction_from_deg": climate.get(f"wind_direction_from_deg_{bin_name}"),
            "climate_cluster_id": climate.get(f"climate_cluster_id_{bin_name}"),
            "climate_cluster_confidence": climate.get(f"cluster_confidence_{bin_name}"),
            "time_bin_utc": bin_name,
        }
        raster_id = crp.get(crp_key)
        assignment_id = assignment.get("cluster_id") if assignment else None
        spatial_id = raster_id if raster_id is not None else assignment_id
        context = {
            "terrain_elevation_m": elev,
            "ground_elevation_m": elev,
            "terrain_slope_deg": terrain.get("terrain_slope_deg"),
            "terrain_aspect_deg": terrain.get("terrain_aspect_deg"),
            "local_relief_m": relief,
            "building_height_m": terrain.get("building_height_m"),
            "magnetic_anomaly_nt": magnetic.get("magnetic_anomaly_nt"),
            "magnetic_gradient_nt_per_m": magnetic.get("magnetic_anomaly_gradient_nt_per_m"),
            "crp_raster_id": spatial_id,
            "crp_assignment_id": assignment_id,
            "crp_time_bin": bin_name,
            "boulder_reference_climate": {k: v for k, v in reference_context.items() if v is not None},
            "crp_assignment_reference": assignment or {},
        }
        raster_assignment_agree = (
            raster_id is None or assignment_id is None or int(float(raster_id)) == int(float(assignment_id))
        )
        return {
            "context": {k: v for k, v in context.items() if v is not None},
            "reference_context": {k: v for k, v in reference_context.items() if v is not None},
            "terrain_sample": terrain,
            "climate_sample": climate,
            "magnetic_sample": magnetic,
            "crp_sample": crp,
            "crp_assignment": assignment,
            "pixel": {"row": pixel[0], "col": pixel[1]} if pixel else None,
            "crp_spatial_crosscheck": {
                "raster_cluster_id": raster_id,
                "assignment_cluster_id": assignment_id,
                "agree": raster_assignment_agree,
                "assignment_source": str(self.config.crp_root / "crp_cell_assignments.csv.gz"),
                "raster_source": str(self.config.crp_root / "crp_cluster_map.tif"),
            },
            "terrain_roi_min_m": self._terrain_min_m,
            "api": {
                "sample": "map_mosaic_query.sample_file",
                "time_bin": "map_mosaic_query.time_bin",
                "band_map": "map_mosaic_query.band_map",
                "crp_assignment": "crp_cell_assignments.csv.gz row/col/time_state lookup",
            },
            "semantics": {
                "live_climate_owner": "MicroMetTwin",
                "boulder_climate_role": "regional/reference context only",
                "crp_assignment_role": "spatial/reference membership only",
                "state_overwrite": False,
            },
        }
