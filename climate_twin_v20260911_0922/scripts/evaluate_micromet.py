#!/usr/bin/env python3
"""Evaluate MicroMetTwin against a point-forecast reference.

Reference modes:
  auto   : try Open-Meteo online once, then fall back to the packaged/input CSV
           for the complete run if the network request fails.
  online : require Open-Meteo; network errors remain fatal.
  input  : fully offline consistency check against the nearest source point in
           the input forecast CSV.

This is a forecast-to-forecast consistency evaluation, not validation against
independent observations. MicroMet may itself be forced by Open-Meteo.
"""
from __future__ import annotations
import argparse, json, math, ssl, sys, urllib.error, urllib.parse, urllib.request
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from region_geojson import load_region_geojson
from estimate_climate import estimate

MAP={
 'temperature_local':('temperature_2m','°C'),
 'relative_humidity_local':('relative_humidity_2m','%'),
 'wind_speed_terrain':('wind_speed_10m','m/s'),
 'wind_direction_terrain_deg':('wind_direction_10m','deg'),
 'surface_pressure_pa':('surface_pressure','Pa'),
 'precipitation_local':('precipitation','mm'),
 'shortwave_radiation':('shortwave_radiation','W/m²'),
}

LOCAL_MAP={
 'temperature_2m':'temperature_2m_C',
 'relative_humidity_2m':'relative_humidity_2m_pct',
 'wind_speed_10m':'wind_speed_10m_ms',
 'wind_direction_10m':'wind_direction_10m_deg',
 'surface_pressure':'surface_pressure_hPa',
 'precipitation':'precipitation_mm',
 'shortwave_radiation':'shortwave_radiation_Wm2',
}

NETWORK_ERRORS=(TimeoutError, urllib.error.URLError, ConnectionError, ssl.SSLError)

def pip(x,y,ring):
    inside=False
    for i in range(len(ring)-1):
        x0,y0=ring[i]; x1,y1=ring[i+1]
        if ((y0>y)!=(y1>y)) and x < (x1-x0)*(y-y0)/(y1-y0)+x0: inside=not inside
    return inside

def sample_points(region,n):
    ring=region['exterior_ring']; a,b,c,d=region['bbox']; side=max(2,int(math.ceil(math.sqrt(n))))
    pts=[]
    for iy in range(side):
      for ix in range(side):
        lon=a+(ix+.5)*(c-a)/side; lat=b+(iy+.5)*(d-b)/side
        if pip(lon,lat,ring): pts.append((lon,lat))
    return pts[:n]

def fetch_reference(lat,lon,start,end,timeout):
    hourly=','.join(sorted(set(v[0] for v in MAP.values())))
    q=urllib.parse.urlencode({'latitude':lat,'longitude':lon,'hourly':hourly,'wind_speed_unit':'ms','timezone':'auto','start_date':start[:10],'end_date':end[:10]})
    url='https://api.open-meteo.com/v1/forecast?'+q
    with urllib.request.urlopen(url,timeout=timeout) as r: data=json.load(r)
    h=data['hourly']; rows=[]
    for i,t in enumerate(h['time']):
      row={'time':t}
      for var,_ in MAP.values(): row[var]=h.get(var,[None]*len(h['time']))[i]
      rows.append(row)
    return pd.DataFrame(rows),url,float(data.get('elevation',float('nan')))

def input_reference(source,lat,lon):
    required={'time','latitude','longitude'}
    missing=required-set(source.columns)
    if missing:
        raise ValueError('Offline input-reference mode requires columns: '+', '.join(sorted(missing)))
    sites=source[['latitude','longitude']].drop_duplicates().copy()
    sites['d2']=(sites.latitude.astype(float)-lat)**2+(sites.longitude.astype(float)-lon)**2
    nearest=sites.loc[sites.d2.idxmin()]
    slat=float(nearest.latitude); slon=float(nearest.longitude)
    g=source[(np.isclose(source.latitude.astype(float),slat)) & (np.isclose(source.longitude.astype(float),slon))].copy()
    rows={'time':g.time.astype(str).tolist()}
    for canonical,col in LOCAL_MAP.items():
        if col in g.columns: rows[canonical]=pd.to_numeric(g[col],errors='coerce').tolist()
    ref=pd.DataFrame(rows)
    elev=float(pd.to_numeric(g.get('elevation_m',pd.Series([float('nan')])),errors='coerce').dropna().iloc[0]) if 'elevation_m' in g.columns and pd.to_numeric(g['elevation_m'],errors='coerce').notna().any() else float('nan')
    tag=f'input:{Path("forecast.csv").name}; nearest_source=({slat:.7f},{slon:.7f})'
    return ref,tag,elev,(slat,slon)

