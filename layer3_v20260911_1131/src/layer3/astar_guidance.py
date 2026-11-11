from __future__ import annotations

import heapq
import math
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .config import Layer3Config


@dataclass
class AStarPath:
    cells: list[tuple[int,int]]
    local_enu_m: list[list[float]]
    altitude_msl_m: list[float]
    cost: float
    path_length_m: float
    min_obstacle_clearance_m: float
    min_terrain_clearance_m: float
    expanded_nodes: int
    planner_feedback: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            'cells':[list(x) for x in self.cells], 'local_enu_m':self.local_enu_m,
            'altitude_msl_m':self.altitude_msl_m, 'cost':self.cost,
            'path_length_m':self.path_length_m,
            'min_obstacle_clearance_m':self.min_obstacle_clearance_m,
            'min_terrain_clearance_m':self.min_terrain_clearance_m,
            'expanded_nodes':self.expanded_nodes, 'planner_feedback':dict(self.planner_feedback),
        }


class RasterAStarPlanner:
    """8-neighbour A* over the authoritative Boulder raster.

    Hard obstacles come from building/artificial/natural obstacle bands. Cost is
    distance + slope/elevation + obstacle proximity + wind + Layer-3 risk.
    Runtime MESA uncertainty expands the obstacle penalty and MicroMet wind / Meso
    risk are accepted as feedback for replanning.
    """
    def __init__(self, config: Layer3Config, *, origin_projected_m: tuple[float,float], hour_utc: int = 12):
        import rasterio
        self.config=config; self.origin_x=float(origin_projected_m[0]); self.origin_y=float(origin_projected_m[1])
        terrain_path=config.map_root/'terrain_obstacles.tif'; climate_path=config.map_root/'climate_contexts.tif'
        if not terrain_path.is_file(): raise FileNotFoundError(terrain_path)
        with rasterio.open(terrain_path) as ds:
            self.transform=ds.transform; self.crs=str(ds.crs); self.height=ds.height; self.width=ds.width
            bands={name:i+1 for i,name in enumerate(ds.descriptions or [])}
            def read(name,default=0.0):
                if name not in bands:return np.full((ds.height,ds.width),default,dtype=float)
                a=ds.read(bands[name]).astype(float)
                if ds.nodata is not None:a[np.isclose(a,float(ds.nodata))]=np.nan
                return a
            self.terrain=read('terrain_elevation_m'); self.slope=read('terrain_slope_deg')
            self.building=read('building_mask'); self.building_height=read('building_height_m')
            self.natural=read('natural_obstacle_class'); self.artificial=read('artificial_obstacle_class')
            self.resolution=float((abs(ds.transform.a)+abs(ds.transform.e))/2.0)
        self.wind_e=np.zeros_like(self.terrain); self.wind_n=np.zeros_like(self.terrain)
        if climate_path.is_file():
            with rasterio.open(climate_path) as ds:
                desc=list(ds.descriptions or []); slot='00_06' if hour_utc<6 else '06_12' if hour_utc<12 else '12_18' if hour_utc<18 else '18_24'
                for attr,name in [('wind_e',f'wind_u_east_ms_{slot}'),('wind_n',f'wind_v_north_ms_{slot}')]:
                    if name in desc:
                        a=ds.read(desc.index(name)+1).astype(float)
                        if ds.nodata is not None:a[np.isclose(a,float(ds.nodata))]=np.nan
                        setattr(self,attr,np.nan_to_num(a,nan=0.0))
        block=(self.building>0.5)|(self.artificial>0.0)|(self.natural>=float(config.planner.natural_obstacle_block_class))|~np.isfinite(self.terrain)
        self.blocked=block
        self.obstacle_distance_cells=self._distance_to_obstacle(block)

    @staticmethod
    def _distance_to_obstacle(mask: np.ndarray) -> np.ndarray:
        h,w=mask.shape; inf=h+w+10; dist=np.full((h,w),inf,dtype=float); q=deque()
        for r,c in zip(*np.where(mask)):
            dist[r,c]=0.0; q.append((int(r),int(c)))
        steps=((1,0,1.),(-1,0,1.),(0,1,1.),(0,-1,1.),(1,1,math.sqrt(2)),(1,-1,math.sqrt(2)),(-1,1,math.sqrt(2)),(-1,-1,math.sqrt(2)))
        while q:
            r,c=q.popleft(); base=dist[r,c]
            for dr,dc,d in steps:
                nr,nc=r+dr,c+dc
                if 0<=nr<h and 0<=nc<w and base+d<dist[nr,nc]-1e-12:
                    dist[nr,nc]=base+d; q.append((nr,nc))
        return dist

    def local_to_cell(self,east_m:float,north_m:float)->tuple[int,int]:
        import rasterio.transform
        x=self.origin_x+float(east_m); y=self.origin_y+float(north_m)
        r,c=rasterio.transform.rowcol(self.transform,x,y)
        return int(r),int(c)

    def cell_to_local(self,cell:tuple[int,int])->tuple[float,float]:
        import rasterio.transform
        r,c=cell; x,y=rasterio.transform.xy(self.transform,r,c,offset='center')
        return float(x)-self.origin_x,float(y)-self.origin_y

    def obstacle_clearance_m(self,east_m:float,north_m:float)->float:
        r,c=self.local_to_cell(east_m,north_m)
        if not (0<=r<self.height and 0<=c<self.width):return 0.0
        return float(self.obstacle_distance_cells[r,c]*self.resolution)

    def terrain_height_m(self,east_m:float,north_m:float)->float:
        r,c=self.local_to_cell(east_m,north_m)
        if not (0<=r<self.height and 0<=c<self.width):return float('nan')
        return float(self.terrain[r,c])

    def _nearest_free(self,cell:tuple[int,int])->tuple[int,int]:
        r0,c0=cell
        if 0<=r0<self.height and 0<=c0<self.width and not self.blocked[r0,c0]:return cell
        for radius in range(1,25):
            for r in range(max(0,r0-radius),min(self.height,r0+radius+1)):
                for c in range(max(0,c0-radius),min(self.width,c0+radius+1)):
                    if not self.blocked[r,c]:return r,c
        raise RuntimeError('no nearby traversable raster cell')

    def plan(self,start_local:tuple[float,float],goal_local:tuple[float,float],*,start_altitude_msl_m:float,
             runtime_feedback:Mapping[str,Any]|None=None)->AStarPath:
        fb=dict(runtime_feedback or {}); start=self._nearest_free(self.local_to_cell(*start_local)); goal=self._nearest_free(self.local_to_cell(*goal_local))
        risk=max(0.0,min(1.0,float(fb.get('risk',0.0) or 0.0))); unc=max(0.0,float(fb.get('mesa_uncertainty',0.0) or 0.0))
        dyn_we=float(fb.get('wind_e_m_s',0.0) or 0.0); dyn_wn=float(fb.get('wind_n_m_s',0.0) or 0.0)
        buffer_m=float(self.config.planner.base_obstacle_buffer_m)+float(self.config.planner.uncertainty_buffer_gain_m)*unc
        hard_buffer=float(fb.get('hard_buffer_m', min(buffer_m,float(self.config.planner.min_obstacle_clearance_m))))
        relaxation_count=int(fb.get('relaxation_count',0) or 0)
        min_clear=float(self.config.planner.min_terrain_clearance_m)
        neigh=((1,0,1.),(-1,0,1.),(0,1,1.),(0,-1,1.),(1,1,math.sqrt(2)),(1,-1,math.sqrt(2)),(-1,1,math.sqrt(2)),(-1,-1,math.sqrt(2)))
        def heuristic(a,b):return math.hypot(a[0]-b[0],a[1]-b[1])*self.resolution
        g={start:0.0}; parent={}; pq=[(heuristic(start,goal),0.0,start)]; expanded=0
        while pq:
            _,gc,cur=heapq.heappop(pq)
            if gc>g.get(cur,float('inf'))+1e-9:continue
            expanded+=1
            if cur==goal:break
            r,c=cur
            for dr,dc,mult in neigh:
                nr,nc=r+dr,c+dc
                if not (0<=nr<self.height and 0<=nc<self.width) or self.blocked[nr,nc]:continue
                step=self.resolution*mult
                clearance=float(self.obstacle_distance_cells[nr,nc]*self.resolution)
                if clearance < hard_buffer and (nr,nc) not in {start,goal}: continue
                proximity=max(0.0,(buffer_m+self.config.planner.min_obstacle_clearance_m-clearance)/max(self.config.planner.min_obstacle_clearance_m,1.0))
                slope=max(0.0,float(self.slope[nr,nc] if np.isfinite(self.slope[nr,nc]) else 90.0))
                elev_gain=max(0.0,float(self.terrain[nr,nc]-self.terrain[r,c]))
                # Move vector in ENU: raster rows decrease with Northing.
                de=dc*self.resolution; dn=-dr*self.resolution; norm=max(math.hypot(de,dn),1e-9); ue,un=de/norm,dn/norm
                we=float(self.wind_e[nr,nc])+dyn_we; wn=float(self.wind_n[nr,nc])+dyn_wn
                along=we*ue+wn*un; cross=abs(-we*un+wn*ue)
                wind_pen=(self.config.planner.wind_headwind_weight*max(0.0,-along)+self.config.planner.wind_crosswind_weight*cross)/max(self.config.planner.cruise_airspeed_m_s,1.0)
                cell_cost=step*(1.0+self.config.planner.slope_weight*slope+self.config.planner.elevation_gain_weight*elev_gain+
                               (1.0+2.0*risk)*self.config.planner.risk_weight*proximity+wind_pen)
                ng=gc+cell_cost
                nxt=(nr,nc)
                if ng<g.get(nxt,float('inf')):
                    g[nxt]=ng; parent[nxt]=cur; heapq.heappush(pq,(ng+heuristic(nxt,goal),ng,nxt))
        if goal not in g:
            if hard_buffer > 2.0 and relaxation_count < 4:
                retry=dict(fb); retry['hard_buffer_m']=hard_buffer*0.5; retry['relaxation_count']=relaxation_count+1
                return self.plan(start_local,goal_local,start_altitude_msl_m=start_altitude_msl_m,runtime_feedback=retry)
            raise RuntimeError('A* failed to find a traversable route even after obstacle-buffer relaxation')
        cells=[goal]
        while cells[-1]!=start:cells.append(parent[cells[-1]])
        cells.reverse(); local=[list(self.cell_to_local(c)) for c in cells]
        alts=[max(float(start_altitude_msl_m),float(self.terrain[r,c])+min_clear) for r,c in cells]
        length=sum(math.hypot(local[i][0]-local[i-1][0],local[i][1]-local[i-1][1]) for i in range(1,len(local)))
        obs=min(float(self.obstacle_distance_cells[r,c]*self.resolution) for r,c in cells)
        terr=min(alts[i]-float(self.terrain[r,c]) for i,(r,c) in enumerate(cells))
        return AStarPath(cells,local,alts,float(g[goal]),float(length),float(obs),float(terr),expanded,
                         {'risk':risk,'mesa_uncertainty':unc,'runtime_wind_e_m_s':dyn_we,'runtime_wind_n_m_s':dyn_wn,'obstacle_buffer_m':buffer_m,'hard_buffer_m':hard_buffer,'relaxation_count':relaxation_count})


