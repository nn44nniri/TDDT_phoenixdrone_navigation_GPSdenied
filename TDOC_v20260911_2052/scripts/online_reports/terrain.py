"""Read-only DEM context for post-flight plots, with an explicit vertical frame."""
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Transformer

from .data import GEOD, load_json
from .provenance import file_hash, write_json


def _bilinear(ds, longitude, latitude):
    """Pixel-centre interpolation; outside/nodata stays NaN, never becomes ground=0."""
    x, y = Transformer.from_crs('EPSG:4326', ds.crs, always_xy=True).transform(longitude, latitude)
    inv = ~ds.transform
    col = inv.a*np.asarray(x) + inv.b*np.asarray(y) + inv.c
    row = inv.d*np.asarray(x) + inv.e*np.asarray(y) + inv.f
    valid = np.isfinite(col) & np.isfinite(row) & (col >= 0) & (row >= 0) & (col < ds.width) & (row < ds.height)
    col = np.clip(np.nan_to_num(col) - .5, 0, ds.width - 1)
    row = np.clip(np.nan_to_num(row) - .5, 0, ds.height - 1)
    c0, r0 = np.floor(col).astype(int), np.floor(row).astype(int)
    c1, r1 = np.minimum(c0 + 1, ds.width - 1), np.minimum(r0 + 1, ds.height - 1)
    dc, dr = col - c0, row - r0
    band = ds.read(1, masked=True).astype(float).filled(np.nan) * ds.scales[0] + ds.offsets[0]
    z = np.zeros(col.shape)
    for rr, cc, weight in ((r0, c0, (1-dr)*(1-dc)), (r0, c1, (1-dr)*dc),
                           (r1, c0, dr*(1-dc)), (r1, c1, dr*dc)):
        value = band[rr, cc]
        valid &= (weight <= 1e-15) | np.isfinite(value)
        z += np.where(weight > 1e-15, np.nan_to_num(value) * weight, 0.)
    return np.where(valid, z, np.nan)


def _finite_list(array):
    a = np.asarray(array)
    if a.ndim > 1:
        return [_finite_list(row) for row in a]
    return [float(v) if np.isfinite(v) else None for v in a]


