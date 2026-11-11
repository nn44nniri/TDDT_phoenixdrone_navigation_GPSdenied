#!/usr/bin/env python3
"""Generate one interactive 2D MicroMet map report at one selected elevation.

The output is a single HTML page. It contains:
- a parameter selector;
- an hourly forecast-time slider;
- a 2D longitude/latitude map;
- an IDW-interpolated colour surface clipped to the polygon in region/region_v1.geojson;
- the original FMU grid-cell centres and the complete region boundary.

The historical filename is retained to avoid breaking existing package workflows.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from climate_errors import ALTITUDE_BELOW_GROUND, error_dict
from region_geojson import geojson_vertices, load_region_geojson

META_COLUMNS = {
    "grid_cell_id", "grid_ix", "grid_iy", "grid_iz", "latitude", "longitude",
    "elevation_m", "altitude_msl_m", "altitude_agl_m", "ground_elevation_m",
    "time", "time_index", "resolution_m",
}

LABELS = {
    "temperature_local": "Local air temperature (°C)",
    "relative_humidity_local": "Local relative humidity (%)",
    "wind_speed_terrain": "Terrain-adjusted wind speed (m/s)",
    "wind_direction_terrain_deg": "Terrain-adjusted wind direction (deg)",
    "surface_pressure_pa": "Surface pressure (Pa)",
    "shortwave_radiation": "Incoming shortwave radiation (W/m²)",
    "longwave_radiation": "Incoming longwave radiation (W/m²)",
    "precipitation_local": "Local precipitation (mm/h)",
    "cloud_fraction": "Cloud fraction",
    "dewpoint_local": "Local dew-point temperature (°C)",
}


def parse_elevation(value: str) -> float:
    """Parse values such as 100, 100m, 100 m, or 0.1km into metres."""
    text = value.strip().lower().replace(" ", "")
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))(m|km)?", text)
    if not match:
        raise argparse.ArgumentTypeError(
            f"Invalid elevation {value!r}; use a value such as 100m, 100, or 0.1km"
        )
    number = float(match.group(1))
    unit = match.group(2) or "m"
    return number * (1000.0 if unit == "km" else 1.0)


def slug_number(value: float) -> str:
    return (f"{value:g}").replace("-", "minus_").replace(".", "p")


def load_vertices(path: Path) -> List[Dict[str, float]]:
    return geojson_vertices(load_region_geojson(path))


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate one interactive 2D HTML climate report at one elevation."
    )
    ap.add_argument("--input", default="climate_dataset/micromet_grid_24h.csv",
                    help="Grid dataset used for horizontal cell locations and terrain metadata")
    ap.add_argument("--forecast-input", default=None,
                    help=("Offline forecast CSV used as the FMU forcing source. When omitted, "
                          "the script first reads the dataset provenance sidecar, then tries "
                          "climate_dataset/openmeteo_24h.csv, and finally the packaged example."))
    ap.add_argument("--fmu", default="fmu/MicroMetTwin.fmu",
                    help="MicroMetTwin FMU to evaluate at the requested altitude")
    ap.add_argument("--barnes-filter-km", type=float, default=20.0)
    ap.add_argument("--dem", default=None, help="Shared terrain_obstacles.tif")
    ap.add_argument("--dem-dir", default=None, help="Directory containing shared terrain_obstacles.tif")
    ap.add_argument("--terrain-policy", choices=("prefer-dem", "require-dem"), default="require-dem")
    ap.add_argument("--output-dir", default="reports/parameters_3d")
    vertical = ap.add_mutually_exclusive_group(required=True)
    vertical.add_argument("--elevation-sea-level", type=parse_elevation,
                          help="Absolute altitude above mean sea level (MSL), e.g. 100m")
    vertical.add_argument("--elevation-ground-level", type=parse_elevation,
                          help="Altitude above local ground (AGL), e.g. 100m")
    ap.add_argument("--parameters", default="all", help="Comma-separated columns or 'all'")
    ap.add_argument("--region", default="region/region_v1.geojson",
                    help="RFC 7946 GeoJSON file containing the WGS 84 region polygon")
    ap.add_argument("--raster-size", type=int, default=90,
                    help="Number of interpolation samples along each map axis")
    ap.add_argument("--idw-power", type=float, default=2.0,
                    help="Inverse-distance-weighting power")
    args = ap.parse_args()

    if args.raster_size < 20:
        raise ValueError("--raster-size must be at least 20")
    if args.idw_power <= 0:
        raise ValueError("--idw-power must be positive")

    df = pd.read_csv(args.input)
    required = {"longitude", "latitude", "altitude_msl_m", "altitude_agl_m", "ground_elevation_m", "time"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")

    if args.elevation_sea_level is not None:
        filter_column = "altitude_msl_m"
        selected_elevation = args.elevation_sea_level
        vertical_reference = "MSL"
        vertical_label = "above mean sea level"
    else:
        filter_column = "altitude_agl_m"
        selected_elevation = args.elevation_ground_level
        vertical_reference = "AGL"
        vertical_label = "above local ground"
    # The report is evaluated directly by the FMU at the requested altitude.
    # The input grid dataset supplies horizontal cell locations and terrain
    # metadata only; it is not used as a vertical lookup table. This means a
    # request such as 200 m AGL remains valid even when the packaged dataset
    # contains only a 100 m AGL layer.
    from datetime import datetime
    from climate_io import build_fmi_inputs, read_forecast, sources_at_time
    from fmu_client import MicroMetFMU
    from terrain_truth import TerrainTruth, find_terrain_tif

    def resolve_forecast_input() -> tuple[Path, str]:
        if args.forecast_input:
            candidate = Path(args.forecast_input)
            if not candidate.exists():
                raise FileNotFoundError(
                    f"Offline forecast input not found: {candidate}. "
                    "Check --forecast-input or download the forecast again."
                )
            return candidate, "explicit_cli"

        dataset_path = Path(args.input)
        sidecars = [
            dataset_path.with_suffix(dataset_path.suffix + ".meta.json"),
            dataset_path.with_suffix(".meta.json"),
        ]
        for sidecar in sidecars:
            if not sidecar.exists():
                continue
            try:
                metadata = json.loads(sidecar.read_text(encoding="utf-8"))
                raw = metadata.get("forecast_input")
                if raw:
                    candidate = Path(raw)
                    if not candidate.is_absolute():
                        # Paths written by run_fmu_dataset.py are normally relative
                        # to the package working directory. Also try relative to the
                        # sidecar itself for moved datasets.
                        alternatives = [candidate, sidecar.parent / candidate.name]
                    else:
                        alternatives = [candidate]
                    for item in alternatives:
                        if item.exists():
                            return item, f"dataset_provenance:{sidecar}"
            except (OSError, ValueError, TypeError):
                pass

        candidates = [
            Path("climate_dataset/openmeteo_24h.csv"),
            dataset_path.parent / "openmeteo_24h.csv",
            Path("climate_dataset/example_offline_input_spatial.csv"),
            dataset_path.parent / "example_offline_input_spatial.csv",
        ]
        seen = set()
        for candidate in candidates:
            key = str(candidate.resolve()) if candidate.exists() else str(candidate)
            if key in seen:
                continue
            seen.add(key)
            if candidate.exists():
                return candidate, "automatic_fallback"

        searched = [str(x) for x in candidates]
        raise FileNotFoundError(
            "No offline forecast CSV could be resolved. "
            "Use --forecast-input, regenerate the dataset to create its provenance sidecar, "
            f"or place openmeteo_24h.csv beside the dataset. Searched: {searched}"
        )

    forecast_path, forecast_resolution = resolve_forecast_input()
    forecast_rows = read_forecast(forecast_path)

    horizontal_columns = [
        c for c in ["grid_cell_id", "grid_ix", "grid_iy", "latitude", "longitude",
                    "ground_elevation_m", "resolution_m"] if c in df.columns
    ]
    horizontal = df[horizontal_columns].drop_duplicates(
        subset=[c for c in ["grid_cell_id", "latitude", "longitude"] if c in horizontal_columns]
    ).reset_index(drop=True)
    times = sorted(df["time"].astype(str).unique())

    generated_records = []
    terrain_path = find_terrain_tif(args.dem, dem_dir=args.dem_dir)
    if terrain_path is None and args.terrain_policy == "require-dem":
        raise FileNotFoundError("Shared terrain truth is required; pass --dem or --dem-dir")
    terrain_reader = TerrainTruth(terrain_path) if terrain_path else None
    terrain_cache = {}
    try:
      with MicroMetFMU(args.fmu) as fmu:
        for time_index, time_text in enumerate(times):
            requested = datetime.fromisoformat(time_text)
            sources = sources_at_time(forecast_rows, requested)
            for cell in horizontal.to_dict("records"):
                key=(float(cell["longitude"]),float(cell["latitude"]))
                ts=terrain_cache.get(key)
                if ts is None and terrain_reader is not None:
                    try:
                        ts=terrain_reader.sample(key[0],key[1])
                    except ValueError:
                        continue
                    terrain_cache[key]=ts
                ground = float(ts.ground_elevation_m) if ts is not None else float(cell["ground_elevation_m"])
                target_msl = selected_elevation if vertical_reference == "MSL" else ground + selected_elevation
                target_agl = selected_elevation if vertical_reference == "AGL" else selected_elevation - ground
                base = {
                    **cell,
                    "time": time_text,
                    "time_index": time_index,
                    "ground_elevation_m": ground,
                    "terrain_source": ts.source_path if ts is not None else "dataset/legacy",
                    "altitude_msl_m": target_msl,
                    "altitude_agl_m": target_agl,
                    "elevation_m": target_msl,
                    "vertical_reference": vertical_reference,
                    "vertical_valid": target_agl >= -1e-9,
                    "vertical_sampling_mode": "direct_fmu_evaluation",
                }
                if target_agl < -1e-9:
                    base["vertical_sampling_mode"] = "below_ground_masked"
                    generated_records.append(base)
                    continue

                source_match = df[
                    (df["latitude"].sub(float(cell["latitude"])).abs() < 1e-9)
                    & (df["longitude"].sub(float(cell["longitude"])).abs() < 1e-9)
                ]
                slope_deg = float(ts.slope_deg) if ts is not None else (float(source_match["slope_deg"].iloc[0]) if "slope_deg" in source_match and len(source_match) else 0.0)
                aspect_deg = float(ts.aspect_deg) if ts is not None else (float(source_match["aspect_deg"].iloc[0]) if "aspect_deg" in source_match and len(source_match) else 0.0)
                curvature = float(ts.curvature_scaled) if ts is not None else (float(source_match["curvature_scaled"].iloc[0]) if "curvature_scaled" in source_match and len(source_match) else 0.0)
                base.update({"slope_deg":slope_deg,"aspect_deg":aspect_deg,"curvature_scaled":curvature})
                inputs = build_fmi_inputs(
                    sources,
                    latitude=float(cell["latitude"]),
                    longitude=float(cell["longitude"]),
                    elevation_m=target_msl,
                    requested_time=requested,
                    slope_deg=slope_deg,
                    aspect_deg=aspect_deg,
                    curvature=curvature,
                    barnes_filter_km=args.barnes_filter_km,
                )
                if ts is not None:
                    inputs.update(ts.fmi_overrides())
                outputs = fmu.step(inputs)
                generated_records.append({**base, **outputs})
    finally:
      if terrain_reader is not None:
        terrain_reader.close()

    selected = pd.DataFrame(generated_records)
    candidate_numeric = [
        c for c in selected.select_dtypes(include="number").columns
        if c not in META_COLUMNS and selected[c].notna().any()
    ]
    available_elevations = sorted(float(v) for v in df[filter_column].dropna().unique())

    # A constant MSL plane can lie below terrain everywhere. Return a stable,
    # machine-readable dictionary instead of a Python traceback.
    if vertical_reference == "MSL" and not bool(selected["vertical_valid"].fillna(False).any()):
        ground_min = float(selected["ground_elevation_m"].min())
        ground_max = float(selected["ground_elevation_m"].max())
        result = error_dict(
            ALTITUDE_BELOW_GROUND,
            "Requested MSL altitude is below local terrain for every grid cell in the region.",
            requested_altitude_msl_m=float(selected_elevation),
            ground_elevation_min_msl_m=ground_min,
            ground_elevation_max_msl_m=ground_max,
            affected_grid_cells=int(selected[["longitude", "latitude"]].drop_duplicates().shape[0]),
            affected_rows=int(len(selected)),
            vertical_reference="MSL",
            input_dataset=str(args.input),
            forecast_input=str(forecast_path),
            region_file=str(args.region),
            suggested_action="Use --elevation-ground-level or request an MSL altitude above local terrain.",
        )
        outdir = Path(args.output_dir)
        outdir.mkdir(parents=True, exist_ok=True)
        error_path = outdir / f"error_msl_{slug_number(selected_elevation)}m.json"
        error_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        result["error"]["details"]["error_file"] = str(error_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(2)

    numeric = list(candidate_numeric)
    if args.parameters != "all":
        wanted = [x.strip() for x in args.parameters.split(",") if x.strip()]
        unknown = sorted(set(wanted) - set(selected.columns))
        if unknown:
            raise ValueError(f"Unknown parameters: {unknown}")
        nonnumeric = [c for c in wanted if not pd.api.types.is_numeric_dtype(selected[c])]
        if nonnumeric:
            raise ValueError(f"Parameters must be numeric: {nonnumeric}")
        numeric = wanted
    if not numeric:
        raise ValueError("No numeric climate parameters selected")

    vertices = load_vertices(Path(args.region))
    times = sorted(selected["time"].astype(str).unique())

    # Keep the payload compact: one record per source cell and hour, with all selected parameters.
    records = []
    cols = ["longitude", "latitude", "ground_elevation_m", "altitude_msl_m", "altitude_agl_m", "time", "vertical_valid", "vertical_sampling_mode"] + numeric
    for row in selected[cols].itertuples(index=False, name=None):
        record = {cols[i]: (None if pd.isna(row[i]) else row[i]) for i in range(len(cols))}
        records.append(record)

    labels = {p: LABELS.get(p, p.replace("_", " ").title()) for p in numeric}
    payload = {
        "selected_elevation_m": selected_elevation,
        "vertical_reference": vertical_reference,
        "vertical_label": vertical_label,
        "parameters": numeric,
        "labels": labels,
        "times": times,
        "vertices": vertices,
        "records": records,
        "raster_size": args.raster_size,
        "idw_power": args.idw_power,
        "source": str(args.input),
        "forecast_input": str(forecast_path),
        "forecast_input_resolution": forecast_resolution,
        "fmu": str(args.fmu),
        "evaluation_mode": "direct_fmu_evaluation_at_requested_altitude",
        "available_source_levels_m": available_elevations,
        "valid_row_count": int(selected.get("vertical_valid", pd.Series(dtype=bool)).fillna(False).sum()),
        "masked_below_ground_count": int((selected.get("vertical_sampling_mode", pd.Series(dtype=str)) == "below_ground_masked").sum()),
    }

    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    output = outdir / f"micromet_2d_{vertical_reference.lower()}_{slug_number(selected_elevation)}m.html"

    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MicroMetTwin 2D report — {selected_elevation:g} m {vertical_reference}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
body{{font-family:system-ui,sans-serif;margin:0;background:#f5f7fa;color:#17202a}}
header{{padding:18px 24px;background:white;border-bottom:1px solid #dfe6e9}}
.controls{{display:grid;grid-template-columns:minmax(280px,1fr) minmax(300px,2fr);gap:18px;padding:14px 24px;background:white}}
label{{font-weight:600;display:block;margin-bottom:5px}} select,input{{width:100%}}
#map{{height:calc(100vh - 210px);min-height:620px}}
.meta{{font-size:.9rem;color:#52616b;margin-top:6px}}
@media(max-width:800px){{.controls{{grid-template-columns:1fr}}#map{{height:70vh}}}}
</style>
</head>
<body>
<header><h1 style="margin:0">MicroMetTwin fixed-elevation 2D climate report</h1>
<div class="meta">Altitude: {selected_elevation:g} m {vertical_reference} ({vertical_label}) · Dataset: {html.escape(str(args.input))} · Region: {html.escape(str(args.region))}</div></header>
<div class="controls">
<div><label for="parameter">Climate parameter</label><select id="parameter"></select></div>
<div><label for="time">Forecast time: <span id="timeLabel"></span></label>
<input id="time" type="range" min="0" max="{max(0, len(times)-1)}" step="1" value="0"></div>
</div>
<div id="map"></div>
<script>
const DATA={payload_json};
const parameterSelect=document.getElementById('parameter');
const timeSlider=document.getElementById('time');
const timeLabel=document.getElementById('timeLabel');
for(const p of DATA.parameters){{const o=document.createElement('option');o.value=p;o.textContent=DATA.labels[p]+' ['+p+']';parameterSelect.appendChild(o);}}

function pointInPolygon(x,y,poly){{let inside=false;for(let i=0,j=poly.length-1;i<poly.length;j=i++){{const xi=poly[i].longitude,yi=poly[i].latitude,xj=poly[j].longitude,yj=poly[j].latitude;const cross=((yi>y)!=(yj>y))&&(x<(xj-xi)*(y-yi)/(yj-yi)+xi);if(cross)inside=!inside;}}return inside;}}
function linspace(a,b,n){{const out=[];const d=(b-a)/(n-1);for(let i=0;i<n;i++)out.push(a+i*d);return out;}}
function rowsAt(time){{return DATA.records.filter(r=>r.time===time);}}
function idw(x,y,rows,param){{let num=0,den=0;for(const r of rows){{if(r.vertical_valid===false)continue;const value=Number(r[param]);if(!Number.isFinite(value))continue;const dx=(x-r.longitude)*Math.cos(y*Math.PI/180),dy=y-r.latitude;const d2=dx*dx+dy*dy;if(d2<1e-16)return value;const w=1/Math.pow(Math.sqrt(d2),DATA.idw_power);num+=w*value;den+=w;}}return den>0?num/den:null;}}

const lons=DATA.vertices.map(v=>v.longitude),lats=DATA.vertices.map(v=>v.latitude);
const padX=(Math.max(...lons)-Math.min(...lons))*0.015,padY=(Math.max(...lats)-Math.min(...lats))*0.015;
const xs=linspace(Math.min(...lons),Math.max(...lons),DATA.raster_size);
const ys=linspace(Math.min(...lats),Math.max(...lats),DATA.raster_size);
const polygonClosed=[...DATA.vertices,DATA.vertices[0]];

function render(){{
 const param=parameterSelect.value||DATA.parameters[0];
 const time=DATA.times[Number(timeSlider.value)];timeLabel.textContent=time;
 const rows=rowsAt(time);
 const z=[];for(const y of ys){{const line=[];for(const x of xs)line.push(pointInPolygon(x,y,DATA.vertices)?idw(x,y,rows,param):null);z.push(line);}}
 const validRows=rows.filter(r=>r.vertical_valid!==false);
 const invalidRows=rows.filter(r=>r.vertical_valid===false);
 const values=validRows.map(r=>Number(r[param])).filter(Number.isFinite);
 const heat={{type:'heatmap',x:xs,y:ys,z:z,colorscale:'Turbo',connectgaps:false,colorbar:{{title:DATA.labels[param]}},hovertemplate:'longitude=%{{x:.6f}}<br>latitude=%{{y:.6f}}<br>'+DATA.labels[param]+'=%{{z:.6g}}<extra></extra>'}};
 const boundary={{type:'scatter',mode:'lines',x:polygonClosed.map(v=>v.longitude),y:polygonClosed.map(v=>v.latitude),line:{{color:'black',width:3}},fill:'none',name:'Region boundary',hoverinfo:'skip'}};
 const points={{type:'scatter',mode:'markers',x:validRows.map(r=>r.longitude),y:validRows.map(r=>r.latitude),marker:{{size:5,color:'rgba(0,0,0,.55)'}},name:'Valid grid cells',customdata:validRows.map(r=>[r.ground_elevation_m,r.altitude_msl_m,r.altitude_agl_m,r.vertical_sampling_mode]),hovertemplate:'Grid cell<br>longitude=%{{x:.6f}}<br>latitude=%{{y:.6f}}<br>ground MSL=%{{customdata[0]:.2f}} m<br>altitude MSL=%{{customdata[1]:.2f}} m<br>altitude AGL=%{{customdata[2]:.2f}} m<br>sampling=%{{customdata[3]}}<extra></extra>'}};
 const invalid={{type:'scatter',mode:'markers',x:invalidRows.map(r=>r.longitude),y:invalidRows.map(r=>r.latitude),marker:{{size:7,symbol:'x',color:'rgba(180,0,0,.8)'}},name:'Below-ground cells',customdata:invalidRows.map(r=>[r.ground_elevation_m,r.altitude_msl_m,r.altitude_agl_m]),hovertemplate:'Below local terrain<br>longitude=%{{x:.6f}}<br>latitude=%{{y:.6f}}<br>ground MSL=%{{customdata[0]:.2f}} m<br>requested MSL=%{{customdata[1]:.2f}} m<br>requested AGL=%{{customdata[2]:.2f}} m<extra></extra>'}};
 const layout={{title:{{text:DATA.labels[param]+' — '+time+' — altitude '+DATA.selected_elevation_m+' m '+DATA.vertical_reference}},xaxis:{{title:'Longitude',range:[Math.min(...lons)-padX,Math.max(...lons)+padX],scaleanchor:'y',scaleratio:1}},yaxis:{{title:'Latitude',range:[Math.min(...lats)-padY,Math.max(...lats)+padY]}},margin:{{l:75,r:45,t:70,b:65}},legend:{{orientation:'h'}},uirevision:'fixed-elevation-map'}};
 if(values.length===0){{layout.annotations=[{{text:'No valid FMU values are available for this parameter and time at the requested altitude.',xref:'paper',yref:'paper',x:0.5,y:0.5,showarrow:false,font:{{size:18,color:'#a00'}},bgcolor:'rgba(255,255,255,.9)'}}];}}
 Plotly.react('map',[heat,boundary,points,invalid],layout,{{responsive:true,displaylogo:false}});
}}
parameterSelect.addEventListener('change',render);timeSlider.addEventListener('input',render);render();
</script>
</body></html>"""
    output.write_text(document, encoding="utf-8")

    manifest = {
        "report": output.name,
        "selected_elevation_m": selected_elevation,
        "vertical_reference": vertical_reference,
        "vertical_label": vertical_label,
        "parameters": numeric,
        "parameter_count": len(numeric),
        "time_count": len(times),
        "grid_rows_at_elevation": int(len(selected)),
        "valid_rows": int(selected["vertical_valid"].fillna(False).sum()),
        "masked_below_ground_rows": int((selected["vertical_sampling_mode"] == "below_ground_masked").sum()),
        "unique_grid_cells": int(selected[["longitude", "latitude"]].drop_duplicates().shape[0]),
        "region_vertex_count": len(vertices),
        "raster_size": args.raster_size,
        "evaluation_mode": "direct_fmu_evaluation_at_requested_altitude",
        "fmu": str(args.fmu),
        "forecast_input": str(forecast_path),
        "forecast_input_resolution": forecast_resolution,
        "source_grid_dataset": str(args.input),
    }
    (outdir / "manifest_2d.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **manifest}, indent=2))


if __name__ == "__main__":
    main()
