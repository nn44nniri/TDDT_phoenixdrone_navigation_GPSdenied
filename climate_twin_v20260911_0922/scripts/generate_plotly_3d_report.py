#!/usr/bin/env python3
"""Build an animated Plotly 3D climate-over-DEM report.

This implementation keeps the HTML lightweight and browser-friendly:
- the HTML page contains only the user interface and rendering logic;
- the large numeric payload is written to a sidecar JavaScript file;
- the climate colour surface is interpolated on demand in the browser instead of
  precomputing all parameter/time rasters in Python.

The result is a much smaller HTML document that opens reliably in common browsers.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.warp import calculate_default_transform, reproject, transform_bounds

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

META_COLUMNS = {
    "grid_cell_id", "grid_ix", "grid_iy", "grid_iz", "latitude", "longitude",
    "elevation_m", "altitude_msl_m", "altitude_agl_m", "ground_elevation_m",
    "time", "time_index", "resolution_m", "vertical_reference", "vertical_valid",
    "vertical_sampling_mode",
}

LABELS = {
    "temperature_local": "Local air temperature (°C)",
    "relative_humidity_local": "Local relative humidity (%)",
    "wind_speed_terrain": "Terrain-adjusted wind speed (m/s)",
    "wind_direction_terrain_deg": "Terrain-adjusted wind direction (°)",
    "surface_pressure_pa": "Surface pressure (Pa)",
    "shortwave_radiation": "Incoming shortwave radiation (W/m²)",
    "longwave_radiation": "Incoming longwave radiation (W/m²)",
    "precipitation_local": "Local precipitation (mm/h)",
    "cloud_fraction": "Cloud fraction",
    "dewpoint_local": "Local dew-point temperature (°C)",
}


def parse_elevation(value: str) -> float:
    text = value.strip().lower().replace(" ", "")
    m = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))(m|km)?", text)
    if not m:
        raise argparse.ArgumentTypeError("Use an elevation such as 100, 100m, or 0.1km")
    return float(m.group(1)) * (1000.0 if m.group(2) == "km" else 1.0)


def slug_number(value: float) -> str:
    return f"{value:g}".replace("-", "minus_").replace(".", "p")


def find_dem(path: Path) -> Path:
    if path.is_file():
        return path
    candidates = [
        path / "terrain_obstacles.tif",
        path / "dem.tif",
        path / "terrain.tif",
        path / "elevation.tif",
    ]
    for c in candidates:
        if c.exists():
            return c
    tifs = sorted(path.glob("*.tif")) + sorted(path.glob("*.tiff"))
    for c in tifs:
        name = c.name.lower()
        if "terrain" in name or "dem" in name or "elevation" in name:
            return c
    raise FileNotFoundError(
        f"No DEM GeoTIFF found under {path}. Expected terrain_obstacles.tif or a DEM/elevation TIFF."
    )


def read_dem(path: Path, max_size: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    """Read and regularize a DEM on a 2-D WGS84 mesh.

    Real map rasters may be projected, may include NoData sentinels, and may have
    a scale/offset.  Building longitude/latitude from the complete raster bounds
    avoids the degenerate one-latitude-line problem that can occur when sampled
    pixel centres are transformed independently.
    """
    with rasterio.open(path) as ds:
        if ds.count < 1:
            raise ValueError(f"DEM has no raster bands: {path}")
        if ds.crs is None:
            raise ValueError(f"DEM CRS is missing: {path}")

        # Preserve the complete map extent while limiting browser-side mesh size.
        scale = min(1.0, float(max_size) / max(ds.width, ds.height))
        out_width = max(2, int(round(ds.width * scale)))
        out_height = max(2, int(round(ds.height * scale)))

        dst_crs = "EPSG:4326"
        west, south, east, north = transform_bounds(ds.crs, dst_crs, *ds.bounds, densify_pts=21)
        if not all(math.isfinite(v) for v in (west, south, east, north)) or east <= west or north <= south:
            raise ValueError(f"Invalid transformed DEM bounds: {(west, south, east, north)}")
        dst_transform = from_bounds(west, south, east, north, out_width, out_height)
        dst = np.full((out_height, out_width), np.nan, dtype=np.float64)

        src_nodata = ds.nodata
        reproject(
            source=rasterio.band(ds, 1),
            destination=dst,
            src_transform=ds.transform,
            src_crs=ds.crs,
            src_nodata=src_nodata,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )

        band_scale = float(ds.scales[0]) if ds.scales and ds.scales[0] is not None else 1.0
        band_offset = float(ds.offsets[0]) if ds.offsets and ds.offsets[0] is not None else 0.0
        # Rasterio usually applies neither scale nor offset during read/reproject.
        if band_scale != 1.0 or band_offset != 0.0:
            dst = dst * band_scale + band_offset

        # Remove common sentinel/outlier values while retaining realistic mountain terrain.
        dst[~np.isfinite(dst)] = np.nan
        dst[(dst < -500.0) | (dst > 9000.0)] = np.nan
        finite = dst[np.isfinite(dst)]
        if finite.size == 0:
            raise ValueError("DEM contains no finite terrestrial elevation values after cleaning")
        q_low, q_high = np.percentile(finite, [0.05, 99.95])
        robust_span = max(50.0, q_high - q_low)
        dst[(dst < q_low - 5.0 * robust_span) | (dst > q_high + 5.0 * robust_span)] = np.nan

        xs = west + (np.arange(out_width) + 0.5) * (east - west) / out_width
        ys = north - (np.arange(out_height) + 0.5) * (north - south) / out_height
        lon, lat = np.meshgrid(xs, ys)

        unique_lon = int(np.unique(np.round(lon[np.isfinite(lon)], 10)).size)
        unique_lat = int(np.unique(np.round(lat[np.isfinite(lat)], 10)).size)
        if unique_lon < 2 or unique_lat < 2:
            raise ValueError(
                f"Degenerate geographic DEM grid: unique longitudes={unique_lon}, "
                f"unique latitudes={unique_lat}. Check the raster CRS and bounds."
            )

        meta = {
            "path": str(path), "crs": str(ds.crs), "width": ds.width, "height": ds.height,
            "band": 1, "band_scale": band_scale, "band_offset": band_offset,
            "downsampled_shape": [int(dst.shape[0]), int(dst.shape[1])],
            "wgs84_bounds": [float(west), float(south), float(east), float(north)],
            "unique_longitudes": unique_lon, "unique_latitudes": unique_lat,
            "elevation_min_msl_m": float(np.nanmin(dst)),
            "elevation_max_msl_m": float(np.nanmax(dst)),
        }
    return lon, lat, dst, meta


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, np.ndarray):
        return json_safe(value.tolist())
    if isinstance(value, (np.floating, float)):
        v = float(value)
        return v if math.isfinite(v) else None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def resolve_forecast(dataset_path: Path, explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise FileNotFoundError(f"Forecast CSV not found: {p}")
        return p
    sidecars = [dataset_path.with_suffix(dataset_path.suffix + ".meta.json"), dataset_path.with_suffix(".meta.json")]
    for s in sidecars:
        if not s.exists():
            continue
        try:
            raw = json.loads(s.read_text(encoding="utf-8")).get("forecast_input")
            if raw:
                for p in (Path(raw), s.parent / Path(raw).name):
                    if p.exists():
                        return p
        except Exception:
            pass
    for p in (
        dataset_path.parent / "openmeteo_24h.csv",
        ROOT / "climate_dataset/openmeteo_24h.csv",
        dataset_path.parent / "example_offline_input_spatial.csv",
    ):
        if p.exists():
            return p
    return None


def direct_fmu_rows(df: pd.DataFrame, selected_elevation: float, reference: str,
                    forecast_path: Path, fmu_path: Path, barnes_filter_km: float, terrain_path: Path) -> pd.DataFrame:
    from climate_io import build_fmi_inputs, read_forecast, sources_at_time
    from fmu_client import MicroMetFMU
    from terrain_truth import TerrainTruth

    horizontal_cols = [
        c for c in ["grid_cell_id", "grid_ix", "grid_iy", "latitude", "longitude",
                    "ground_elevation_m", "resolution_m"]
        if c in df.columns
    ]
    horizontal = df[horizontal_cols].drop_duplicates(subset=["latitude", "longitude"]).reset_index(drop=True)
    times = sorted(df["time"].astype(str).unique())
    forecast = read_forecast(forecast_path)
    rows: list[dict[str, Any]] = []
    terrain_cache = {}
    with MicroMetFMU(fmu_path) as fmu, TerrainTruth(terrain_path) as terrain:
        for ti, time_text in enumerate(times):
            requested = datetime.fromisoformat(time_text)
            sources = sources_at_time(forecast, requested)
            for cell in horizontal.to_dict("records"):
                key=(float(cell["longitude"]),float(cell["latitude"]))
                ts=terrain_cache.get(key)
                if ts is None:
                    try:
                        ts=terrain.sample(key[0],key[1])
                    except ValueError:
                        continue
                    terrain_cache[key]=ts
                ground = float(ts.ground_elevation_m)
                msl = ground + selected_elevation if reference == "AGL" else selected_elevation
                agl = selected_elevation if reference == "AGL" else selected_elevation - ground
                base = {
                    **cell,
                    "time": time_text,
                    "time_index": ti,
                    "ground_elevation_m": ground,
                    "slope_deg": ts.slope_deg,
                    "aspect_deg": ts.aspect_deg,
                    "curvature_scaled": ts.curvature_scaled,
                    "terrain_source": ts.source_path,
                    "altitude_msl_m": msl,
                    "altitude_agl_m": agl,
                    "vertical_reference": reference,
                    "vertical_valid": agl >= -1e-9,
                    "vertical_sampling_mode": "direct_fmu_evaluation" if agl >= -1e-9 else "below_ground_masked",
                }
                if agl < -1e-9:
                    rows.append(base)
                    continue
                slope = ts.slope_deg
                aspect = ts.aspect_deg
                curvature = ts.curvature_scaled
                inputs = build_fmi_inputs(
                    sources,
                    latitude=float(cell["latitude"]),
                    longitude=float(cell["longitude"]),
                    elevation_m=msl,
                    requested_time=requested,
                    slope_deg=slope,
                    aspect_deg=aspect,
                    curvature=curvature,
                    barnes_filter_km=barnes_filter_km,
                )
                inputs.update(ts.fmi_overrides())
                rows.append({**base, **fmu.step(inputs)})
    return pd.DataFrame(rows)


def build_payload(selected: pd.DataFrame, numeric: list[str], times: list[str],
                  lon_dem: np.ndarray, lat_dem: np.ndarray, z_dem: np.ndarray,
                  selected_elevation: float, reference: str, vertical_label: str,
                  dem_meta: dict[str, Any], sampling_mode: str, args: argparse.Namespace) -> dict[str, Any]:
    by_time: dict[str, dict[str, Any]] = {}
    for time_text in times:
        frame = selected[selected["time"].astype(str) == time_text].copy()
        # Keep stable ordering to align all per-parameter arrays.
        frame = frame.sort_values(["latitude", "longitude"], kind="mergesort")
        entry: dict[str, Any] = {
            "longitude": frame["longitude"].astype(float).tolist(),
            "latitude": frame["latitude"].astype(float).tolist(),
            "ground_elevation_m": frame["ground_elevation_m"].astype(float).tolist(),
            "altitude_msl_m": frame["altitude_msl_m"].astype(float).tolist(),
            "altitude_agl_m": frame["altitude_agl_m"].astype(float).tolist(),
            "vertical_valid": [bool(v) for v in frame.get("vertical_valid", pd.Series([True] * len(frame))).tolist()],
            "values": {},
        }
        for parameter in numeric:
            vals = []
            for v in frame[parameter].tolist():
                if pd.isna(v):
                    vals.append(None)
                else:
                    vals.append(float(v))
            entry["values"][parameter] = vals
        by_time[time_text] = entry

    payload = {
        "times": times,
        "parameters": numeric,
        "default_parameter": args.default_parameter if args.default_parameter in numeric else numeric[0],
        "labels": {p: LABELS.get(p, p.replace("_", " ").title()) for p in numeric},
        "vertical_reference": reference,
        "selected_elevation_m": selected_elevation,
        "vertical_label": vertical_label,
        "sampling_mode": sampling_mode,
        "dem": {
            **dem_meta,
            "lon": np.round(lon_dem, 6).tolist(),
            "lat": np.round(lat_dem, 6).tolist(),
            "z": np.round(z_dem, 3).tolist(),
        },
        "data": by_time,
        "idw_power": float(args.idw_power),
        "terrain_opacity": float(args.terrain_opacity),
        "climate_opacity": float(args.climate_opacity),
        "vertical_exaggeration": float(args.vertical_exaggeration),
        "z_padding_m": float(args.z_padding_m),
        "z_axis_reference": "local_dem_minimum",
        "source_dataset": str(args.input),
        "forecast_input": args.forecast_input,
        "fmu": str(args.fmu),
        "manifest_note": "3D report uses browser-side IDW interpolation over the DEM grid; HTML is kept lightweight and loads its payload from a sidecar JavaScript file.",
    }
    return payload


def write_html(out_path: Path, data_js_name: str, selected_elevation: float, reference: str) -> None:
    template = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MicroMetTwin 3D DEM report — __ALT__ m __REF__</title>
<script src="plotly-2.35.2.min.js"></script><script src="__DATA_JS__"></script>
<style>
body{font-family:system-ui,sans-serif;margin:0;background:#f5f7fa;color:#17202a;overflow:hidden}
#controls{position:fixed;left:18px;top:16px;z-index:1000;background:rgba(255,255,255,.96);padding:12px 14px;border-radius:10px;box-shadow:0 3px 16px #0002;min-width:340px;max-width:min(480px,calc(100vw - 36px))}
#controls label{font-weight:650;font-size:13px;display:block;margin:4px 0}#controls select,#controls input{width:100%}
#meta{font-size:12px;color:#52616b;line-height:1.4;margin-top:8px}#plot{width:100vw;height:100vh}.small{font-size:11px;color:#6b7a88}
</style></head><body>
<div id="controls"><label>Climate parameter</label><select id="parameter"></select>
<label>Forecast time: <span id="timeLabel"></span></label><input id="time" type="range" min="0" step="1" value="0">
<label style="display:flex;align-items:center;gap:8px;margin-top:10px;font-weight:600"><input id="terrainVisible" type="checkbox" checked style="width:auto"> Show DEM terrain</label>
<div id="meta"></div><div class="small">The vertical display uses a local datum for readability; hover information preserves true MSL elevations.</div></div>
<div id="plot"></div><script>
const MICROMET=window.MICROMET_3D_REPORT_DATA;
if(!MICROMET){document.body.innerHTML='<pre style="padding:20px">Payload data was not loaded. Keep the HTML, data.js and Plotly JavaScript files together.</pre>';throw new Error('Missing report payload');}
const parameterSelect=document.getElementById('parameter'),timeSlider=document.getElementById('time'),timeLabel=document.getElementById('timeLabel'),terrainVisible=document.getElementById('terrainVisible'),meta=document.getElementById('meta'),plotDiv=document.getElementById('plot');
MICROMET.parameters.forEach(p=>{const o=document.createElement('option');o.value=p;o.textContent=MICROMET.labels[p]+' ['+p+']';parameterSelect.appendChild(o);});parameterSelect.value=MICROMET.default_parameter;timeSlider.max=Math.max(0,MICROMET.times.length-1);
function finiteRange2D(a){let lo=Infinity,hi=-Infinity;for(const row of a)for(const v of row)if(Number.isFinite(v)){if(v<lo)lo=v;if(v>hi)hi=v;}return[lo,hi];}
function rowMean2D(a){let s=0,c=0;for(const row of a)for(const v of row)if(Number.isFinite(v)){s+=v;c++;}return c?s/c:0;}
function map2D(a,fn){return a.map((row,i)=>row.map((v,j)=>fn(v,i,j)));}
function idwGrid(px,py,pv,tx,ty,power){const latMean=rowMean2D(ty)*Math.PI/180,cosLat=Math.cos(latMean),out=[];for(let i=0;i<tx.length;i++){const row=[];for(let j=0;j<tx[i].length;j++){const x=tx[i][j],y=ty[i][j];if(!Number.isFinite(x)||!Number.isFinite(y)){row.push(null);continue;}let num=0,den=0,exact=null;for(let k=0;k<pv.length;k++){const v=pv[k];if(v===null||!Number.isFinite(v))continue;const dx=(x-px[k])*cosLat*111320,dy=(y-py[k])*110540,d2=dx*dx+dy*dy;if(d2<1e-8){exact=v;break;}const w=1/Math.pow(Math.max(d2,1e-8),power/2);num+=w*v;den+=w;}row.push(exact!==null?exact:(den>0?num/den:null));}out.push(row);}return out;}
const terrainX=MICROMET.dem.lon,terrainY=MICROMET.dem.lat,terrainMsl=MICROMET.dem.z,terrainRange=finiteRange2D(terrainMsl),terrainBaseMsl=terrainRange[0];
const terrainDisplayZ=map2D(terrainMsl,v=>Number.isFinite(v)?v-terrainBaseMsl:null);
const climateMsl=map2D(terrainMsl,v=>{if(!Number.isFinite(v))return null;if(MICROMET.vertical_reference==='AGL')return v+MICROMET.selected_elevation_m;return MICROMET.selected_elevation_m>=v?MICROMET.selected_elevation_m:null;});
const climateDisplayZ=map2D(climateMsl,v=>Number.isFinite(v)?v-terrainBaseMsl:null),displayRange=finiteRange2D(climateDisplayZ),lonRange=finiteRange2D(terrainX),latRange=finiteRange2D(terrainY);
const meanLat=rowMean2D(terrainY)*Math.PI/180,widthM=Math.max(1,(lonRange[1]-lonRange[0])*111320*Math.cos(meanLat)),heightM=Math.max(1,(latRange[1]-latRange[0])*110540),horizontalMax=Math.max(widthM,heightM),verticalSpan=Math.max(1,displayRange[1]-Math.min(0,displayRange[0])),aspectZ=Math.max(.22,Math.min(1.35,verticalSpan/horizontalMax*MICROMET.vertical_exaggeration*8));
function valuesRange(rows,p){let min=Infinity,max=-Infinity;for(const v of(rows.values[p]||[]))if(v!==null&&Number.isFinite(v)){if(v<min)min=v;if(v>max)max=v;}if(!Number.isFinite(min)||!Number.isFinite(max))return[null,null];if(min===max){min-=.5;max+=.5;}return[min,max];}
function render(){const time=MICROMET.times[Number(timeSlider.value)||0],parameter=parameterSelect.value||MICROMET.default_parameter,rows=MICROMET.data[time];timeLabel.textContent=time;const idx=[];for(let i=0;i<rows.longitude.length;i++)if(rows.vertical_valid[i])idx.push(i);const px=[],py=[],pv=[],pz=[],pmsl=[];for(const i of idx){px.push(rows.longitude[i]);py.push(rows.latitude[i]);pv.push(rows.values[parameter][i]);pmsl.push(rows.altitude_msl_m[i]);pz.push(rows.altitude_msl_m[i]-terrainBaseMsl);}const colorGrid=idwGrid(px,py,pv,terrainX,terrainY,MICROMET.idw_power),range=valuesRange(rows,parameter);
const terrain={type:'surface',visible:terrainVisible.checked,x:terrainX,y:terrainY,z:terrainDisplayZ,surfacecolor:terrainMsl,colorscale:'Earth',showscale:false,opacity:MICROMET.terrain_opacity,name:'DEM terrain',customdata:terrainMsl,contours:{z:{show:true,usecolormap:false,color:'rgba(50,50,50,.45)',width:1,project:{z:true}}},lighting:{ambient:.45,diffuse:.85,specular:.15,roughness:.82,fresnel:.08},lightposition:{x:1000,y:-1500,z:2500},hovertemplate:'lon=%{x:.6f}<br>lat=%{y:.6f}<br>terrain MSL=%{customdata:.1f} m<br>height above local datum=%{z:.1f} m<extra>DEM</extra>'};
const climate={type:'surface',x:terrainX,y:terrainY,z:climateDisplayZ,surfacecolor:colorGrid,colorscale:'Turbo',opacity:MICROMET.climate_opacity,cmin:range[0],cmax:range[1],colorbar:{title:MICROMET.labels[parameter]},name:'Climate sheet',customdata:climateMsl,lighting:{ambient:.75,diffuse:.55,specular:.05,roughness:.95},hovertemplate:'lon=%{x:.6f}<br>lat=%{y:.6f}<br>altitude MSL=%{customdata:.1f} m<br>height above local datum=%{z:.1f} m<br>'+MICROMET.labels[parameter]+'=%{surfacecolor:.4g}<extra>Climate</extra>'};
const markers={type:'scatter3d',mode:'markers',x:px,y:py,z:pz,customdata:pmsl,marker:{size:2.6,color:'rgba(0,0,0,.55)'},name:'Evaluation cells',hovertemplate:'lon=%{x:.6f}<br>lat=%{y:.6f}<br>altitude MSL=%{customdata:.1f} m<extra>Grid cell</extra>'};
const zUpper=Math.max(displayRange[1],terrainRange[1]-terrainBaseMsl)+MICROMET.z_padding_m,layout={title:{text:'MicroMetTwin 3D climate over DEM — '+MICROMET.vertical_label+' — '+MICROMET.labels[parameter]+' — '+time},scene:{xaxis:{title:'Longitude',range:lonRange,showbackground:true,backgroundcolor:'rgb(235,240,245)',gridcolor:'white'},yaxis:{title:'Latitude',range:latRange,showbackground:true,backgroundcolor:'rgb(235,240,245)',gridcolor:'white'},zaxis:{title:'Height above DEM minimum (m)',range:[-MICROMET.z_padding_m,zUpper],showbackground:true,backgroundcolor:'rgb(245,247,250)',gridcolor:'white'},aspectmode:'manual',aspectratio:{x:widthM/horizontalMax,y:heightM/horizontalMax,z:aspectZ},camera:{eye:{x:1.42,y:-1.52,z:1.08}},dragmode:'orbit'},margin:{l:0,r:0,t:80,b:0},template:'plotly_white',showlegend:true,legend:{orientation:'h',y:1.02,x:.02},uirevision:'micromet-3d-fixed'};
Plotly.react(plotDiv,[terrain,climate,markers],layout,{responsive:true,displaylogo:false});meta.innerHTML='Vertical mode: <b>'+MICROMET.vertical_reference+'</b> · Requested altitude: <b>'+MICROMET.selected_elevation_m+' m</b> · DEM MSL: <b>'+terrainRange[0].toFixed(1)+'–'+terrainRange[1].toFixed(1)+' m</b> · longitude samples: <b>'+MICROMET.dem.unique_longitudes+'</b> · latitude samples: <b>'+MICROMET.dem.unique_latitudes+'</b> · DEM grid: <b>'+MICROMET.dem.downsampled_shape[0]+'×'+MICROMET.dem.downsampled_shape[1]+'</b>';}
parameterSelect.addEventListener('change',render);timeSlider.addEventListener('input',render);terrainVisible.addEventListener('change',()=>{if(plotDiv.data&&plotDiv.data.length)Plotly.restyle(plotDiv,{visible:terrainVisible.checked},[0]);else render();});render();
</script></body></html>"""
    html = (template.replace("__ALT__", f"{selected_elevation:g}")
                    .replace("__REF__", reference)
                    .replace("__DATA_JS__", data_js_name))
    out_path.write_text(html, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a Plotly 3D climate sheet above a DEM terrain surface.")
    ap.add_argument("--input", default="climate_dataset/micromet_grid_24h.csv")
    ap.add_argument("--dem", default=None, help="DEM GeoTIFF or directory containing terrain_obstacles.tif")
    ap.add_argument("--dem-dir", default="outputs/region_v1_map",
                    help="Default DEM directory from boulder_multilayer_map_builder")
    ap.add_argument("--output-dir", default="reports/parameters_3d")
    ap.add_argument("--forecast-input", default=None)
    ap.add_argument("--fmu", default="fmu/MicroMetTwin.fmu")
    vertical = ap.add_mutually_exclusive_group(required=True)
    vertical.add_argument("--elevation-ground-level", type=parse_elevation,
                          help="Climate sheet altitude above local terrain (AGL), e.g. 100m")
    vertical.add_argument("--elevation-sea-level", type=parse_elevation,
                          help="Absolute climate sheet altitude MSL, e.g. 1800m")
    ap.add_argument("--parameters", default="all")
    ap.add_argument("--default-parameter", default="temperature_local")
    ap.add_argument("--dem-max-size", type=int, default=120,
                    help="Maximum number of DEM samples along either dimension")
    ap.add_argument("--idw-power", type=float, default=2.0)
    ap.add_argument("--barnes-filter-km", type=float, default=20.0)
    ap.add_argument("--terrain-opacity", type=float, default=1.0)
    ap.add_argument("--climate-opacity", type=float, default=0.62)
    ap.add_argument("--vertical-exaggeration", type=float, default=2.0,
                    help="Visual exaggeration of local terrain relief; does not alter MSL values")
    ap.add_argument("--z-padding-m", type=float, default=20.0,
                    help="Padding around the local vertical display range")
    args = ap.parse_args()

    dataset_path = Path(args.input)
    if not dataset_path.exists():
        raise FileNotFoundError(dataset_path)
    dem_path = find_dem(Path(args.dem) if args.dem else Path(args.dem_dir))
    lon_dem, lat_dem, z_dem, dem_meta = read_dem(dem_path, args.dem_max_size)
    if np.count_nonzero(np.isfinite(z_dem)) == 0:
        raise ValueError("DEM contains no finite elevation values")

    df = pd.read_csv(dataset_path)
    required = {"longitude", "latitude", "ground_elevation_m", "time"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Dataset is missing required columns: {sorted(missing)}")

    if args.elevation_ground_level is not None:
        selected_elevation = args.elevation_ground_level
        reference = "AGL"
        vertical_label = f"{selected_elevation:g} m above local terrain (AGL)"
    else:
        selected_elevation = args.elevation_sea_level
        reference = "MSL"
        vertical_label = f"{selected_elevation:g} m above mean sea level (MSL)"

    forecast_path = resolve_forecast(dataset_path, args.forecast_input)
    fmu_path = Path(args.fmu)
    if forecast_path is not None and fmu_path.exists():
        selected = direct_fmu_rows(df, selected_elevation, reference, forecast_path, fmu_path,
                                   args.barnes_filter_km, dem_path)
        sampling_mode = "direct_fmu_evaluation"
    else:
        col = "altitude_agl_m" if reference == "AGL" else "altitude_msl_m"
        if col not in df.columns:
            raise ValueError(f"{col} is unavailable and direct FMU evaluation could not be started")
        selected = df[np.isclose(df[col].astype(float), selected_elevation, atol=1e-6)].copy()
        if selected.empty:
            raise ValueError(
                f"No rows at {selected_elevation:g} m {reference}; provide --forecast-input and --fmu "
                "for direct FMU evaluation at an arbitrary altitude."
            )
        if "vertical_valid" not in selected.columns:
            selected["vertical_valid"] = True
        if "vertical_sampling_mode" not in selected.columns:
            selected["vertical_sampling_mode"] = "dataset_exact_level"
        sampling_mode = "dataset_exact_level"

    numeric = [c for c in selected.select_dtypes(include="number").columns
               if c not in META_COLUMNS and selected[c].notna().any()]
    if args.parameters != "all":
        requested = [x.strip() for x in args.parameters.split(",") if x.strip()]
        missing = [x for x in requested if x not in numeric]
        if missing:
            raise ValueError(f"Requested non-numeric or missing parameters: {missing}")
        numeric = requested
    if not numeric:
        raise ValueError("No numeric climate parameters available")
    times = sorted(selected["time"].astype(str).unique())

    payload = build_payload(selected, numeric, times, lon_dem, lat_dem, z_dem,
                            selected_elevation, reference, vertical_label, dem_meta,
                            sampling_mode, args)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base_name = f"micromet_3d_dem_{reference.lower()}_{slug_number(selected_elevation)}m"
    out_path = out_dir / f"{base_name}.html"
    data_path = out_dir / f"{base_name}_data.js"
    manifest_path = out_dir / f"{base_name}_manifest.json"

    data_js = "window.MICROMET_3D_REPORT_DATA=" + json.dumps(json_safe(payload), ensure_ascii=False, separators=(",", ":"), allow_nan=False) + ";\n"
    data_path.write_text(data_js, encoding="utf-8")
    # Keep Plotly local so the report also works without internet access.
    plotly_target = out_dir / "plotly-2.35.2.min.js"
    if not plotly_target.exists():
        try:
            import plotly
            source = Path(plotly.__file__).resolve().parent / "package_data" / "plotly.min.js"
            if source.exists():
                plotly_target.write_bytes(source.read_bytes())
        except Exception:
            pass
    write_html(out_path, data_path.name, selected_elevation, reference)

    manifest = {
        "report": str(out_path),
        "report_data": str(data_path),
        "dataset": str(dataset_path),
        "dem": dem_meta,
        "vertical_reference": reference,
        "selected_elevation_m": selected_elevation,
        "sampling_mode": sampling_mode,
        "forecast_input": str(forecast_path) if forecast_path else None,
        "fmu": str(fmu_path) if fmu_path.exists() else None,
        "times": len(times),
        "parameters": numeric,
        "dem_shape": list(z_dem.shape),
        "html_size_bytes": out_path.stat().st_size,
        "data_js_size_bytes": data_path.stat().st_size,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
