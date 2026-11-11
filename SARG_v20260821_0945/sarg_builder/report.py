from __future__ import annotations
import html, json, math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import numpy as np
import pandas as pd
from .region import load_region, point_in_geometry, transform_geometry

PHASE_NAMES={1:"takeoff_transition",2:"climb",3:"cruise",4:"turn_or_replan",5:"approach",6:"emergency_or_return"}

@dataclass(frozen=True)
class ReportOptions:
    terrain_max_size:int=180
    route_points:int=180
    vertical_exaggeration:float=1.0
    origin_x:float|None=None; origin_y:float|None=None
    destination_x:float|None=None; destination_y:float|None=None
    include_plotlyjs:str|bool="directory"
    region_file:str|None=None

def _terrain_score(path:Path):
    n=path.name.lower(); pos=sum(t in n for t in ("dem","elev","terrain","dtm","height")); neg=sum(t in n for t in ("slope","aspect","cluster","mask","landcover","obstacle")); return (pos-neg,-len(n),n)

def find_terrain_raster(map_path:str|Path)->Path:
    root=Path(map_path).expanduser().resolve(); c=[root] if root.is_file() else list(root.rglob("*.tif"))+list(root.rglob("*.tiff")); c=[p for p in c if p.is_file()]
    if not c: raise FileNotFoundError(f"No GeoTIFF terrain raster found under {root}")
    return sorted(c,key=_terrain_score,reverse=True)[0]

def _read_terrain(path:Path,max_size:int)->dict[str,Any]:
    import rasterio
    from rasterio.enums import Resampling
    with rasterio.open(path) as ds:
        scale=min(1.0,max_size/max(ds.width,ds.height)); w=max(2,int(round(ds.width*scale))); h=max(2,int(round(ds.height*scale)))
        z=np.asarray(ds.read(1,out_shape=(h,w),resampling=Resampling.bilinear,masked=True).filled(np.nan),dtype=float)
        finite=np.isfinite(z)
        if not finite.any(): raise ValueError(f"Terrain raster contains no finite values: {path}")
        z[~finite]=float(np.nanmedian(z[finite])); left,bottom,right,top=ds.bounds
        return {"x":np.linspace(left,right,w),"y":np.linspace(top,bottom,h),"z":z,"bounds":(left,bottom,right,top),"crs":ds.crs,"is_geographic":bool(ds.crs and ds.crs.is_geographic)}

def _sample(path:Path,x:np.ndarray,y:np.ndarray)->np.ndarray:
    import rasterio
    with rasterio.open(path) as ds:
        v=np.array([q[0] for q in ds.sample(zip(x,y),masked=True)],dtype=float)
        if ds.nodata is not None: v[np.isclose(v,ds.nodata)]=np.nan
    ok=np.isfinite(v)
    if not ok.any(): raise ValueError("Route does not intersect valid terrain")
    v[~ok]=np.interp(np.flatnonzero(~ok),np.flatnonzero(ok),v[ok]); return v

def _safe(v:str)->str: return "".join(c if c.isalnum() or c in "-_." else "_" for c in v)

def _route_file(sarg:Path, scenario_id:int, crp_id:int, records:pd.DataFrame)->Path:
    candidates=[sarg/"routes"/f"S{scenario_id:02d}_C{crp_id:03d}.csv"]
    if "route_file" in records.columns:
        candidates.extend(sarg/str(x) for x in records["route_file"].dropna().unique())
    for p in candidates:
        if p.is_file(): return p
    raise FileNotFoundError(f"Route file not found for S{scenario_id:02d}, C{crp_id:03d}")

def _normalize_route(route:pd.DataFrame,endpoints:tuple[float,float,float,float])->pd.DataFrame:
    xcol=next((c for c in ("x_m","map_x","x") if c in route),None); ycol=next((c for c in ("y_m","map_y","y") if c in route),None)
    if not xcol or not ycol: raise ValueError("Route file must contain x_m/y_m (or map_x/map_y)")
    r=route.copy(); r["map_x"]=pd.to_numeric(r[xcol]); r["map_y"]=pd.to_numeric(r[ycol])
    ox,oy,dx,dy=endpoints
    # Force exact shared mission endpoints while retaining the interior policy geometry.
    r.loc[r.index[0],["map_x","map_y"]]=[ox,oy]; r.loc[r.index[-1],["map_x","map_y"]]=[dx,dy]
    if "phase_id" not in r: r["phase_id"]=np.minimum(6,(np.linspace(0,1,len(r))*6).astype(int)+1)
    return r