def circular_error(a,b): return abs((a-b+180)%360-180)

def metrics(g):
    p=g.prediction.to_numpy(float); r=g.reference.to_numpy(float)
    if g.parameter.iloc[0]=='wind_direction_terrain_deg': e=np.array([circular_error(x,y) for x,y in zip(p,r)]); signed=((p-r+180)%360)-180
    else: signed=p-r; e=np.abs(signed)
    rmse=float(np.sqrt(np.mean(signed*signed))); mae=float(np.mean(e)); bias=float(np.mean(signed))
    corr=float(np.corrcoef(p,r)[0,1]) if len(p)>1 and np.std(p)>0 and np.std(r)>0 else None
    return {'count':len(p),'mae':mae,'rmse':rmse,'bias':bias,'pearson_r':corr,'reference_mean':float(np.mean(r)),'prediction_mean':float(np.mean(p))}

def write_report(rows,summary,path,notice,reference_label):
    payload=json.dumps({'rows':rows.to_dict('records'),'summary':summary,'notice':notice},ensure_ascii=False,separators=(',',':'))
    html=f'''<!doctype html><html><head><meta charset="utf-8"><script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script><style>body{{font-family:system-ui;margin:0}}header,.controls{{padding:14px 22px}}#plot{{height:72vh}}select{{min-width:320px}}</style></head><body><header><h1>MicroMet forecast-consistency evaluation</h1><p><b>Reference:</b> {reference_label}</p><p>{notice}</p></header><div class="controls"><select id="p"></select></div><div id="plot"></div><script>const D={payload};const s=document.getElementById('p');Object.keys(D.summary).forEach(p=>{{let o=document.createElement('option');o.value=p;o.textContent=p;s.appendChild(o)}});function draw(){{let p=s.value,r=D.rows.filter(x=>x.parameter===p);let lo=Math.min(...r.map(x=>Math.min(x.reference,x.prediction))),hi=Math.max(...r.map(x=>Math.max(x.reference,x.prediction)));Plotly.react('plot',[{{x:r.map(x=>x.reference),y:r.map(x=>x.prediction),mode:'markers',type:'scatter',text:r.map(x=>x.time+'<br>'+x.point),hovertemplate:'Reference=%{{x:.4g}}<br>MicroMet=%{{y:.4g}}<br>%{{text}}<extra></extra>'}},{{x:[lo,hi],y:[lo,hi],mode:'lines',name:'1:1'}}],{{title:p+' | MAE='+D.summary[p].mae.toFixed(3)+' RMSE='+D.summary[p].rmse.toFixed(3)+' Bias='+D.summary[p].bias.toFixed(3),xaxis:{{title:'Reference'}},yaxis:{{title:'MicroMet FMU estimate'}},margin:{{t:70}}}},{{responsive:true}})}}s.onchange=draw;s.value=Object.keys(D.summary)[0];draw();</script></body></html>'''
    path.write_text(html,encoding='utf-8')

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--input',default='climate_dataset/openmeteo_24h.csv'); ap.add_argument('--region',default='region/region_v1.geojson'); ap.add_argument('--fmu',default='fmu/MicroMetTwin.fmu')
    ap.add_argument('--elevation-ground-level',type=float,default=10.0); ap.add_argument('--points',type=int,default=5); ap.add_argument('--time-step',type=int,default=3)
    ap.add_argument('--output-dir',default='evaluation'); ap.add_argument('--timeout',type=float,default=5); ap.add_argument('--reference-cache',default=None); ap.add_argument('--reference-mode',choices=['auto','online','input'],default='auto',help='auto: online once then offline fallback; online: require API; input: fully offline nearest-source reference')
    ap.add_argument('--dem',default=None,help='Shared terrain_obstacles.tif used by all TDDT twins')
    ap.add_argument('--dem-dir',default=None,help='Directory containing shared terrain_obstacles.tif')
    ap.add_argument('--terrain-policy',choices=['prefer-dem','require-dem'],default='require-dem')
    ap.add_argument('--curvature-radius-m',type=float,default=1000.0)
    args=ap.parse_args(); source=pd.read_csv(args.input); times=sorted(source.time.astype(str).unique())[::max(1,args.time_step)]
    reg=load_region_geojson(args.region); pts=sample_points(reg,args.points); out=[]; urls=[]
    cache=json.loads(Path(args.reference_cache).read_text()) if args.reference_cache else None
    effective_mode='cache' if cache else args.reference_mode
    fallback_reason=None; source_points=[]
    for pi,(lon,lat) in enumerate(pts):
      if cache:
        ref=pd.DataFrame(cache[str(pi)]['rows']); elev=cache[str(pi)].get('elevation_m',float('nan')); url='cache'; source_pt=None
      elif effective_mode=='input':
        ref,url,elev,source_pt=input_reference(source,lat,lon); url=url.replace('forecast.csv',Path(args.input).name)
      else:
        try:
          ref,url,elev=fetch_reference(lat,lon,times[0],times[-1],args.timeout); source_pt=None
        except NETWORK_ERRORS as exc:
          if args.reference_mode=='online': raise
          effective_mode='input'
          fallback_reason=f'{type(exc).__name__}: {exc}'
          ref,url,elev,source_pt=input_reference(source,lat,lon); url=url.replace('forecast.csv',Path(args.input).name)
      urls.append(url); source_points.append(source_pt); ref=ref.set_index('time')
      for t in times:
        if t not in ref.index: continue
        ns=SimpleNamespace(fmu=args.fmu,input=args.input,time=t,latitude=lat,longitude=lon,elevation_sea_level=None,elevation_ground_level=args.elevation_ground_level,ground_elevation_m=(elev if effective_mode in ('online','cache') and np.isfinite(elev) else None),slope_deg=0.0,aspect_deg=0.0,curvature=0.0,barnes_filter_km=20.0,wind_3dw_dx_m=100.0,wind_3dw_dy_m=100.0,wind_3dw_dz_m=10.0,wind_profile_power=0.143,dem=args.dem,dem_dir=args.dem_dir,terrain_policy=args.terrain_policy,curvature_radius_m=args.curvature_radius_m)
        est=estimate(ns)
        if est.get('ok') is False: continue
        fo=est['fmu_outputs']
        for p,(rv,unit) in MAP.items():
          if p not in fo or rv not in ref.columns or pd.isna(ref.loc[t,rv]): continue
          pred=float(fo[p]); reference=float(ref.loc[t,rv]);
          if p=='surface_pressure_pa': reference*=100.0
          err=circular_error(pred,reference) if p=='wind_direction_terrain_deg' else pred-reference
          out.append({'point':pi,'latitude':lat,'longitude':lon,'time':t,'parameter':p,'unit':unit,'prediction':pred,'reference':reference,'error':err,'absolute_error':abs(err)})
    df=pd.DataFrame(out)
    if df.empty: raise RuntimeError('No comparable evaluation records were produced')
    summary={p:metrics(g) for p,g in df.groupby('parameter')}
    od=Path(args.output_dir); od.mkdir(parents=True,exist_ok=True)
    df.to_csv(od/'micromet_evaluation_records.csv',index=False)
    if effective_mode=='input':
      reference_label='Input forecast CSV (offline nearest-source reference)'
      notice='Offline forecast-to-forecast forcing-consistency check. The nearest source point from --input is used as reference for each sampled target; this is not same-location Open-Meteo validation and not independent observational ground truth.'
    elif effective_mode=='cache':
      reference_label='Reference cache'
      notice='Forecast-to-forecast consistency check using the supplied reference cache; this is not independent observational ground truth.'
    else:
      reference_label='Open-Meteo Forecast API (online, same sampled coordinates)'
      notice='Forecast-to-forecast consistency check against Open-Meteo at sampled coordinates; it is not independent observational ground truth.'
    result={'ok':True,'reference':reference_label,'reference_mode_requested':args.reference_mode,'reference_mode_used':effective_mode,'network_fallback_reason':fallback_reason,'notice':notice,'region':args.region,'points':len(pts),'times':len(times),'elevation_agl_m':args.elevation_ground_level,'terrain_dem':args.dem or args.dem_dir,'terrain_policy':args.terrain_policy,'metrics':summary,'reference_urls':urls,'offline_reference_source_points':source_points if effective_mode=='input' else None}
    (od/'micromet_evaluation_summary.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    write_report(df,summary,od/'micromet_evaluation_report.html',notice,reference_label)
    print(json.dumps(result,indent=2))
if __name__=='__main__': main()