def nearest_path_index(path:list[list[float]],point:tuple[float,float])->int:
    if not path:return 0
    return min(range(len(path)),key=lambda i:(path[i][0]-point[0])**2+(path[i][1]-point[1])**2)


def cross_track_error(path:list[list[float]],point:tuple[float,float])->float:
    if len(path)<2:return 0.0
    px,py=point; best=float('inf')
    for a,b in zip(path[:-1],path[1:]):
        ax,ay=a; bx,by=b; dx,dy=bx-ax,by-ay; den=dx*dx+dy*dy
        t=0.0 if den<=1e-12 else max(0.0,min(1.0,((px-ax)*dx+(py-ay)*dy)/den))
        qx,qy=ax+t*dx,ay+t*dy; best=min(best,math.hypot(px-qx,py-qy))
    return float(best)


def point_along_path(path:list[list[float]],start_index:int,distance_m:float)->tuple[list[float],int]:
    if not path:return [0.0,0.0],0
    i=max(0,min(int(start_index),len(path)-1)); remain=max(0.0,float(distance_m)); cur=list(path[i])
    while i<len(path)-1:
        nxt=path[i+1]; d=math.hypot(nxt[0]-cur[0],nxt[1]-cur[1])
        if d>=remain and d>1e-9:
            t=remain/d; return [cur[0]+t*(nxt[0]-cur[0]),cur[1]+t*(nxt[1]-cur[1])],i
        remain-=d; i+=1; cur=list(path[i])
    return list(path[-1]),len(path)-1
