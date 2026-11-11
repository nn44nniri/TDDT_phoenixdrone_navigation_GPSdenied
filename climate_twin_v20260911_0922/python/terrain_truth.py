from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import rasterio
from rasterio.warp import transform as crs_transform


ELEVATION_NAMES = {"terrain_elevation_m", "elevation", "dem", "ground_elevation_m"}
SLOPE_NAMES = {"terrain_slope_deg", "slope", "slope_deg"}
ASPECT_NAMES = {"terrain_aspect_deg", "aspect", "aspect_deg"}


def find_terrain_tif(path: str | Path | None = None, *, dem_dir: str | Path | None = None) -> Optional[Path]:
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path).expanduser())
    if dem_dir:
        candidates.append(Path(dem_dir).expanduser())
    for env_name in ("MICROMET_TERRAIN_TIF", "MICROMET_DEM"):
        if os.environ.get(env_name):
            candidates.append(Path(os.environ[env_name]).expanduser())
    for env_name in ("MICROMET_DEM_DIR", "TDDT_MAP_DIR"):
        if os.environ.get(env_name):
            candidates.append(Path(os.environ[env_name]).expanduser())

    for p in candidates:
        if p.is_file():
            return p.resolve()
        if p.is_dir():
            for name in ("terrain_obstacles.tif", "dem.tif", "terrain.tif", "elevation.tif"):
                q = p / name
                if q.is_file():
                    return q.resolve()
            hits = sorted(p.glob("*terrain*obstacle*.tif")) + sorted(p.glob("*dem*.tif"))
            if hits:
                return hits[0].resolve()
    return None


def _band_by_description(ds: rasterio.io.DatasetReader, names: set[str], fallback: int | None) -> int | None:
    desc = [str(x or "").strip().lower() for x in ds.descriptions]
    for i, d in enumerate(desc, 1):
        if d in names:
            return i
    return fallback if fallback is not None and fallback <= ds.count else None


@dataclass(frozen=True)
class TerrainSample:
    longitude: float
    latitude: float
    ground_elevation_m: float
    slope_deg: float
    aspect_deg: float
    curvature_raw: float
    curvature_scaled: float
    dzdx: float
    dzdy: float
    zW: float
    zE: float
    zS: float
    zN: float
    zSW: float
    zNE: float
    zNW: float
    zSE: float
    curvature_radius_m: float
    source_path: str
    elevation_band: int
    slope_band: int | None
    aspect_band: int | None

    def fmi_overrides(self) -> dict[str, float]:
        return {
            "slope_rad_input": math.radians(self.slope_deg),
            "aspect_rad_input": math.radians(self.aspect_deg),
            "curvature_scaled_input": self.curvature_scaled,
            "terrain_dzdx": self.dzdx,
            "terrain_dzdy": self.dzdy,
            # Preserve the exact slope/aspect bands from the common terrain raster.
            "derive_slope_aspect_from_gradients": 0.0,
            "curvature_radius_m": self.curvature_radius_m,
            "zW": self.zW, "zE": self.zE, "zS": self.zS, "zN": self.zN,
            "zSW": self.zSW, "zNE": self.zNE, "zNW": self.zNW, "zSE": self.zSE,
            # curvature_scaled is derived from the same DEM. Feeding it directly avoids
            # hidden domain-normalization assumptions in the native FMU.
            "use_dem_curvature": 0.0,
        }


