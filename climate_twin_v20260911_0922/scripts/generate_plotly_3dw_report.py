#!/usr/bin/env python3
"""Generate one-time MicroMet-3DW wind-vector report over the complete DEM region."""
from __future__ import annotations
import argparse, json, math, shutil, sys
from datetime import datetime
from pathlib import Path
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import rasterio
from rasterio.warp import transform as crs_transform, transform_bounds

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from climate_io import read_forecast, sources_at_time, build_fmi_inputs, ForecastTimeOutOfRangeError
from fmu_client import MicroMetFMU
from estimate_climate import _time_bracket, _sparse_profile_inputs
from terrain_truth import TerrainTruth


def find_dem(path: Path)->Path:
    if path.is_file(): return path
    for n in ('terrain_obstacles.tif','dem.tif','terrain.tif','elevation.tif'):
        p=path/n
        if p.exists(): return p
    cands=sorted(list(path.glob('*.tif'))+list(path.glob('*.tiff')))
    if not cands: raise FileNotFoundError(f'No DEM GeoTIFF found under {path}')
    return cands[0]


def polygon_from_geojson(path: Path):
    obj=json.loads(path.read_text(encoding='utf-8'))
    feats=obj.get('features',[]) if obj.get('type')=='FeatureCollection' else [obj]
    for f in feats:
        g=f.get('geometry',{})
        if g.get('type')=='Polygon': return [(float(x),float(y)) for x,y in g['coordinates'][0]]
    raise ValueError('Region GeoJSON has no Polygon geometry')


def inside(x,y,poly):
    # Boundary-inclusive point-in-polygon so rectangular grid edges are retained.
    eps=1e-10
    c=False;j=len(poly)-1
    for i in range(len(poly)):
        xi,yi=poly[i];xj,yj=poly[j]
        cross=(x-xj)*(yi-yj)-(y-yj)*(xi-xj)
        if abs(cross)<=eps and min(xi,xj)-eps<=x<=max(xi,xj)+eps and min(yi,yj)-eps<=y<=max(yi,yj)+eps:
            return True
        if ((yi>y)!=(yj>y)) and x < (xj-xi)*(y-yi)/(yj-yi+1e-30)+xi: c=not c
        j=i
    return c


class DEM:
    def __init__(self,path:Path):
        self.ds=rasterio.open(path);self.path=path
        if self.ds.crs is None: raise ValueError('DEM CRS missing')
    def close(self): self.ds.close()
    def _xy(self,lon,lat):
        if self.ds.crs.to_string().upper()=='EPSG:4326': return lon,lat
        xs,ys=crs_transform('EPSG:4326',self.ds.crs,[lon],[lat]);return xs[0],ys[0]
    def safe_wgs84_bounds(self):
        west,south,east,north=transform_bounds(self.ds.crs,'EPSG:4326',*self.ds.bounds,densify_pts=21)
        # Keep a 1.5-pixel interior margin because slope estimation uses a 3x3 window.
        dx=max((east-west)/max(self.ds.width,1),0.0)
        dy=max((north-south)/max(self.ds.height,1),0.0)
        mx=1.5*dx;my=1.5*dy
        return west+mx,south+my,east-mx,north-my
    def contains(self,lon,lat):
        x,y=self._xy(lon,lat)
        b=self.ds.bounds
        return (b.left <= x < b.right) and (b.bottom < y <= b.top)
    def sample(self,lon,lat):
        if not self.contains(lon,lat):
            raise ValueError(f'Point is outside DEM coverage at {lon},{lat}')
        x,y=self._xy(lon,lat);row,col=self.ds.index(x,y)
        if row < 1 or row > self.ds.height-2 or col < 1 or col > self.ds.width-2:
            raise ValueError(f'Point is too close to DEM edge for 3x3 slope sampling at {lon},{lat}')
        a=self.ds.read(1,window=((row-1,row+2),(col-1,col+2)),masked=True).astype(float)
        a=np.asarray(a.filled(np.nan) if hasattr(a,'filled') else a,float)
        if not np.isfinite(a[1,1]): raise ValueError(f'DEM has no finite elevation at {lon},{lat}')
        if self.ds.crs.is_projected:
            dx=abs(self.ds.transform.a);dy=abs(self.ds.transform.e)
        else:
            latr=math.radians(lat);dx=abs(self.ds.transform.a)*111320*max(math.cos(latr),1e-6);dy=abs(self.ds.transform.e)*110540
        dzdx=(a[1,2]-a[1,0])/(2*max(dx,1e-6)) if np.isfinite(a[1,2]) and np.isfinite(a[1,0]) else 0.0
        dzdy=-(a[2,1]-a[0,1])/(2*max(dy,1e-6)) if np.isfinite(a[2,1]) and np.isfinite(a[0,1]) else 0.0
        return float(a[1,1]),float(dzdx),float(dzdy)


def slope_aspect(dzdx,dzdy):
    slope=math.degrees(math.atan(math.hypot(dzdx,dzdy)))
    aspect=(math.degrees(1.5*math.pi-math.atan2(dzdy,dzdx)))%360.0
    return slope,aspect


