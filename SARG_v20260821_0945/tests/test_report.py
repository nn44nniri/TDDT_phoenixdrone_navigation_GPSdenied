from pathlib import Path
import json
import numpy as np
import pandas as pd
import pytest


def test_policy_report_generation_and_reference_endpoints(tmp_path: Path):
    rasterio=pytest.importorskip("rasterio"); pytest.importorskip("plotly")
    from rasterio.transform import from_origin
    from sarg_builder.report import ReportOptions, build_reports
    maps=tmp_path/"maps"; (maps/"route_3d_magnetic"/"overall_route").mkdir(parents=True)
    dem=maps/"terrain_dem.tif"; data=(100+np.add.outer(np.arange(24),np.arange(30))).astype("float32")
    with rasterio.open(dem,"w",driver="GTiff",height=24,width=30,count=1,dtype=data.dtype,crs="EPSG:32613",transform=from_origin(500000,4420000,10,10)) as ds: ds.write(data,1)
    summary={"origin":{"x":500020,"y":4419790},"destination":{"x":500270,"y":4419980}}
    (maps/"route_3d_magnetic"/"overall_route"/"boulder_route_overall_plotly_3d_summary.json").write_text(json.dumps(summary))
    from rasterio.warp import transform
    lon,lat=transform("EPSG:32613","EPSG:4326",[500020,500270,500290,500000],[4419790,4419980,4420000,4419760])
    region=tmp_path/"region.geojson"
    region.write_text(json.dumps({"type":"FeatureCollection","features":[
      {"type":"Feature","properties":{"role":"region","region_id":"test"},"geometry":{"type":"Polygon","coordinates":[[[lon[3],lat[3]],[lon[2],lat[3]],[lon[2],lat[2]],[lon[3],lat[2]],[lon[3],lat[3]]]]}},
      {"type":"Feature","properties":{"role":"origin"},"geometry":{"type":"Point","coordinates":[lon[0],lat[0]]}},
      {"type":"Feature","properties":{"role":"destination"},"geometry":{"type":"Point","coordinates":[lon[1],lat[1]]}}
    ]}))
    sarg=tmp_path/"SARG"; (sarg/"routes").mkdir(parents=True)
    pd.DataFrame([{"scenario_id":1,"regime_id":1,"regime_code":"R1","regime_name":"economic_range","active":1}]).to_csv(sarg/"scenarios.csv",index=False)
    rows=[]
    for p in range(1,7): rows.append({"scenario_id":1,"regime_id":1,"phase_id":p,"crp_id":7,"record_status":0,"observability_score":.9})
    pd.DataFrame(rows).to_csv(sarg/"sarg_records.csv",index=False)
    pd.DataFrame([{"group":"record","code":0,"name":"VALID"}]).to_csv(sarg/"status_codes.csv",index=False)
    t=np.linspace(0,1,61); phase=np.minimum(6,(t*6).astype(int)+1); phase[-1]=6
    pd.DataFrame({"point_id":range(61),"phase_id":phase,"x_m":500020+250*t,"y_m":4419790+190*t,"h_m":50+10*np.sin(np.pi*t)}).to_csv(sarg/"routes"/"S01_C007.csv",index=False)
    reports=build_reports(sarg,maps,options=ReportOptions(terrain_max_size=20,include_plotlyjs=True,region_file=str(region)))
    assert (reports/"policies"/"S01_R1_C007.html").is_file()
    q=pd.read_csv(reports/"policy_report_summary.csv"); assert len(q)==1 and q.loc[0,"phase_record_count"]==6
    meta=json.loads((reports/"report_metadata.json").read_text()); assert meta["endpoint_source"]["type"]=="geojson_wgs84"
    assert meta["confidence_column"]=="observability_score"