class TerrainTruth:
    """Windowed reader for the shared TDDT terrain GeoTIFF.

    The raster is never loaded as a whole. Band 1/description terrain_elevation_m
    supplies ground MSL; slope/aspect use the named bands when available; curvature
    and dz/dx,dz/dy are derived from the same elevation band.
    """

    def __init__(self, path: str | Path, *, curvature_radius_m: float = 1000.0):
        self.path = Path(path).expanduser().resolve()
        self.ds = rasterio.open(self.path)
        if self.ds.crs is None:
            self.ds.close()
            raise ValueError(f"Terrain raster has no CRS: {self.path}")
        self.elevation_band = _band_by_description(self.ds, ELEVATION_NAMES, 1)
        self.slope_band = _band_by_description(self.ds, SLOPE_NAMES, 2 if self.ds.count >= 2 else None)
        self.aspect_band = _band_by_description(self.ds, ASPECT_NAMES, 3 if self.ds.count >= 3 else None)
        self.curvature_radius_m = max(float(curvature_radius_m), 1.0)

    def close(self) -> None:
        self.ds.close()

    def __enter__(self) -> "TerrainTruth":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _xy(self, lon: float, lat: float) -> tuple[float, float]:
        if self.ds.crs.to_epsg() == 4326:
            return float(lon), float(lat)
        xs, ys = crs_transform("EPSG:4326", self.ds.crs, [lon], [lat])
        return float(xs[0]), float(ys[0])

    def _lonlat(self, x: float, y: float) -> tuple[float, float]:
        if self.ds.crs.to_epsg() == 4326:
            return float(x), float(y)
        lons, lats = crs_transform(self.ds.crs, "EPSG:4326", [x], [y])
        return float(lons[0]), float(lats[0])

    def contains(self, lon: float, lat: float) -> bool:
        x, y = self._xy(lon, lat)
        b = self.ds.bounds
        return b.left <= x < b.right and b.bottom < y <= b.top

    def bounds_wgs84(self) -> tuple[float, float, float, float]:
        """Return raster coverage as (west, south, east, north) in EPSG:4326."""
        b = self.ds.bounds
        corners = [
            self._lonlat(b.left, b.bottom), self._lonlat(b.left, b.top),
            self._lonlat(b.right, b.bottom), self._lonlat(b.right, b.top),
        ]
        lons = [c[0] for c in corners]
        lats = [c[1] for c in corners]
        return min(lons), min(lats), max(lons), max(lats)

    def nearest_covered_lonlat(self, lon: float, lat: float) -> tuple[float, float]:
        """Nearest point strictly inside raster bounds; diagnostic only, never sampled implicitly."""
        x, y = self._xy(lon, lat)
        b = self.ds.bounds
        epsx = max(abs(float(self.ds.transform.a)) * 0.51, 1e-12)
        epsy = max(abs(float(self.ds.transform.e)) * 0.51, 1e-12)
        xmin, xmax = b.left + epsx, b.right - epsx
        ymin, ymax = b.bottom + epsy, b.top - epsy
        if xmin > xmax:
            xmin = xmax = (b.left + b.right) * 0.5
        if ymin > ymax:
            ymin = ymax = (b.bottom + b.top) * 0.5
        xx = min(max(x, xmin), xmax)
        yy = min(max(y, ymin), ymax)
        return self._lonlat(xx, yy)

    def _read_nearest(self, band: int, x: float, y: float) -> float:
        b = self.ds.bounds
        if not (b.left <= x < b.right and b.bottom < y <= b.top):
            raise ValueError("requested point lies outside terrain raster coverage")
        row, col = self.ds.index(x, y)
        if row < 0 or row >= self.ds.height or col < 0 or col >= self.ds.width:
            raise ValueError("requested point lies outside terrain raster grid")
        a = self.ds.read(band, window=((row, row + 1), (col, col + 1)), masked=True)
        cell = a[0, 0]
        if np.ma.is_masked(cell):
            raise ValueError("terrain raster has NoData at requested point")
        value = float(cell)
        if not math.isfinite(value):
            raise ValueError("terrain raster has non-finite data at requested point")
        return value

    def _offset_xy(self, x: float, y: float, east_m: float, north_m: float, lat: float) -> tuple[float, float]:
        if self.ds.crs.is_projected:
            unit_scale = 1.0
            try:
                factor = float(self.ds.crs.linear_units_factor[1])
                if factor > 0:
                    unit_scale = factor
            except Exception:
                pass
            return x + east_m / unit_scale, y + north_m / unit_scale
        # Geographic fallback: offsets are only used locally for terrain derivatives.
        dx_deg = east_m / (111320.0 * max(math.cos(math.radians(lat)), 1e-6))
        dy_deg = north_m / 110540.0
        return x + dx_deg, y + dy_deg

    def _read_bilinear_elevation(self, x: float, y: float) -> float:
        """Same pixel-centre ground-height contract as TDOC terrain.sample_bilinear."""
        b=self.ds.bounds
        if not (b.left<=x<b.right and b.bottom<y<=b.top):raise ValueError('point outside terrain coverage')
        col,row=(~self.ds.transform) @ (x,y)
        col=min(max(float(col)-.5,0.),self.ds.width-1.)
        row=min(max(float(row)-.5,0.),self.ds.height-1.)
        c0,r0=int(math.floor(col)),int(math.floor(row));c1=min(c0+1,self.ds.width-1);r1=min(r0+1,self.ds.height-1)
        values=self.ds.read(self.elevation_band,window=((r0,r1+1),(c0,c1+1)),masked=True)
        dc,dr=col-c0,row-r0;result=0.
        for rr,cc,w in ((r0,c0,(1-dr)*(1-dc)),(r0,c1,(1-dr)*dc),(r1,c0,dr*(1-dc)),(r1,c1,dr*dc)):
            if w<=1e-15:continue
            v=values[rr-r0,cc-c0]
            if np.ma.is_masked(v) or not math.isfinite(float(v)):raise ValueError('terrain interpolation has NoData/nonfinite input')
            result+=w*float(v)
        return result

    def _pixel_gradient(self, lon: float, lat: float) -> tuple[float, float]:
        x, y = self._xy(lon, lat)
        row, col = self.ds.index(x, y)
        if row < 0 or row >= self.ds.height or col < 0 or col >= self.ds.width:
            raise ValueError("point lies outside terrain raster for gradient sampling")
        if self.ds.crs.is_projected:
            scale = 1.0
            try:
                scale = float(self.ds.crs.linear_units_factor[1])
            except Exception:
                pass
            dx = abs(float(self.ds.transform.a)) * scale
            dy = abs(float(self.ds.transform.e)) * scale
        else:
            dx = abs(float(self.ds.transform.a)) * 111320.0 * max(math.cos(math.radians(lat)), 1e-6)
            dy = abs(float(self.ds.transform.e)) * 110540.0

        def cell(r: int, c: int) -> float:
            if r < 0 or r >= self.ds.height or c < 0 or c >= self.ds.width:
                return float("nan")
            a = self.ds.read(self.elevation_band, window=((r, r + 1), (c, c + 1)), masked=True)
            if np.ma.is_masked(a[0, 0]):
                return float("nan")
            v = float(a[0, 0])
            return v if math.isfinite(v) else float("nan")

        zc = cell(row, col)
        if not math.isfinite(zc):
            raise ValueError("terrain raster has NoData at requested point")
        zl, zr = cell(row, col - 1), cell(row, col + 1)
        zn, zs = cell(row - 1, col), cell(row + 1, col)
        if math.isfinite(zl) and math.isfinite(zr):
            dzdx = (zr - zl) / (2.0 * max(dx, 1e-9))
        elif math.isfinite(zr):
            dzdx = (zr - zc) / max(dx, 1e-9)
        elif math.isfinite(zl):
            dzdx = (zc - zl) / max(dx, 1e-9)
        else:
            dzdx = 0.0
        # Raster row decreases toward geographic/projected north.
        if math.isfinite(zn) and math.isfinite(zs):
            dzdy = (zn - zs) / (2.0 * max(dy, 1e-9))
        elif math.isfinite(zn):
            dzdy = (zn - zc) / max(dy, 1e-9)
        elif math.isfinite(zs):
            dzdy = (zc - zs) / max(dy, 1e-9)
        else:
            dzdy = 0.0
        return float(dzdx), float(dzdy)

    @staticmethod
    def _derived_slope_aspect(dzdx: float, dzdy: float) -> tuple[float, float]:
        slope = math.degrees(math.atan(math.hypot(dzdx, dzdy)))
        aspect = math.degrees(1.5 * math.pi - math.atan2(dzdy, dzdx)) % 360.0
        return slope, aspect

    def sample(self, lon: float, lat: float) -> TerrainSample:
        x, y = self._xy(lon, lat)
        ground = self._read_bilinear_elevation(x, y)
        dzdx, dzdy = self._pixel_gradient(lon, lat)
        derived_slope, derived_aspect = self._derived_slope_aspect(dzdx, dzdy)
        try:
            slope = self._read_nearest(self.slope_band, x, y) if self.slope_band else derived_slope
        except ValueError:
            slope = derived_slope
        try:
            aspect = self._read_nearest(self.aspect_band, x, y) if self.aspect_band else derived_aspect
        except ValueError:
            aspect = derived_aspect
        if not math.isfinite(slope):
            slope = derived_slope
        if not math.isfinite(aspect):
            aspect = derived_aspect
        aspect %= 360.0

        eta = self.curvature_radius_m
        offsets = {
            "W": (-eta, 0.0), "E": (eta, 0.0), "S": (0.0, -eta), "N": (0.0, eta),
            "SW": (-eta / math.sqrt(2.0), -eta / math.sqrt(2.0)),
            "NE": (eta / math.sqrt(2.0), eta / math.sqrt(2.0)),
            "NW": (-eta / math.sqrt(2.0), eta / math.sqrt(2.0)),
            "SE": (eta / math.sqrt(2.0), -eta / math.sqrt(2.0)),
        }
        vals: dict[str, float] = {}
        for key, (de, dn) in offsets.items():
            ox, oy = self._offset_xy(x, y, de, dn, lat)
            try:
                vals[key] = self._read_nearest(self.elevation_band, ox, oy)
            except ValueError:
                # At a raster edge use the centre elevation instead of leaving a fake
                # climate-derived terrain value. The sample remains entirely DEM-sourced.
                vals[key] = ground

        raw = 0.25 * (
            (ground - 0.5 * (vals["W"] + vals["E"])) / (2.0 * eta)
            + (ground - 0.5 * (vals["S"] + vals["N"])) / (2.0 * eta)
            + (ground - 0.5 * (vals["SW"] + vals["NE"])) / (2.0 * math.sqrt(2.0) * eta)
            + (ground - 0.5 * (vals["NW"] + vals["SE"])) / (2.0 * math.sqrt(2.0) * eta)
        )
        scaled = max(-0.5, min(0.5, float(raw)))
        return TerrainSample(
            longitude=float(lon), latitude=float(lat), ground_elevation_m=ground,
            slope_deg=float(slope), aspect_deg=float(aspect), curvature_raw=float(raw),
            curvature_scaled=float(scaled), dzdx=dzdx, dzdy=dzdy,
            zW=vals["W"], zE=vals["E"], zS=vals["S"], zN=vals["N"],
            zSW=vals["SW"], zNE=vals["NE"], zNW=vals["NW"], zSE=vals["SE"],
            curvature_radius_m=eta, source_path=str(self.path),
            elevation_band=int(self.elevation_band), slope_band=self.slope_band, aspect_band=self.aspect_band,
        )
