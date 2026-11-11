from __future__ import annotations
import json
import hashlib
import subprocess
import sys
from pathlib import Path
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from site_spatial_contract import audit_dataset,SpatialContractError
from map_mosaic_query import sample_file
from region_geojson import load_region_geojson


def test_valid_source_is_diagnostic_not_native(region_assets):
    reg,base,o,d=region_assets
    a=audit_dataset(reg,base,vehicle_altitude_msl_m=1802.)
    assert a['status']=='MAP_CRP_SPATIAL_DIAGNOSTIC_PASS_UNCERTIFIED'
    assert a['crs']=='EPSG:32613'
    assert a['crp']['cluster_count']==1
    assert a['points']['origin']['terrain_elevation_m']==1798.
    assert a['agl_diagnostic']['origin_agl_unverified_m']==4.
    assert not a['native_agl_verified'] and not a['flight_authorized']
    assert a['coordinate_order']=='WGS84_LON_LAT_TO_UTM_EAST_NORTH'


def test_expected_identity_hash_contract(region_assets):
    reg,base,*_=region_assets
    good=hashlib.sha256(reg.read_bytes()).hexdigest()
    x=audit_dataset(reg,base,expected_region_sha256=good)
    assert x['region_sha256']==good
    with pytest.raises(SpatialContractError,match='sha256_mismatch'):
        audit_dataset(reg,base,expected_region_sha256='0'*64)
    with pytest.raises(SpatialContractError,match='sha256_mismatch'):
        audit_dataset(reg,base,expected_terrain_sha256='0'*64)


def test_missing_dem_fail_closed(region_assets):
    reg,base,*_=region_assets
    (base/'terrain_obstacles.tif').unlink()
    with pytest.raises(SpatialContractError,match='missing_required_artifacts'):audit_dataset(reg,base)


def test_outside_region_origin_fails(region_assets):
    reg,base,o,d=region_assets
    j=json.loads(reg.read_text());j['features'][1]['geometry']['coordinates']=[-106.,41.];reg.write_text(json.dumps(j))
    with pytest.raises(SpatialContractError,match='origin:outside_authoritative_region'):audit_dataset(reg,base)


def test_invalid_wgs84_point_rejected(region_assets):
    reg,base,o,d=region_assets
    j=json.loads(reg.read_text());j['features'][1]['geometry']['coordinates']=[181.,91.];reg.write_text(json.dumps(j))
    with pytest.raises(ValueError,match='WGS84'):load_region_geojson(reg)


def test_raster_crs_discrepancy_fails(region_assets):
    reg,base,o,d=region_assets
    with rasterio.open(base/'climate_contexts.tif','r+') as ds:ds.crs='EPSG:32612'
    with pytest.raises(SpatialContractError,match='crs_mismatch'):audit_dataset(reg,base)


def test_raster_grid_discrepancy_fails(region_assets):
    reg,base,o,d=region_assets
    with rasterio.open(base/'climate_contexts.tif','r+') as ds:ds.transform=from_origin(ds.bounds.left+5,ds.bounds.top,10,10)
    with pytest.raises(SpatialContractError,match='grid_mismatch'):audit_dataset(reg,base)


def test_dem_masked_origin_fails(region_assets):
    reg,base,o,d=region_assets
    with rasterio.open(base/'terrain_obstacles.tif','r+') as ds:
        arr=ds.read(1);arr[2,2]=-9999;ds.write(arr,1)
    with pytest.raises(SpatialContractError,match='terrain_sample_unavailable'):audit_dataset(reg,base)


def test_edge_pixel_half_open_and_nan_mask(region_assets):
    reg,base,o,d=region_assets
    terrain=base/'terrain_obstacles.tif'
    with rasterio.open(terrain) as ds:
        west,east=ds.bounds.left,ds.bounds.right
        north,south=ds.bounds.top,ds.bounds.bottom
        from pyproj import Transformer
        inv=Transformer.from_crs(ds.crs,'EPSG:4326',always_xy=True)
        # A 0.1m outside point avoids WGS84 round-trip floating point noise
        # exactly on the nominal outer pixel edge.
        rightlon,rightlat=inv.transform(east+0.1,(north+south)/2)
        lowlon,lowlat=inv.transform((west+east)/2,south-0.1)
    assert sample_file(terrain,rightlon,rightlat) is None
    assert sample_file(terrain,lowlon,lowlat) is None
    with rasterio.open(terrain,'r+') as ds:
        arr=ds.read(1);arr[2,2]=np.nan;ds.write(arr,1)
    assert sample_file(terrain,*o)['terrain_elevation_m'] is None