def _write_index(report_dir:Path,summary:pd.DataFrame)->None:
    rows=[]
    for _,r in summary.iterrows():
        rows.append(f"<tr><td><a href='{html.escape(str(r.report_file))}'>{html.escape(str(r.policy_id))}</a></td><td>{r.regime_code}</td><td>{int(r.crp_id)}</td><td>{int(r.phase_record_count)}</td><td>{r.validation_status}</td></tr>")
    doc=f"""<!doctype html><html><head><meta charset='utf-8'><title>SARG policy reports</title><style>body{{font-family:Arial;margin:2rem}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #bbb;padding:.45rem}}th{{background:#eee}}</style></head><body><h1>SARG 3D policy reports</h1><p>Each report represents one complete policy R for one CRP context, from the validated Boulder origin to destination, with all mission phases overlaid on one route.</p><table><tr><th>Policy</th><th>Regime</th><th>CRP</th><th>Phase records</th><th>Status</th></tr>{''.join(rows)}</table></body></html>"""
    (report_dir/"index.html").write_text(doc,encoding="utf-8")

def build_reports(sarg_dir:str|Path,map_path:str|Path,report_dir:str|Path|None=None,options:ReportOptions|None=None)->Path:
    import plotly.graph_objects as go
    import plotly.io as pio
    options=options or ReportOptions(); sarg=Path(sarg_dir).expanduser().resolve(); out=Path(report_dir).expanduser().resolve() if report_dir else sarg/"reports"; out.mkdir(parents=True,exist_ok=True); pdir=out/"policies"; pdir.mkdir(exist_ok=True)
    recfile=sarg/"sarg_records.csv"
    if not recfile.is_file():
        legacy=sarg/"records.csv"
        if legacy.is_file(): recfile=legacy
    scfile=sarg/"scenarios.csv"
    if not recfile.is_file(): raise FileNotFoundError(f"Canonical SARG records not found: {recfile}")
    records=pd.read_csv(recfile); scenarios=pd.read_csv(scfile) if scfile.is_file() else pd.DataFrame()
    required={"scenario_id","regime_id","phase_id","crp_id","record_status"}; missing=required-set(records.columns)
    if missing: raise ValueError(f"{recfile.name} missing architecture fields: {sorted(missing)}")
    # Architecture confidence is generated from MESA-NAV observability. Accept the
    # explicit canonical name and the older FMI-only column for backward compatibility.
    if "record_confidence" in records.columns:
        confidence_column="record_confidence"
    elif "observability_score" in records.columns:
        confidence_column="observability_score"
    else:
        confidence_column=None
    terrain_path=find_terrain_raster(map_path); terrain=_read_terrain(terrain_path,options.terrain_max_size)
    region_path = options.region_file or str(sarg / "region_v1.geojson")
    region_def = load_region(region_path); region = region_def.in_map_crs(terrain["crs"])
    if all(v is not None for v in (options.origin_x,options.origin_y,options.destination_x,options.destination_y)):
        ox,oy,dx,dy = map(float,(options.origin_x,options.origin_y,options.destination_x,options.destination_y))
        endpoint_source={"type":"explicit_cli_override","region":region.metadata()}
    else:
        ox,oy=region.origin_map; dx,dy=region.destination_map
        endpoint_source={"type":"geojson_wgs84","region":region.metadata()}
    for label,px,py in (("Origin",ox,oy),("Destination",dx,dy)):
        if not region.contains_map(px,py): raise ValueError(f"{label} lies outside region_v1.geojson")
    # Mask the displayed terrain outside the authoritative GeoJSON polygon.
    xx,yy=np.meshgrid(terrain["x"],terrain["y"]); inside=np.vectorize(region.contains_map)(xx,yy)
    terrain["z"]=np.where(inside,terrain["z"],np.nan)
    status_map={}
    sf=sarg/"status_codes.csv"
    if sf.is_file():
        sdf=pd.read_csv(sf); status_map={int(r.code):str(r["name"]) for _,r in sdf[sdf["group"]=="record"].iterrows()}
    summaries=[]; all_points=[]
    groups=records.sort_values(["scenario_id","crp_id","phase_id"]).groupby(["scenario_id","regime_id","crp_id"],sort=True)
    for (sid,rid,cid),g in groups:
        route=_normalize_route(pd.read_csv(_route_file(sarg,int(sid),int(cid),g)),(ox,oy,dx,dy))
        outside=[i for i,(rx,ry) in enumerate(zip(route.map_x,route.map_y)) if not region.contains_map(float(rx),float(ry))]
        if outside: raise ValueError(f"Route S{int(sid):02d}_C{int(cid):03d} has points outside region_v1.geojson; first index={outside[0]}")
        x=route.map_x.to_numpy(float); y=route.map_y.to_numpy(float); terrain_z=_sample(terrain_path,x,y)
        if "h_m" in route: raw=pd.to_numeric(route.h_m,errors="coerce").to_numpy(float); flight_z=np.where(np.isfinite(raw),raw,terrain_z+30)
        else: flight_z=terrain_z+30
        # Existing route h_m may be AGL in older packages. Detect and convert to MSL.
        if float(np.nanmedian(flight_z)) < float(np.nanmedian(terrain_z))*0.5: flight_z=terrain_z+flight_z
        phase=route.phase_id.astype(int).to_numpy(); clearance=flight_z-terrain_z
        scenario_row=scenarios[scenarios.scenario_id==sid].iloc[0] if not scenarios.empty and (scenarios.scenario_id==sid).any() else None
        regime_code=str(scenario_row.regime_code) if scenario_row is not None and "regime_code" in scenario_row else f"R{int(rid)}"
        regime_name=str(scenario_row.regime_name) if scenario_row is not None and "regime_name" in scenario_row else ""
        policy_id=f"S{int(sid):02d}_{regime_code}_C{int(cid):03d}"
        fig=go.Figure(); fig.add_trace(go.Surface(x=terrain["x"],y=terrain["y"],z=terrain["z"]*options.vertical_exaggeration,colorscale="Earth",showscale=True,colorbar={"title":"Terrain m"},name="Terrain",hovertemplate="X: %{x}<br>Y: %{y}<br>Terrain: %{z:.2f} m<extra></extra>"))
        for pid in sorted(set(phase)):
            mask=phase==pid; rr=g[g.phase_id==pid]; rec=rr.iloc[0] if not rr.empty else None
            status=int(rec.record_status) if rec is not None else -1
            conf=float(rec[confidence_column]) if rec is not None and confidence_column and pd.notna(rec[confidence_column]) else float("nan")
            confidence_text=f"{conf:.3f}" if math.isfinite(conf) else "N/A"
            crp_code = str(rec.get("crp_code", f"CRP-C{int(cid):03d}")) if rec is not None else f"CRP-C{int(cid):03d}"
            temperature_state = str(rec.get("crp_temperature_state", "")) if rec is not None else ""
            humidity_state = str(rec.get("crp_humidity_state", "")) if rec is not None else ""
            wind_state = str(rec.get("crp_wind_state", "")) if rec is not None else ""
            temp = rec.get("crp_temperature_mean_c", float("nan")) if rec is not None else float("nan")
            rh = rec.get("crp_humidity_mean_pct", float("nan")) if rec is not None else float("nan")
            ws = rec.get("crp_wind_speed_mean_ms", float("nan")) if rec is not None else float("nan")
            wd = rec.get("crp_wind_direction_mean_deg", float("nan")) if rec is not None else float("nan")
            climate_text = (f"CRP code: {crp_code}<br>Temperature state: {temperature_state}"
                            f"<br>Humidity state: {humidity_state}<br>Wind state: {wind_state}"
                            f"<br>Temperature: {temp:.2f} °C<br>Humidity: {rh:.2f} %"
                            f"<br>Wind: {ws:.2f} m/s @ {wd:.1f}°")
            hover=[f"Policy: {policy_id}<br>Regime: {regime_code} ({regime_name})<br>Phase: P{pid} — {PHASE_NAMES.get(pid,'unknown')}<br>{climate_text}<br>Record status: {status_map.get(status,status)}<br>Confidence: {confidence_text}<br>Terrain: {tz:.2f} m<br>Flight: {fz:.2f} m<br>Clearance: {cl:.2f} m" for tz,fz,cl in zip(terrain_z[mask],flight_z[mask],clearance[mask])]
            fig.add_trace(go.Scatter3d(x=x[mask],y=y[mask],z=flight_z[mask]*options.vertical_exaggeration,mode="lines+markers",line={"width":8},marker={"size":2.5},text=hover,hovertemplate="%{text}<extra></extra>",name=f"P{pid} {PHASE_NAMES.get(pid,'')}"))
        fig.add_trace(go.Scatter3d(x=[ox],y=[oy],z=[flight_z[0]*options.vertical_exaggeration],mode="markers+text",marker={"size":9,"symbol":"diamond"},text=["ORIGIN"],textposition="top center",name="Origin")); fig.add_trace(go.Scatter3d(x=[dx],y=[dy],z=[flight_z[-1]*options.vertical_exaggeration],mode="markers+text",marker={"size":9,"symbol":"diamond"},text=["DESTINATION"],textposition="top center",name="Destination"))
        valid=bool(len(g)==6 and set(g.phase_id.astype(int))==set(range(1,7))); overall="PASS" if valid else "INCOMPLETE"
        fig.update_layout(title=f"SARG policy {policy_id}<br><sup>One complete policy from Origin to Destination; all six phase records; architecture validation={overall}</sup>",scene={"xaxis_title":"Longitude" if terrain["is_geographic"] else "Map X","yaxis_title":"Latitude" if terrain["is_geographic"] else "Map Y","zaxis_title":"Elevation / flight altitude (m)","aspectmode":"data"},legend={"orientation":"h"},margin={"l":0,"r":0,"b":0,"t":95})
        filename=f"policies/{_safe(policy_id)}.html"; pio.write_html(fig,out/filename,include_plotlyjs=options.include_plotlyjs,full_html=True,auto_open=False)
        rp=route.copy(); rp.insert(0,"policy_id",policy_id); rp["terrain_elevation_m"]=terrain_z; rp["flight_altitude_m"]=flight_z; rp["terrain_clearance_m"]=clearance; all_points.append(rp)
        first_record=g.iloc[0]
        summaries.append({"policy_id":policy_id,"scenario_id":int(sid),"regime_id":int(rid),"regime_code":regime_code,"crp_id":int(cid),
            "crp_code":first_record.get("crp_code",f"CRP-C{int(cid):03d}"),
            "crp_temperature_state":first_record.get("crp_temperature_state",""),
            "crp_humidity_state":first_record.get("crp_humidity_state",""),
            "crp_wind_state":first_record.get("crp_wind_state",""),
            "crp_temperature_mean_c":first_record.get("crp_temperature_mean_c",float("nan")),
            "crp_humidity_mean_pct":first_record.get("crp_humidity_mean_pct",float("nan")),
            "crp_wind_speed_mean_ms":first_record.get("crp_wind_speed_mean_ms",float("nan")),
            "crp_wind_direction_mean_deg":first_record.get("crp_wind_direction_mean_deg",float("nan")),
            "phase_record_count":len(g),"route_point_count":len(route),"minimum_terrain_clearance_m":float(np.min(clearance)),
            "origin_x_m":ox,"origin_y_m":oy,"destination_x_m":dx,"destination_y_m":dy,
            "origin_lon_deg":region_def.origin_wgs84[0],"origin_lat_deg":region_def.origin_wgs84[1],
            "destination_lon_deg":region_def.destination_wgs84[0],"destination_lat_deg":region_def.destination_wgs84[1],
            "validation_status":overall,"report_file":filename})
    summary=pd.DataFrame(summaries); summary.to_csv(out/"policy_report_summary.csv",index=False)
    if all_points: pd.concat(all_points,ignore_index=True).to_csv(out/"route_points_all_policies.csv.gz",index=False,compression="gzip")
    _write_index(out,summary)
    expected=10*records.crp_id.nunique()
    metadata={"report_type":"SARG policy-level full-map 3D validation","created_utc":datetime.now(timezone.utc).isoformat(),"policy_report_count":len(summary),"expected_policy_report_count_formula":"10 * dynamic CRP cluster count","expected_policy_report_count":expected,"report_count_validated":bool(len(summary)==expected),"record_source_file":recfile.name,"record_count":len(records),"record_formula":"10 * 6 * dynamic CRP cluster count","terrain_raster":str(terrain_path),"terrain_crs":str(terrain["crs"]),"terrain_bounds":list(terrain["bounds"]),
        "coordinate_reference":{"geodetic_crs":"EPSG:4326","map_crs":str(terrain["crs"]),"local_frame":"ENU","vertical_reference":"MSL","coordinate_order_geodetic":"longitude,latitude","linear_unit":"m","angular_unit":"deg"},
        "origin":{"lon_deg":region_def.origin_wgs84[0],"lat_deg":region_def.origin_wgs84[1],"x_m":ox,"y_m":oy},
        "destination":{"lon_deg":region_def.destination_wgs84[0],"lat_deg":region_def.destination_wgs84[1],"x_m":dx,"y_m":dy},
        "endpoint_source":endpoint_source,"region":region.metadata(),"region_geometry_enforced":True,"confidence_column":confidence_column,"confidence_semantics":"MESA-NAV observability-derived phase confidence; N/A when unavailable","options":asdict(options),"outputs":{"index":"index.html","summary":"policy_report_summary.csv","route_points":"route_points_all_policies.csv.gz","policy_directory":"policies"}}
    (out/"report_metadata.json").write_text(json.dumps(metadata,indent=2,ensure_ascii=False),encoding="utf-8"); return out
