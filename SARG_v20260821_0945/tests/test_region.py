import json
from pathlib import Path

def test_geojson_region_and_endpoints(tmp_path: Path):
    from sarg_builder.region import load_region
    f=tmp_path/"r.geojson"
    f.write_text(json.dumps({"type":"FeatureCollection","features":[
      {"type":"Feature","properties":{"role":"region","region_id":"r1"},"geometry":{"type":"Polygon","coordinates":[[[0,0],[2,0],[2,2],[0,2],[0,0]]]}},
      {"type":"Feature","properties":{"role":"origin"},"geometry":{"type":"Point","coordinates":[.2,.2]}},
      {"type":"Feature","properties":{"role":"destination"},"geometry":{"type":"Point","coordinates":[1.8,1.8]}}
    ]}))
    r=load_region(f)
    assert r.region_id=="r1"
    assert r.origin_wgs84==(.2,.2)
    assert r.in_map_crs("EPSG:4326").contains_map(1,1)

def test_filter_crp_by_catalog_wgs84_points(tmp_path: Path):
    import pandas as pd
    from sarg_builder.io import filter_crp_to_region
    from sarg_builder.region import load_region
    f=tmp_path/"r.geojson"
    f.write_text(json.dumps({"type":"FeatureCollection","features":[
      {"type":"Feature","properties":{"role":"region"},"geometry":{"type":"Polygon","coordinates":[[[0,0],[2,0],[2,2],[0,2],[0,0]]]}},
      {"type":"Feature","properties":{"role":"origin"},"geometry":{"type":"Point","coordinates":[.2,.2]}},
      {"type":"Feature","properties":{"role":"destination"},"geometry":{"type":"Point","coordinates":[1.8,1.8]}}
    ]}))
    crp=tmp_path/"CRP"; crp.mkdir()
    df=pd.DataFrame({"cluster_id":[1,2],"centroid_lon":[1.0,3.0],"centroid_lat":[1.0,3.0]})
    df.to_csv(crp/"crp_cluster_catalog.csv",index=False)
    out,meta=filter_crp_to_region(df,crp,load_region(f).in_map_crs("EPSG:4326"))
    assert out.cluster_id.tolist()==[1]
    assert meta["method"]=="catalog_wgs84_points"

def test_filter_crp_reads_all_temporal_raster_bands(tmp_path: Path):
    import numpy as np
    import pandas as pd
    import rasterio
    from rasterio.transform import from_origin
    from sarg_builder.io import filter_crp_to_region
    from sarg_builder.region import load_region
    f=tmp_path/'r2.geojson'
    f.write_text(json.dumps({'type':'FeatureCollection','features':[
      {'type':'Feature','properties':{'role':'region'},'geometry':{'type':'Polygon','coordinates':[[[0,0],[2,0],[2,2],[0,2],[0,0]]]}},
      {'type':'Feature','properties':{'role':'origin'},'geometry':{'type':'Point','coordinates':[.2,.2]}},
      {'type':'Feature','properties':{'role':'destination'},'geometry':{'type':'Point','coordinates':[1.8,1.8]}}
    ]}))
    crp=tmp_path/'CRP'; crp.mkdir()
    df=pd.DataFrame({'cluster_id':[7,8,9,10]})
    df.to_csv(crp/'crp_cluster_catalog.csv',index=False)
    data=np.array([[[7,7],[7,7]],[[8,8],[8,8]],[[9,9],[9,9]],[[10,10],[10,10]]],dtype='int16')
    with rasterio.open(crp/'crp_cluster_map.tif','w',driver='GTiff',height=2,width=2,count=4,dtype='int16',crs='EPSG:4326',transform=from_origin(0,2,1,1),nodata=-1) as ds:
        ds.write(data)
        ds.descriptions=('00_06','06_12','12_18','18_24')
    out,meta=filter_crp_to_region(df,crp,load_region(f).in_map_crs('EPSG:4326'))
    assert sorted(out.cluster_id.tolist()) == [7,8,9,10]
    assert meta['complete_catalog_coverage'] is True
    assert meta['selected_cluster_count'] == 4

def test_filter_crp_audits_catalog_clusters_absent_from_region(tmp_path: Path):
    import numpy as np
    import pandas as pd
    import rasterio
    from rasterio.transform import from_origin
    from sarg_builder.io import filter_crp_to_region
    from sarg_builder.region import load_region
    f=tmp_path/'r3.geojson'
    f.write_text(json.dumps({'type':'FeatureCollection','features':[
      {'type':'Feature','properties':{'role':'region'},'geometry':{'type':'Polygon','coordinates':[[[0,0],[2,0],[2,2],[0,2],[0,0]]]}},
      {'type':'Feature','properties':{'role':'origin'},'geometry':{'type':'Point','coordinates':[.2,.2]}},
      {'type':'Feature','properties':{'role':'destination'},'geometry':{'type':'Point','coordinates':[1.8,1.8]}}
    ]}))
    crp=tmp_path/'CRP'; crp.mkdir()
    df=pd.DataFrame({'cluster_id':[7,8,28,29]})
    df.to_csv(crp/'crp_cluster_catalog.csv',index=False)
    data=np.array([[[7,7],[7,7]],[[8,8],[8,8]]],dtype='int16')
    with rasterio.open(crp/'crp_cluster_map.tif','w',driver='GTiff',height=2,width=2,count=2,dtype='int16',crs='EPSG:4326',transform=from_origin(0,2,1,1),nodata=-1) as ds:
        ds.write(data)
    out,meta=filter_crp_to_region(df,crp,load_region(f).in_map_crs('EPSG:4326'))
    assert sorted(out.cluster_id.tolist()) == [7,8]
    assert meta['catalog_clusters_absent_from_region'] == [28,29]
    assert meta['region_spatial_coverage_complete'] is True
    assert meta['catalog_scope_matches_region'] is False


def test_crs_roundtrip_wgs84_utm13():
    from sarg_builder.region import transform_point, validate_crs_roundtrip
    wgs=(-105.29, 40.02)
    xy=transform_point(wgs, "EPSG:32613")
    check=validate_crs_roundtrip(wgs, xy, "EPSG:32613", map_tolerance_m=0.05, geodetic_tolerance_deg=1e-8)
    assert check["valid"]
    assert check["forward_error_m"] <= 0.05
    assert check["reverse_error_deg"] <= 1e-8