def build_terrain(out, evidence, timeline, rows, result, config=None):
    """Archive a bounded sampled DEM and path/DEM comparisons; no navigation writes."""
    meta = {'schema': 'TDOC-online-terrain-context-v1', 'available': False,
        'used_for_control': False, 'terrain_constraint_certified': False,
        'vertical_reference': 'Declared DEM MSL contract; geoid/datum equivalence is not certified',
        'physical_path_vertical_reference': 'Unmodified SIM_STATE altitude MSL',
        'navigation_path_vertical_reference': 'Surveyed origin CG altitude MSL minus MESA down',
        'route_draped_or_shifted_to_terrain': False,
        'warning_fa': 'زمین، زمینهٔ DEM است؛ نمایش آن اثبات اعمال همین ناهمواری در تماس فیزیکی JSBSim نیست. ارتفاع مسیر برای منطبق‌کردن با زمین جابه‌جا نشده است.'}
    out, evidence = Path(out), Path(evidence)
    if rows:
        meta['origin_altitude_msl_m'] = rows[0]['L3']['origin']['altitude_msl_m']
        meta['origin_lonlat'] = [rows[0]['L3']['origin']['longitude_deg'], rows[0]['L3']['origin']['latitude_deg']]
    binding = load_json(evidence / 'native_session/binding.json')
    terrain_file = (binding.get('origin') or {}).get('terrain_file') or {}
    declared = terrain_file.get('path')
    raster = Path(declared) if declared else None
    meta['selection'] = 'same-session binding' if declared else 'configured map context; session binding unavailable'
    if raster is None and config is not None:
        root = config.path_value('TDDT_MAP_ROOT')
        if root:
            raster = root / 'terrain_obstacles.tif'
    grid, frame = None, pd.DataFrame(columns=['cycle', 'flight_elapsed_s', 'phase', 'terrain_elevation_msl_m'])
    try:
        if not timeline or not rows:
            raise ValueError('no flight records')
        if raster is None or not raster.is_file():
            raise ValueError('declared/configured DEM is unavailable')
        actual_hash = file_hash(raster)
        expected_hash = terrain_file.get('file_sha256')
        if expected_hash and expected_hash != actual_hash:
            raise ValueError('DEM hash differs from the Native session binding; no fallback substitution')
        import rasterio
        origin = rows[0]['L3']['origin']
        df = pd.DataFrame(timeline)
        finite = df[['physical_east_m', 'physical_north_m']].dropna()
        if finite.empty:
            raise ValueError('physical path coordinates unavailable')
        east, north = finite.physical_east_m.tolist(), finite.physical_north_m.tolist()
        if result.get('goal_lonlat'):
            az, _, distance = GEOD.inv(origin['longitude_deg'], origin['latitude_deg'], *result['goal_lonlat'])
            east.append(distance*np.sin(np.radians(az))); north.append(distance*np.cos(np.radians(az)))
        size = int(np.clip(config.get('reporting.terrain_grid_n', 90) if config else 90, 24, 160))
        ex = np.linspace(min(east)-250, max(east)+250, size)
        ny = np.linspace(min(north)-250, max(north)+250, size)
        ee, nn = np.meshgrid(ex, ny)
        lon, lat, _ = GEOD.fwd(np.full(ee.size, origin['longitude_deg']), np.full(ee.size, origin['latitude_deg']),
            np.degrees(np.arctan2(ee, nn)).ravel(), np.hypot(ee, nn).ravel())
        with rasterio.open(raster) as ds:
            if ds.crs is None:
                raise ValueError('DEM CRS missing')
            z = _bilinear(ds, lon, lat).reshape(ee.shape)
            if not np.isfinite(z).any():
                raise ValueError('flight region is outside valid DEM coverage')
            terrain = _bilinear(ds, df.physical_lon_deg.to_numpy(), df.physical_lat_deg.to_numpy())
            goal_elevation = _bilinear(ds, [result['goal_lonlat'][0]], [result['goal_lonlat'][1]])[0] if result.get('goal_lonlat') else np.nan
            frame = df[['cycle', 'flight_elapsed_s', 'phase', 'physical_lon_deg', 'physical_lat_deg',
                'physical_east_m', 'physical_north_m', 'physical_altitude_msl_m',
                'navigation_east_m', 'navigation_north_m']].copy()
            frame['navigation_altitude_msl_m'] = origin['altitude_msl_m'] - df.navigation_down_m
            frame['terrain_elevation_msl_m'] = terrain
            frame['physical_CG_minus_DEM_m'] = df.physical_altitude_msl_m - terrain
            meta.update(available=True, source_raster=str(raster.resolve()), source_sha256=actual_hash,
                binding_sha256_verified=expected_hash == actual_hash if expected_hash else None,
                source_crs=str(ds.crs), source_band=1, source_band_description=ds.descriptions[0],
                sampling='pixel-centre bilinear; masked/outside/nodata stays unavailable',
                grid_shape=[size, size], grid_valid_fraction=float(np.isfinite(z).mean()),
                grid_min_elevation_msl_m=float(np.nanmin(z)), grid_max_elevation_msl_m=float(np.nanmax(z)),
                route_valid_samples=int(np.isfinite(terrain).sum()), route_samples=len(df),
                origin_altitude_msl_m=origin['altitude_msl_m'],
                goal_terrain_elevation_msl_m=float(goal_elevation) if np.isfinite(goal_elevation) else None,
                negative_CG_minus_DEM_samples=int((frame.physical_CG_minus_DEM_m < 0).sum()),
                CG_minus_DEM_is_ground_clearance_certificate=False)
            grid = {'schema': 'TDOC-online-terrain-surface-v1', 'east_m': ex.tolist(), 'north_m': ny.tolist(),
                'elevation_msl_m': _finite_list(z), 'origin_lonlat': [origin['longitude_deg'], origin['latitude_deg']],
                'source_sha256': actual_hash, 'height_offset_m': 0., 'height_exaggeration': 1.}
    except (ValueError, OSError, ImportError) as exc:
        meta.update(available=False, reason=str(exc))
    write_json(out / 'TERRAIN_CONTEXT.json', meta)
    write_json(out / 'TERRAIN_SURFACE.json', grid or {'available': False, 'reason': meta.get('reason')})
    frame.to_csv(out / 'TERRAIN_ROUTE_HISTORY.csv', index=False)
    return meta, grid, frame
