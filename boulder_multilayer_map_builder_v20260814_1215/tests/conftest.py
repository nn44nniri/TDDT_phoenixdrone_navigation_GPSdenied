import json
import hashlib
from pathlib import Path
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from pyproj import Transformer

@pytest.fixture
def region_assets(tmp_path):
    base=tmp_path/'outputs'/'region_v1_map';(base/'CRP').mkdir(parents=True)
    # Deliberately distinct NE and ENU coordinates; no axis swap in query.
    tx=Transformer.from_crs('EPSG:4326','EPSG:32613',always_xy=True)
    inv=Transformer.from_crs('EPSG:32613','EPSG:4326',always_xy=True)
    x,y=tx.transform(-105.30,40.01)
    transform=from_origin(x-40,y+40,10,10)
    def center(row,col):return inv.transform(x-40+(col+.5)*10,y+40-(row+.5)*10)
    def corner(col,row):return inv.transform(x-40+col*10,y+40-row*10)
    bounds=[corner(1,1),corner(7,1),corner(7,7),corner(1,7),corner(1,1)]
    origin=center(2,2);dest=center(5,5)
    reg={'type':'FeatureCollection','features':[
       {'type':'Feature','properties':{'role':'boundary'},'geometry':{'type':'Polygon','coordinates':[bounds]}},
       {'type':'Feature','properties':{'role':'origin'},'geometry':{'type':'Point','coordinates':list(origin)}},
       {'type':'Feature','properties':{'role':'destination'},'geometry':{'type':'Point','coordinates':list(dest)}}]}
    region=tmp_path/'region.geojson';region.write_text(json.dumps(reg))
    def tif(path,name,val):
        d=np.full((8,8),val,dtype='float32')
        with rasterio.open(path,'w',driver='GTiff',width=8,height=8,count=1,
                           crs='EPSG:32613',transform=transform,dtype='float32',nodata=-9999.) as dst:
            dst.write(d,1);dst.set_band_description(1,name)
    tif(base/'terrain_obstacles.tif','terrain_elevation_m',1798.0)
    tif(base/'climate_contexts.tif','temperature_c',12.0)
    tif(base/'magnetic_anomaly.tif','magnetic_anomaly_nt',0.0)
    tif(base/'CRP/crp_cluster_map.tif','crp_cluster_id',0.)
    stats={'schema':'CRP-runtime-statistics-v1','feature_order':['terrain_elevation_m','wind_speed_ms'],
       'clusters':[{'cluster_id':0,'centroid':[1798.,3.],
                    'covariance':[[4.,0.],[0.,.2]],'covariance_regularized':[[4.0001,0.],[0.,.2001]],
                    'sample_count':25,'quality_score':0.6}]}
    (base/'CRP/crp_runtime_statistics.json').write_text(json.dumps(stats))
    (base/'CRP/crp_cluster_catalog.csv').write_text('cluster_id,cluster_code\n0,CRP_00\n')
    metadata={'region_geojson':{'region_file_sha256':hashlib.sha256(region.read_bytes()).hexdigest()},
             'cluster_count':1,'runtime_feature_order':stats['feature_order']}
    (base/'CRP/metadata.json').write_text(json.dumps(metadata))
    return region,base,origin,dest