def endpoint_winds(fmu,rows,t,lat,lon,msl,slope,aspect,curvature,barnes):
    t0,t1,alpha=_time_bracket(rows,t)
    uv=[]
    for tt in (t0,t1):
        ss=sources_at_time(rows,tt);ii=build_fmi_inputs(ss,latitude=lat,longitude=lon,elevation_m=msl,requested_time=tt,slope_deg=slope,aspect_deg=aspect,curvature=curvature,barnes_filter_km=barnes)
        ii['wind_3dw_enabled']=0.0;oo=fmu.step(ii);uv.append((oo['wind_u_terrain'],oo['wind_v_terrain']))
    return t0,t1,alpha,uv


def eval_point(fmu,rows,t,lat,lon,terrain_sample,agl,barnes,fdxy,fdz,power):
    ground=terrain_sample.ground_elevation_m; slope=terrain_sample.slope_deg; aspect=terrain_sample.aspect_deg; curvature=terrain_sample.curvature_scaled
    msl=ground+agl;t0,t1,alpha,uv=endpoint_winds(fmu,rows,t,lat,lon,msl,slope,aspect,curvature,barnes)
    ss=sources_at_time(rows,t);ii=build_fmi_inputs(ss,latitude=lat,longitude=lon,elevation_m=msl,requested_time=t,slope_deg=slope,aspect_deg=aspect,curvature=curvature,barnes_filter_km=barnes)
    ii.update(terrain_sample.fmi_overrides())
    ii.update({'wind_3dw_enabled':1.0,'altitude_agl_m':agl,'fd_dx_m':fdxy,'fd_dy_m':fdxy,'fd_dz_m':fdz,
      'temporal_t0_u10':uv[0][0],'temporal_t0_v10':uv[0][1],'temporal_t1_u10':uv[1][0],'temporal_t1_v10':uv[1][1],'temporal_interval_s':max(0,(t1-t0).total_seconds()),'temporal_alpha':alpha,'wind_profile_power':power})
    ii.update(_sparse_profile_inputs(fmu,rows,t,latitude=lat,longitude=lon,altitude_msl_m=msl,slope_deg=slope,aspect_deg=aspect,curvature=curvature,barnes_filter_km=barnes))
    oo=fmu.step(ii);return oo,t0,t1