def test_crp_mismatch_missing_covariance_fails(region_assets):
    reg,base,o,d=region_assets
    p=base/'CRP/crp_runtime_statistics.json';j=json.loads(p.read_text())
    j['clusters'][0]['covariance']=[[1.]];p.write_text(json.dumps(j))
    with pytest.raises(SpatialContractError,match='dimension_mismatch'):audit_dataset(reg,base)


def test_crp_negative_eigenvalue_fails(region_assets):
    reg,base,o,d=region_assets
    p=base/'CRP/crp_runtime_statistics.json';j=json.loads(p.read_text())
    j['clusters'][0]['covariance']=[[-9.,0.],[0.,1.]];p.write_text(json.dumps(j))
    with pytest.raises(SpatialContractError,match='not_psd'):audit_dataset(reg,base)


def test_crp_cluster_catalog_ids_enforced(region_assets):
    reg,base,*_=region_assets
    (base/'CRP/crp_cluster_catalog.csv').write_text('cluster_id\n1\n')
    with pytest.raises(SpatialContractError,match='cluster_id_mismatch'):audit_dataset(reg,base)


def test_bad_agl_reference_fails(region_assets):
    reg,base,*_=region_assets
    with pytest.raises(SpatialContractError,match='nonfinite'):audit_dataset(reg,base,vehicle_altitude_msl_m=float('nan'))


def test_cli_positive_diagnostic_and_native_negative(region_assets,tmp_path):
    reg,base,*_=region_assets
    cli=Path(__file__).resolve().parents[1]/'scripts/validate_stage9_site_map.py'
    cmd=[sys.executable,str(cli),'--region',str(reg),'--dataset-dir',str(base)]
    q=tmp_path/'positive.json';p=subprocess.run(cmd+['--output',str(q)],capture_output=True,text=True)
    assert p.returncode==0,p.stderr
    assert json.loads(q.read_text())['status']=='MAP_CRP_SPATIAL_DIAGNOSTIC_PASS_UNCERTIFIED'
    native=tmp_path/'native.json';n=subprocess.run(cmd+['--require-native-agl','--output',str(native)],capture_output=True,text=True)
    assert n.returncode==2
    assert json.loads(native.read_text())['status']=='BLOCKED_NATIVE_AGL_CONFIRMATION_UNAVAILABLE'
    # Reports are immutable.
    assert subprocess.run(cmd+['--output',str(q)],capture_output=True).returncode==3


def test_cli_missing_assets_records_failure(region_assets,tmp_path):
    reg,base,*_=region_assets
    (base/'magnetic_anomaly.tif').unlink()
    cli=Path(__file__).resolve().parents[1]/'scripts/validate_stage9_site_map.py'
    report=tmp_path/'failure.json'
    r=subprocess.run([sys.executable,str(cli),'--region',str(reg),'--dataset-dir',str(base),'--output',str(report)],capture_output=True)
    assert r.returncode==2
    assert json.loads(report.read_text())['status']=='BLOCKED_SPATIAL_CONTRACT'


def test_crp_region_provenance_not_silently_reused(region_assets):
    reg,base,*_=region_assets
    meta=base/'CRP/metadata.json';m=json.loads(meta.read_text())
    m['region_geojson']['region_file_sha256']='0'*64;meta.write_text(json.dumps(m))
    with pytest.raises(SpatialContractError,match='region_sha256_mismatch'):audit_dataset(reg,base)


def test_crp_metadata_cluster_count_checked(region_assets):
    reg,base,*_=region_assets
    meta=base/'CRP/metadata.json';m=json.loads(meta.read_text());m['cluster_count']=32;meta.write_text(json.dumps(m))
    with pytest.raises(SpatialContractError,match='cluster_count_mismatch'):audit_dataset(reg,base)


def test_crp_metadata_feature_order_checked(region_assets):
    reg,base,*_=region_assets
    meta=base/'CRP/metadata.json';m=json.loads(meta.read_text());m['runtime_feature_order']=['other'];meta.write_text(json.dumps(m))
    with pytest.raises(SpatialContractError,match='feature_order_mismatch'):audit_dataset(reg,base)
