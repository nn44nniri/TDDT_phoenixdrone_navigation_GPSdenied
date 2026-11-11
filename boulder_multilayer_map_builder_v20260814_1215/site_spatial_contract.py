"""Read-only Stage-9 map/CRP source-identity, CRS and AGL diagnostic contract.

This audit never builds a DEM, fixes a raster, rewrites CRP, grants flight
permission or treats GPS/simulator truth as authoritative MESA state.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from pyproj import Transformer
from shapely.geometry import Point, shape

from region_geojson import load_region_geojson
from map_mosaic_query import sample_file

SCHEMA = 'TDDT-Boulder-SpatialBinding-v1'
REQUIRED = ('terrain_obstacles.tif', 'climate_contexts.tif', 'magnetic_anomaly.tif',
            'CRP/crp_cluster_map.tif', 'CRP/crp_runtime_statistics.json',
            'CRP/crp_cluster_catalog.csv', 'CRP/metadata.json')


class SpatialContractError(ValueError):
    pass


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def _finite(v: Any, name: str) -> float:
    try:
        if isinstance(v, bool): raise TypeError()
        value = float(v)
    except (TypeError, ValueError) as e:
        raise SpatialContractError(f'{name}:not_numeric') from e
    if not math.isfinite(value):
        raise SpatialContractError(f'{name}:nonfinite')
    return value


def _check_hash(actual: str, expected: str | None, name: str) -> None:
    if expected is not None:
        if len(expected) != 64 or any(x not in '0123456789abcdef' for x in expected):
            raise SpatialContractError(f'{name}:invalid_expected_sha256')
        if actual != expected:
            raise SpatialContractError(f'{name}:sha256_mismatch')


def _read_source_grid(path: Path, raster_crs: Any = None, grid: Any = None) -> dict:
    with rasterio.open(path) as ds:
        if ds.crs is None:
            raise SpatialContractError(f'{path.name}:crs_missing')
        if ds.width < 1 or ds.height < 1 or ds.count < 1:
            raise SpatialContractError(f'{path.name}:invalid_dimensions')
        if ds.crs.is_geographic or not ds.crs.is_projected:
            raise SpatialContractError(f'{path.name}:projected_metric_crs_required')
        unit_factor = float(ds.crs.linear_units_factor[1])
        if not math.isfinite(unit_factor) or abs(unit_factor - 1.0) > 1e-10:
            raise SpatialContractError(f'{path.name}:raster_unit_must_be_meters')
        if raster_crs is not None and ds.crs != raster_crs:
            raise SpatialContractError(f'{path.name}:crs_mismatch')
        sig=(ds.width,ds.height,tuple(ds.transform))
        if grid is not None and grid != sig:
            raise SpatialContractError(f'{path.name}:grid_mismatch')
        return {'crs':ds.crs,'grid':sig,'metadata':{'sha256':_hash(path),'crs':str(ds.crs),
                'dimensions':[ds.width,ds.height],'affine':list(tuple(ds.transform)),
                'bands':list(ds.descriptions),'nodata':ds.nodata,'dtype':list(ds.dtypes)}}


def _validate_crp(stats_path: Path, catalog_path: Path) -> dict:
    try:
        obj=json.loads(stats_path.read_text(encoding='utf-8'))
    except (OSError,ValueError) as e:
        raise SpatialContractError('crp_runtime_statistics:invalid_json') from e
    if not isinstance(obj,dict) or obj.get('schema') != 'CRP-runtime-statistics-v1':
        raise SpatialContractError('crp_runtime_statistics:schema_mismatch')
    order=obj.get('feature_order');cl=obj.get('clusters')
    if not isinstance(order,list) or not order or len(set(order))!=len(order) or not all(isinstance(x,str) and x for x in order):
        raise SpatialContractError('crp_runtime_statistics:feature_order_invalid')
    if not isinstance(cl,list) or not cl:
        raise SpatialContractError('crp_runtime_statistics:clusters_missing')
    ids=[]; total=0
    for x in cl:
        if not isinstance(x,dict):raise SpatialContractError('crp_cluster:invalid')
        cid=x.get('cluster_id');ids.append(cid)
        if not isinstance(cid,int) or isinstance(cid,bool):raise SpatialContractError('crp_cluster:id_invalid')
        n=x.get('sample_count')
        if not isinstance(n,int) or isinstance(n,bool) or n<=0:raise SpatialContractError('crp_cluster:sample_count_invalid')
        total+=n
        q=_finite(x.get('quality_score'),'crp_quality_score')
        if q<0 or q>1:raise SpatialContractError('crp_cluster:quality_out_of_range')
        d=len(order)
        m=np.asarray(x.get('centroid'),dtype=np.float64)
        cov=np.asarray(x.get('covariance'),dtype=np.float64)
        reg=np.asarray(x.get('covariance_regularized'),dtype=np.float64)
        if m.shape!=(d,) or cov.shape!=(d,d) or reg.shape!=(d,d):
            raise SpatialContractError('crp_cluster:dimension_mismatch')
        if not (np.isfinite(m).all() and np.isfinite(cov).all() and np.isfinite(reg).all()):
            raise SpatialContractError('crp_cluster:nonfinite')
        if not (np.allclose(cov,cov.T,atol=1e-8) and np.allclose(reg,reg.T,atol=1e-8)):
            raise SpatialContractError('crp_cluster:covariance_not_symmetric')
        if np.linalg.eigvalsh(cov).min() < -1e-6 * max(1.0,float(np.linalg.norm(cov,ord=2))):
            raise SpatialContractError('crp_cluster:covariance_not_psd')
        if np.linalg.eigvalsh(reg).min() <= 0:
            raise SpatialContractError('crp_cluster:regularized_covariance_not_pd')
    if len(ids)!=len(set(ids)):
        raise SpatialContractError('crp_cluster:duplicate_ids')
    try:
        with catalog_path.open(encoding='utf-8',newline='') as f:
            reader=csv.DictReader(f)
            if not reader.fieldnames or 'cluster_id' not in reader.fieldnames:
                raise SpatialContractError('crp_catalog:cluster_id_missing')
            catalog={int(row['cluster_id']) for row in reader}
    except (OSError,ValueError,KeyError) as e:
        raise SpatialContractError('crp_catalog:invalid') from e
    if catalog!=set(ids):raise SpatialContractError('crp_catalog:cluster_id_mismatch')
    return {'schema':obj['schema'],'cluster_count':len(ids),'feature_order':order,
            'sample_count_total':total,'runtime_statistics_sha256':_hash(stats_path),
            'catalog_sha256':_hash(catalog_path)}


def audit_dataset(region_file: str|Path, dataset_dir: str|Path, *,
                  vehicle_altitude_msl_m: float|None=None,
                  expected_region_sha256: str|None=None,
                  expected_terrain_sha256: str|None=None) -> dict:
    """Only diagnostic maps; externally trusted manifests needed to authenticate source.

    An AGL diagnostic difference requires an explicit altitude and samples an
    alleged MSL DEM, but it is never elevated to a physical AGL observation.
    """
    base=Path(dataset_dir).resolve()
    reg=load_region_geojson(region_file)
    _check_hash(reg.sha256,expected_region_sha256,'region')
    shape_reg=shape(reg.geometry)
    if shape_reg.is_empty or not shape_reg.is_valid:
        raise SpatialContractError('region_geometry_invalid')
    if reg.origin is None or reg.destination is None:
        raise SpatialContractError('region_origin_destination_required')
    points={'origin':reg.origin,'destination':reg.destination}
    for name,(lon,lat) in points.items():
        if not shape_reg.covers(Point(lon,lat)):
            raise SpatialContractError(f'{name}:outside_authoritative_region')
    paths={rel:base/rel for rel in REQUIRED}
    missing=[rel for rel,path in paths.items() if not path.is_file()]
    if missing:
        raise SpatialContractError('missing_required_artifacts:'+','.join(missing))
    outputs={};ref_crs=None;ref_grid=None
    for rel in REQUIRED[:4]:
        o=_read_source_grid(paths[rel],ref_crs,ref_grid)
        ref_crs=o['crs'];ref_grid=o['grid'];outputs[rel]=o['metadata']
    if outputs['terrain_obstacles.tif']['bands'].count('terrain_elevation_m')!=1:
        raise SpatialContractError('terrain_elevation_m_band_missing_or_duplicate')
    _check_hash(outputs['terrain_obstacles.tif']['sha256'],expected_terrain_sha256,'terrain')
    crp=_validate_crp(paths['CRP/crp_runtime_statistics.json'],paths['CRP/crp_cluster_catalog.csv'])
    try:
        metadata=json.loads(paths['CRP/metadata.json'].read_text(encoding='utf-8'))
    except (OSError,ValueError) as e:
        raise SpatialContractError('crp_metadata:invalid_json') from e
    provenance=metadata.get('region_geojson') or {}
    if provenance.get('region_file_sha256') != reg.sha256:
        raise SpatialContractError('crp_metadata:region_sha256_mismatch')
    if metadata.get('cluster_count') != crp['cluster_count']:
        raise SpatialContractError('crp_metadata:cluster_count_mismatch')
    if metadata.get('runtime_feature_order') != crp['feature_order']:
        raise SpatialContractError('crp_metadata:feature_order_mismatch')
    crp['metadata_sha256']=_hash(paths['CRP/metadata.json'])
    origin_points={}
    for name,(lon,lat) in points.items():
        samp=sample_file(paths['terrain_obstacles.tif'],lon,lat)
        if samp is None or samp.get('terrain_elevation_m') is None:
            raise SpatialContractError(f'{name}:terrain_sample_unavailable_or_nodata')
        elevation=_finite(samp['terrain_elevation_m'],f'{name}:terrain_elevation_m')
        origin_points[name]={'wgs84_lon_lat':[lon,lat], 'projected_coordinate':samp['projected_coordinate'],
                             'pixel':samp['pixel'],'terrain_elevation_m':elevation,
                             'terrain_vertical_datum_verified':False}
    # GIS coordinates are Easting/Northing; NEVER reinterpret as NED position.
    result={'schema':SCHEMA,'status':'MAP_CRP_SPATIAL_DIAGNOSTIC_PASS_UNCERTIFIED',
            'region_sha256':reg.sha256,'region_file':str(reg.path),
            'dataset_dir':str(base),'crs':str(ref_crs),
            'grid_width_height':[ref_grid[0],ref_grid[1]],
            'raster_files':outputs,'crp':crp,'points':origin_points,
            'coordinate_order':'WGS84_LON_LAT_TO_UTM_EAST_NORTH',
            'vertical_datum_authenticated':False,'native_agl_verified':False,
            'native_controller_certified':False,'flight_authorized':False,
            'evidence_scope':'FILE_AND_SPATIAL_CONTRACT_ONLY_NOT_NATIVE_OR_NAVIGATION_AUTHORITY'}
    if vehicle_altitude_msl_m is not None:
        alt=_finite(vehicle_altitude_msl_m,'vehicle_altitude_msl_m')
        result['agl_diagnostic']={'vehicle_altitude_msl_m_declared':alt,
                'origin_agl_unverified_m':alt-origin_points['origin']['terrain_elevation_m'],
                'warning':'DIAGNOSTIC_DIFFERENCE_ONLY_NO_VERTICAL_DATUM_OR_SENSOR_PROOF'}
    return result