def main():
    ap=argparse.ArgumentParser(description='Plot all MicroMet-3DW wind vectors over a DEM at one forecast time.')
    ap.add_argument('--input',required=True);ap.add_argument('--region',required=True);ap.add_argument('--fmu',required=True);ap.add_argument('--time',required=True);ap.add_argument('--levels-agl',default='10,80,180');ap.add_argument('--grid-n',type=int,default=5);ap.add_argument('--dem-dir',required=True);ap.add_argument('--output',required=True)
    ap.add_argument('--barnes-filter-km',type=float,default=20.0);ap.add_argument('--fd-xy-m',type=float,default=100.0);ap.add_argument('--fd-z-m',type=float,default=10.0);ap.add_argument('--wind-profile-power',type=float,default=0.143)
    args=ap.parse_args();t=datetime.fromisoformat(args.time);levels=[float(x) for x in args.levels_agl.split(',') if x.strip()];poly=polygon_from_geojson(Path(args.region));west=min(x for x,y in poly);east=max(x for x,y in poly);south=min(y for x,y in poly);north=max(y for x,y in poly)
    forecast=read_forecast(args.input)
    # Force range validation before opening expensive DEM/FMU loop.
    try:sources_at_time(forecast,t)
    except ForecastTimeOutOfRangeError as e:
        print(json.dumps({'ok':False,'error':'FORECAST_TIME_OUT_OF_RANGE','requested':t.isoformat(),'forecast_start':e.forecast_start.isoformat(),'forecast_end':e.forecast_end.isoformat()},indent=2));raise SystemExit(2)
    dem_path=find_dem(Path(args.dem_dir));dem=DEM(dem_path);terrain=TerrainTruth(dem_path,curvature_radius_m=1000.0);recs=[];skipped_dem=[]
    try:
        dw,ds,de,dn=dem.safe_wgs84_bounds()
        iw=max(west,dw);ie=min(east,de);is_=max(south,ds);in_=min(north,dn)
        if ie <= iw or in_ <= is_:
            raise RuntimeError(f'Region and DEM do not overlap. region={[west,south,east,north]}, dem_safe={[dw,ds,de,dn]}')
        lons=np.linspace(iw,ie,max(args.grid_n,2));lats=np.linspace(is_,in_,max(args.grid_n,2));
        with MicroMetFMU(args.fmu) as fmu:
            for lat in lats:
                for lon in lons:
                    if not inside(float(lon),float(lat),poly) and not any(abs(lon-px)<1e-12 and abs(lat-py)<1e-12 for px,py in poly): continue
                    try:
                        ts=terrain.sample(float(lon),float(lat))
                    except ValueError as exc:
                        skipped_dem.append({'longitude':float(lon),'latitude':float(lat),'reason':str(exc)})
                        continue
                    for agl in levels:
                        oo,t0,t1=eval_point(fmu,forecast,t,float(lat),float(lon),ts,agl,args.barnes_filter_km,args.fd_xy_m,args.fd_z_m,args.wind_profile_power)
                        recs.append({'longitude':float(lon),'latitude':float(lat),'ground_elevation_m':ts.ground_elevation_m,'slope_deg':ts.slope_deg,'aspect_deg':ts.aspect_deg,'curvature_scaled':ts.curvature_scaled,'curvature_raw':ts.curvature_raw,'terrain_source':ts.source_path,'altitude_agl_m':agl,'altitude_msl_m':ts.ground_elevation_m+agl,'forecast_t0':t0.isoformat(),'forecast_t1':t1.isoformat(),**{k:oo[k] for k in ('wind_u_3dw','wind_v_3dw','diagnostic_vertical_wind','wind_speed_3d','du_dx','du_dy','du_dz','dv_dx','dv_dy','dv_dz','dw_dx','dw_dy','dw_dz','du_dt','dv_dt','dw_dt','Du_Dt','Dv_Dt','Dw_Dt','continuity_residual','wind_3dw_valid','wind_3dw_profile_mode')}})
    finally:
        dem.close();terrain.close()
    if not recs: raise RuntimeError('No finite DEM grid points were available inside the Region/DEM intersection')
    df=pd.DataFrame(recs);clon=(west+east)/2;clat=(south+north)/2;mx=111320*math.cos(math.radians(clat));my=110540
    df['east_m']=(df.longitude-clon)*mx;df['north_m']=(df.latitude-clat)*my
    fig=go.Figure()
    # terrain support surface at report grid points (one point per horizontal location)
    h=df.drop_duplicates(['longitude','latitude']).sort_values(['latitude','longitude']);
    # Scatter terrain is robust for arbitrary polygon masks; vectors remain the focal report.
    fig.add_trace(go.Scatter3d(x=h.east_m,y=h.north_m,z=h.ground_elevation_m,mode='markers',marker=dict(size=3,color=h.ground_elevation_m,colorscale='Earth',opacity=.55),name='DEM samples',hovertemplate='E=%{x:.0f} m<br>N=%{y:.0f} m<br>DEM=%{z:.1f} m<extra></extra>'))
    cone_custom=np.column_stack([df.altitude_agl_m.to_numpy(float),df.altitude_msl_m.to_numpy(float),df.ground_elevation_m.to_numpy(float)])
    fig.add_trace(go.Cone(x=df.east_m,y=df.north_m,z=df.altitude_msl_m,u=df.wind_u_3dw,v=df.wind_v_3dw,w=df.diagnostic_vertical_wind,customdata=cone_custom,anchor='tail',sizemode='scaled',sizeref=max(float(df.wind_speed_3d.quantile(.9)),.5),colorscale='Turbo',colorbar=dict(title='3D wind'),name='MicroMet-3DW wind',hovertemplate='E=%{x:.0f} m<br>N=%{y:.0f} m<br>AGL=%{customdata[0]:.1f} m<br>Altitude MSL=%{customdata[1]:.1f} m<br>Ground MSL=%{customdata[2]:.1f} m<br>u=%{u:.2f} m/s<br>v=%{v:.2f} m/s<br>w=%{w:.3f} m/s<extra></extra>'))
    fig.update_layout(title=f'MicroMet-3DW wind field — {t.isoformat()} — requested AGL levels {levels}; plotted Z = DEM MSL + AGL',scene=dict(xaxis_title='East from region center (m)',yaxis_title='North from region center (m)',zaxis_title='Absolute altitude MSL (m) — requested levels are AGL',aspectmode='data'),margin=dict(l=0,r=0,t=80,b=0))
    out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True);fig.write_html(out,include_plotlyjs=True,full_html=True)
    csvp=out.with_suffix('.csv');df.to_csv(csvp,index=False)
    manifest={'ok':True,'report':str(out),'records':len(df),'horizontal_points':int(df[['longitude','latitude']].drop_duplicates().shape[0]),'requested_grid_n':int(args.grid_n),'skipped_dem_points':len(skipped_dem),'region_bounds_wgs84':[west,south,east,north],'dem_safe_bounds_wgs84':[dw,ds,de,dn],'sampled_intersection_bounds_wgs84':[iw,is_,ie,in_],'levels_agl_m':levels,'terrain_source':str(dem_path),'terrain_fields':'elevation/slope/aspect from terrain_obstacles.tif; curvature derived from the same elevation band','time':t.isoformat(),'forecast_start':min(r['time_dt'] for r in forecast).isoformat(),'forecast_end':max(r['time_dt'] for r in forecast).isoformat(),'profile_mode':'fallback_10m_power_law' if (df.wind_3dw_profile_mode==0).all() else 'sparse_levels','diagnostic_vertical_wind_note':'continuity/DEM-derived; not CFD, convection, gust, rotor, wake, or turbulence'}
    out.with_suffix('.manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8');print(json.dumps(manifest,indent=2))
if __name__=='__main__':main()
