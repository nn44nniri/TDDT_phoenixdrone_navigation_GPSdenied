from __future__ import annotations

import copy
import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .astar_guidance import RasterAStarPlanner, cross_track_error, nearest_path_index, point_along_path
from .config import Layer3Config
from .objectives import ObjectiveRegistry
from .runtime import Layer3FMURuntime, RuntimeRequest
from .utils import write_json


@dataclass
class ScenarioArtifacts:
    output_dir: Path
    result_json: Path
    summary_json: Path
    timeseries_csv: Path
    report_3d_html: Path
    report_2d_html: Path

    def to_dict(self) -> dict[str, str]:
        return {k: str(v.resolve()) for k, v in self.__dict__.items() if isinstance(v, Path)}


def _value(obj: Any, default: Any = None) -> Any:
    if isinstance(obj, Mapping) and "value" in obj: return obj.get("value", default)
    return obj if obj is not None else default


def _set_value(container: dict[str, Any], key: str, value: Any) -> None:
    item=container.get(key)
    if isinstance(item,Mapping) and "value" in item:
        item=dict(item); item["value"]=value; container[key]=item
    else: container[key]=value


def _finite(value: Any) -> float | None:
    try:
        x=float(value); return x if math.isfinite(x) else None
    except Exception:return None


def _envelope(value: Any, unit: str, frame: str, semantic: str, *, source: str) -> dict[str, Any]:
    return {"value":value,"unit":unit,"frame":frame,"semantic":semantic,"uncertainty":None,"source":source,
            "provenance":{"origin":source,"contract":"explicit TDOC envelope"}}


def region_geojson_path(config: Layer3Config) -> Path:
    for path in (config.boulder_root/"region"/"region_v1.geojson",config.map_root.parent/"region"/"region_v1.geojson",config.climate_root/"region"/"region_v1.geojson"):
        if path.is_file(): return path
    raise FileNotFoundError("region_v1.geojson was not found")


def load_region_definition(config: Layer3Config) -> dict[str, Any]:
    path=region_geojson_path(config); data=json.loads(path.read_text(encoding="utf-8"))
    roles={str((f.get("properties") or {}).get("feature_role") or "").lower():f for f in (data.get("features") or [])}
    if not all(k in roles for k in ("region_boundary","start","destination")):raise ValueError("region_v1.geojson must contain boundary/start/destination")
    props=roles["region_boundary"].get("properties") or {}
    return {"path":str(path.resolve()),"name":data.get("name") or props.get("name"),"bbox_wgs84":list(data.get("bbox") or []),
            "processing_crs":str(props.get("processing_crs") or "EPSG:32613"),"boundary":roles["region_boundary"],"start":roles["start"],
            "destination":roles["destination"],"center":roles.get("center"),"area_km2":props.get("area_km2"),
            "declared_route_distance_m":props.get("start_destination_distance_m")}


def _lonlat(feature: Mapping[str, Any]) -> tuple[float,float]:
    c=((feature.get("geometry") or {}).get("coordinates") or []); return float(c[0]),float(c[1])


def _project_lonlat(lon:float,lat:float,target_crs:str)->tuple[float,float]:
    from pyproj import Transformer
    x,y=Transformer.from_crs("EPSG:4326",target_crs,always_xy=True).transform(lon,lat); return float(x),float(y)


def _inverse_project(x:float,y:float,source_crs:str)->tuple[float,float]:
    from pyproj import Transformer
    lon,lat=Transformer.from_crs(source_crs,"EPSG:4326",always_xy=True).transform(x,y); return float(lon),float(lat)


def _sample_terrain(config:Layer3Config,lon:float,lat:float)->float|None:
    import rasterio
    from pyproj import Transformer
    p=config.map_root/"terrain_obstacles.tif"
    if not p.is_file():return None
    with rasterio.open(p) as ds:
        x,y=Transformer.from_crs("EPSG:4326",ds.crs,always_xy=True).transform(lon,lat); v=float(next(ds.sample([(x,y)]))[0])
        return None if (ds.nodata is not None and v==float(ds.nodata)) or not math.isfinite(v) else v


def prepare_region_scenario_request(config:Layer3Config,base:Mapping[str,Any])->tuple[dict[str,Any],dict[str,Any]]:
    data=copy.deepcopy(dict(base)); region=load_region_definition(config); slon,slat=_lonlat(region["start"]); dlon,dlat=_lonlat(region["destination"])
    sx,sy=_project_lonlat(slon,slat,region["processing_crs"]); dx,dy=_project_lonlat(dlon,dlat,region["processing_crs"]); local_goal=[dx-sx,dy-sy,0.0]
    climate=data.setdefault("climate",{}); _set_value(climate,"latitude",slat); _set_value(climate,"longitude",slon)
    wing=data.setdefault("wing_inputs",{}); _set_value(wing,"state_x",0.0); _set_value(wing,"state_y",0.0)
    start_h=float(_value(wing.get("state_h"),1800.0)); terr_s=_sample_terrain(config,slon,slat); terr_d=_sample_terrain(config,dlon,dlat)
    if terr_s is not None and start_h<terr_s+config.planner.min_terrain_clearance_m:start_h=terr_s+config.planner.min_terrain_clearance_m; _set_value(wing,"state_h",start_h)
    ctx=data.setdefault("context",{}); ctx["scenario_destination_enu_m"]=_envelope(local_goal,"m","ENU","scenario_destination_position",source="region_v1.geojson destination")
    ctx["target_enu_m"]=_envelope(local_goal,"m","ENU","target_position",source="A* initial destination")
    ctx["geodetic_origin"]={"latitude":_envelope(slat,"deg","WGS84","latitude",source="region start"),"longitude":_envelope(slon,"deg","WGS84","longitude",source="region start"),"altitude_msl_m":_envelope(start_h,"m","MSL","altitude_msl_m",source="scenario origin")}
    ctx["scenario_region_source"]=region["path"]; ctx["scenario_processing_crs"]=region["processing_crs"]
    return data,{**{k:region[k] for k in ("path","name","bbox_wgs84","processing_crs","area_km2","declared_route_distance_m")},
                 "start_wgs84":[slon,slat],"destination_wgs84":[dlon,dlat],"start_projected_m":[sx,sy],"destination_projected_m":[dx,dy],
                 "target_enu_m":local_goal,"computed_direct_distance_m":math.hypot(local_goal[0],local_goal[1]),"start_terrain_msl_m":terr_s,
                 "destination_terrain_msl_m":terr_d,"scenario_start_altitude_msl_m":start_h}


def _vehicle_parameters(config:Layer3Config)->dict[str,float]:
    import yaml
    d=yaml.safe_load((config.twins_root/"config"/"phoenix_drone_v3.yaml").read_text(encoding="utf-8")) or {}
    return {"mass":float((d.get("mass") or {}).get("empty_or_flight_mass_kg",0.65)),"wing_area":float((d.get("geometry") or {}).get("wing_area_m2",0.12)),
            "cd0":float((d.get("aerodynamics") or {}).get("cd0",0.045)),"induced_drag_factor":float((d.get("aerodynamics") or {}).get("induced_drag_factor",0.134)),
            "cl_max":float((d.get("aerodynamics") or {}).get("cl_max",1.05)),"battery_voltage":float((d.get("battery") or {}).get("nominal_voltage_V",11.1)),
            "initial_soc":float((d.get("battery") or {}).get("initial_soc",1.0))}


def _wing_aux(rt:Layer3FMURuntime,*,air_density:float,airspeed:float,groundspeed:float,segment_distance:float,path_prev:float,time_prev:float,energy_prev:float,
              soc:float,current_xyz:tuple[float,float,float],goal_xyz:tuple[float,float,float],initial_goal_distance:float,safety_penalty:float,vehicle:Mapping[str,float],config:Layer3Config)->dict[str,Any]:
    def ev(op:int,inputs:Mapping[str,Any])->dict[str,Any]:
        vals,_=rt._normalize_inputs("WingMPET",rt.wing,op,inputs,{},trusted_internal=True); return rt._evaluate_domain_packet("WingMPET",rt.wing,op,context={"scenario_auxiliary":True},inputs=vals).get("state") or {}
    aero=ev(9,{"air_density":air_density,"airspeed":airspeed,"mass":vehicle["mass"],"wing_area":vehicle["wing_area"],"cd0":vehicle["cd0"],"induced_drag_factor":vehicle["induced_drag_factor"],"cl_max":vehicle["cl_max"],"bank_angle":0.0,"flightpath_angle":0.0,"lift_command":1.0})
    seg_dt=max(segment_distance/max(groundspeed,1.0),0.05)
    power=ev(31,{"dt":seg_dt,"air_density":air_density,"airspeed":airspeed,"total_thrust":max(float(aero.get("drag",0.0)),0.0),"soc":soc,"battery_voltage":vehicle["battery_voltage"],"battery_current":0.0,"battery_telemetry_valid":0.0})
    accounting=ev(14,{"dt":seg_dt,"groundspeed":groundspeed,"power":float(power.get("selected_power",aero.get("aerodynamic_power",0.0))),"path_length_prev":path_prev,"time_prev":time_prev,"energy_prev":energy_prev})
    x,y,h=current_xyz; gx,gy,gh=goal_xyz
    goal=ev(15,{"x":x,"y":y,"h":h,"goal_x":gx,"goal_y":gy,"goal_h":gh,"initial_goal_distance":initial_goal_distance,"goal_tolerance":config.planner.arrival_tolerance_m})
    objective=ev(16,{"path_length":float(accounting.get("path_length_total",path_prev)),"mission_time":float(accounting.get("time_total",time_prev)),"energy":float(accounting.get("energy_total",energy_prev)),"safety_penalty":float(safety_penalty),"length_reference":initial_goal_distance,"time_reference":config.planner.time_budget_s,"energy_reference":config.planner.energy_budget_j,"safety_reference":1.0,"weight_length":0.15,"weight_time":0.20,"weight_energy":0.25,"weight_safety":0.40})
    return {"aerodynamics":aero,"power":power,"accounting":accounting,"goal":goal,"objective":objective,"segment_dt_s":seg_dt}


def run_layer3_scenario(config:Layer3Config,request:Mapping[str,Any],*,steps:int|None=None)->dict[str,Any]:
    prepared,region=prepare_region_scenario_request(config,request); max_steps=int(steps or config.planner.max_steps); start_h=float(region["scenario_start_altitude_msl_m"])
    planner=RasterAStarPlanner(config,origin_projected_m=tuple(region["start_projected_m"]),hour_utc=12); registry=ObjectiveRegistry(config); vehicle=_vehicle_parameters(config)
    goal=(float(region["target_enu_m"][0]),float(region["target_enu_m"][1])); current=[0.0,0.0,start_h]; initial_goal=math.hypot(goal[0],goal[1]); registry.set_baseline("distance_to_goal",initial_goal); registry.set_baseline("arrival",initial_goal)
    feedback={"risk":0.0,"mesa_uncertainty":0.0,"wind_e_m_s":0.0,"wind_n_m_s":0.0}; path=None; replans=[]; results=[]; fast_metrics=[]; path_prev=time_prev=energy_prev=0.0; soc=float(vehicle["initial_soc"]); replan_reason="initial"
    with Layer3FMURuntime(config) as rt:
        for i in range(max_steps):
            if path is None or replan_reason:
                path=planner.plan((current[0],current[1]),goal,start_altitude_msl_m=current[2],runtime_feedback=feedback)
                replans.append({"tick":i+1,"reason":replan_reason or "requested","path":path.to_dict()}); replan_reason=""
            remaining=max(0.0,sum(math.hypot(path.local_enu_m[j][0]-path.local_enu_m[j-1][0],path.local_enu_m[j][1]-path.local_enu_m[j-1][1]) for j in range(1,len(path.local_enu_m))))
            idx=nearest_path_index(path.local_enu_m,(current[0],current[1])); rem_ticks=max(1,max_steps-i); advance=max(float(config.planner.lookahead_m),remaining/rem_ticks)
            target_xy,_=point_along_path(path.local_enu_m,idx,advance); terr_target=planner.terrain_height_m(*target_xy); target_h=max(start_h,terr_target+config.planner.min_terrain_clearance_m)
            # Bounded disturbance: actual MicroMet wind drift plus two deterministic cross-track pulses. A* replanning must recover it.
            dx,dy=target_xy[0]-current[0],target_xy[1]-current[1]; norm=max(math.hypot(dx,dy),1e-9); nx,ny=-dy/norm,dx/norm
            pulse=18.0 if i==6 else -14.0 if i==12 else 0.0; drift_e=0.12*float(feedback.get("wind_e_m_s",0.0))+nx*pulse; drift_n=0.12*float(feedback.get("wind_n_m_s",0.0))+ny*pulse
            dist_goal_before=math.hypot(goal[0]-current[0],goal[1]-current[1]); final_segment=dist_goal_before<=advance*1.15
            next_x=goal[0] if final_segment else target_xy[0]+drift_e; next_y=goal[1] if final_segment else target_xy[1]+drift_n; next_h=max(start_h,planner.terrain_height_m(next_x,next_y)+config.planner.min_terrain_clearance_m)
            seg=math.hypot(next_x-current[0],next_y-current[1]); heading=math.atan2(next_y-current[1],next_x-current[0]); va=float(config.planner.cruise_airspeed_m_s)
            data=copy.deepcopy(prepared); data["reference_time_s"]=float(prepared.get("reference_time_s",0.001))+i*config.time.fast_dt_s
            wing=data.setdefault("wing_inputs",{}); _set_value(wing,"state_x",next_x); _set_value(wing,"state_y",next_y); _set_value(wing,"state_h",next_h); _set_value(wing,"air_velocity_x",va*math.cos(heading)); _set_value(wing,"air_velocity_y",va*math.sin(heading)); _set_value(wing,"air_velocity_h",0.0)
            ctx=data.setdefault("context",{}); ctx["target_enu_m"]=_envelope([target_xy[0],target_xy[1],target_h],"m","ENU","target_position",source="A* lookahead waypoint"); ctx["scenario_destination_enu_m"]=prepared["context"]["scenario_destination_enu_m"]
            # Query MicroMet at the actual A*-guided endpoint.
            absx=region["start_projected_m"][0]+next_x; absy=region["start_projected_m"][1]+next_y; lon,lat=_inverse_project(absx,absy,region["processing_crs"]); clim=data.setdefault("climate",{}); _set_value(clim,"longitude",lon); _set_value(clim,"latitude",lat)
            titem=clim.get("time")
            if isinstance(titem,Mapping) and "value" in titem:
                try:
                    dt=datetime.fromisoformat(str(titem["value"]).replace("Z","+00:00"))+timedelta(seconds=time_prev); titem=dict(titem); titem["value"]=dt.isoformat(); clim["time"]=titem
                except Exception:pass
            result=rt.step(RuntimeRequest.from_mapping(data)).to_dict(); fr=result.get("fast_record") or {}; snap=result.get("snapshot") or {}; reps=((fr.get("std") or {}).get("representations") or {}); wingraw=(reps.get("WingMPET") or {}).get("raw_state") or {}; craw=(reps.get("MicroMetTwin") or {}).get("raw_state") or {}; mesa=(reps.get("MESA_NAV") or {})
            groundspeed=float(wingraw.get("groundspeed") or va); airspeed=float(wingraw.get("airspeed_norm") or va); rho=float(craw.get("air_density") or wingraw.get("air_density") or 1.0)
            terrain=planner.terrain_height_m(next_x,next_y); terrain_clear=next_h-terrain; obstacle_clear=planner.obstacle_clearance_m(next_x,next_y); risk=float((snap.get("guidance") or {}).get("risk_signal") or (fr.get("previous_meso_guidance") or {}).get("risk_signal") or 0.0)
            cte=cross_track_error(path.local_enu_m,(next_x,next_y)); wing_feasible=1.0 if terrain_clear>=config.planner.min_terrain_clearance_m and obstacle_clear>=config.planner.min_obstacle_clearance_m else 0.0
            aux=_wing_aux(rt,air_density=rho,airspeed=airspeed,groundspeed=groundspeed,segment_distance=seg,path_prev=path_prev,time_prev=time_prev,energy_prev=energy_prev,soc=soc,current_xyz=(next_x,next_y,next_h),goal_xyz=(goal[0],goal[1],start_h),initial_goal_distance=initial_goal,safety_penalty=max(risk,0.0 if wing_feasible else 1.0),vehicle=vehicle,config=config)
            accounting=aux["accounting"]; path_prev=float(accounting.get("path_length_total",path_prev)); time_prev=float(accounting.get("time_total",time_prev)); energy_prev=float(accounting.get("energy_total",energy_prev)); soc=float(aux["power"].get("next_soc",soc)); dist_goal=float(aux["goal"].get("goal_distance",math.hypot(goal[0]-next_x,goal[1]-next_y)))
            fast_obs={"distance_to_goal_m":dist_goal,"cross_track_error_m":cte,"terrain_clearance_m":terrain_clear,"obstacle_clearance_m":obstacle_clear,"wing_feasible":wing_feasible,"risk":risk,"cumulative_energy_j":energy_prev,"mission_time_s":time_prev}
            fast_obj=registry.evaluate("fast",fast_obs,provenance={"tick":i+1,"planner":"A*","twins":["MESA_NAV","WingMPET","MicroMetTwin"]}); fast_metrics.append({**fast_obs,"achievement_pct":fast_obj.get("achievement_pct")})
            meso_obj=slow_obj=None
            if result.get("meso_event"):
                window=fast_metrics[-config.time.meso_fast_steps:]; meso_obs={"mean_cross_track_error_m":float(np.mean([x["cross_track_error_m"] for x in window])),"min_terrain_clearance_m":min(x["terrain_clearance_m"] for x in window),"feasible_fraction":float(np.mean([x["wing_feasible"] for x in window])),"mean_risk":float(np.mean([x["risk"] for x in window])),"mean_fast_achievement_pct":float(np.mean([x["achievement_pct"] or 0.0 for x in window]))}
                meso_obj=registry.evaluate("meso",meso_obs,provenance={"meso_index":(result["meso_event"] or {}).get("meso_index")}); replan_reason="meso_boundary"
            if result.get("slow_event"):
                slow_obs={"distance_to_goal_m":dist_goal,"safety_fraction":float(np.mean([1.0 if x["wing_feasible"]>=0.5 else 0.0 for x in fast_metrics])),"cumulative_energy_j":energy_prev,"mission_time_s":time_prev,"mean_fast_achievement_pct":float(np.mean([x["achievement_pct"] or 0.0 for x in fast_metrics]))}
                slow_obj=registry.evaluate("slow",slow_obs,provenance={"slow_index":(result["slow_event"] or {}).get("slow_index")})
            feedback={"risk":risk,"mesa_uncertainty":float(mesa.get("normalized_uncertainty") or 0.0),"wind_e_m_s":float(craw.get("wind_u_3dw") or 0.0),"wind_n_m_s":float(craw.get("wind_v_3dw") or 0.0)}
            if cte>config.planner.replan_cross_track_m:replan_reason="cross_track"
            if terrain_clear<config.planner.min_terrain_clearance_m or obstacle_clear<config.planner.min_obstacle_clearance_m:replan_reason="safety_margin"
            result["planner"]={"name":config.planner.planner_name,"replanned_next":bool(replan_reason),"replan_reason_next":replan_reason,"active_path_cost":path.cost,"active_path_length_m":path.path_length_m,"cross_track_error_m":cte,"terrain_clearance_m":terrain_clear,"obstacle_clearance_m":obstacle_clear,"disturbance_e_m":drift_e,"disturbance_n_m":drift_n,"waypoint_enu_m":[target_xy[0],target_xy[1],target_h],"physical_enu_m":[next_x,next_y,next_h],"feedback":dict(feedback)}
            result["wing_objective_evaluation"]=aux; result["objectives"]={"fast":fast_obj,"meso":meso_obj,"slow":slow_obj}; result["scenario_input"]={"east_m":next_x,"north_m":next_y,"altitude_msl_m":next_h,"air_velocity_e_m_s":va*math.cos(heading),"air_velocity_n_m_s":va*math.sin(heading),"air_velocity_u_m_s":0.0,"segment_distance_m":seg,"physical_elapsed_s":time_prev,"energy_total_j":energy_prev,"distance_to_goal_m":dist_goal,"longitude":lon,"latitude":lat}
            results.append(result); current=[next_x,next_y,next_h]
            if dist_goal<=config.planner.arrival_tolerance_m and result.get("slow_event"):break
    return {"schema":"layer3-astar-scenario-v3","ok":True,"planner":{"name":config.planner.planner_name,"algorithm":"8-neighbour A*","cost_terms":["distance","terrain_slope","elevation_gain","obstacle_proximity","wind_head/crosswind","Layer3 risk","MESA uncertainty buffer"],"replans":replans},"steps":len(results),"fast_dt_s":config.time.fast_dt_s,"meso_events":sum(1 for r in results if r.get("meso_event")),"slow_events":sum(1 for r in results if r.get("slow_event")),"region":region,"results":results}


def extract_timeseries(run:Mapping[str,Any])->list[dict[str,Any]]:
    rows=[]
    for idx,result in enumerate(run.get("results") or []):
        fr=result.get("fast_record") or {}; snap=result.get("snapshot") or {}; reps=((fr.get("std") or {}).get("representations") or {}); mesa=reps.get("MESA_NAV") or {}; craw=(reps.get("MicroMetTwin") or {}).get("raw_state") or {}; health=fr.get("health") or {}; p=result.get("planner") or {}; si=result.get("scenario_input") or {}; obj=result.get("objectives") or {}; aux=result.get("wing_objective_evaluation") or {}; meso=result.get("meso_event") or {}; slow=result.get("slow_event") or {}
        row={"tick":idx+1,"time_s":fr.get("reference_time_s"),"flight_time_s":si.get("physical_elapsed_s"),"mismatch":fr.get("mismatch"),"consistency_margin":fr.get("consistency_margin"),"coc_status":fr.get("coc_status"),"health_mode":health.get("mode"),"risk":p.get("feedback",{}).get("risk"),"lci":(fr.get("hidden_loop") or {}).get("lci"),"tdoc_conversions":(snap.get("tdoc_health") or {}).get("conversions"),"tdoc_failures":(snap.get("tdoc_health") or {}).get("failures"),"meso_event":bool(meso),"slow_event":bool(slow),"replanned_next":p.get("replanned_next"),"replan_reason_next":p.get("replan_reason_next"),"east_m":si.get("east_m"),"north_m":si.get("north_m"),"altitude_msl_m":si.get("altitude_msl_m"),"distance_to_goal_m":si.get("distance_to_goal_m"),"segment_distance_m":si.get("segment_distance_m"),"energy_total_j":si.get("energy_total_j"),"selected_power_w":((aux.get("power") or {}).get("selected_power")),"cross_track_error_m":p.get("cross_track_error_m"),"terrain_clearance_m":p.get("terrain_clearance_m"),"obstacle_clearance_m":p.get("obstacle_clearance_m"),"wind_e_m_s":craw.get("wind_u_3dw"),"wind_n_m_s":craw.get("wind_v_3dw"),"wind_u_m_s":craw.get("diagnostic_vertical_wind"),"mesa_uncertainty":mesa.get("normalized_uncertainty"),"fast_objective_pct":((obj.get("fast") or {}).get("achievement_pct")),"meso_objective_pct":((obj.get("meso") or {}).get("achievement_pct")),"slow_objective_pct":((obj.get("slow") or {}).get("achievement_pct")),"wing_objective":((aux.get("objective") or {}).get("objective")),"goal_progress":((aux.get("goal") or {}).get("mission_progress")),"goal_reached":((aux.get("goal") or {}).get("goal_reached"))}
        for name,value in (fr.get("weights") or {}).items():row[f"weight::{name}"]=value
        for name,value in (fr.get("couplings") or {}).items():row[f"coupling::{name}"]=value
        rows.append(row)
    return rows


def build_summary(run:Mapping[str,Any],rows:list[Mapping[str,Any]])->dict[str,Any]:
    final=rows[-1] if rows else {}; fast=[_finite(r.get("fast_objective_pct")) for r in rows]; fast=[x for x in fast if x is not None]; meso=[_finite(r.get("meso_objective_pct")) for r in rows]; meso=[x for x in meso if x is not None]; slow=[_finite(r.get("slow_objective_pct")) for r in rows]; slow=[x for x in slow if x is not None]
    region=dict(run.get("region") or {}); replans=((run.get("planner") or {}).get("replans") or [])
    last_fast=(run.get("results") or [{}])[-1].get("objectives",{}).get("fast") if run.get("results") else None
    last_meso=next((r.get("objectives",{}).get("meso") for r in reversed(run.get("results") or []) if r.get("objectives",{}).get("meso")),None)
    last_slow=next((r.get("objectives",{}).get("slow") for r in reversed(run.get("results") or []) if r.get("objectives",{}).get("slow")),None)
    return {"schema":"layer3-astar-scenario-summary-v3","ok":bool(run.get("ok")) and int(_finite(final.get("tdoc_failures")) or 0)==0,
            "planner":{"name":(run.get("planner") or {}).get("name"),"algorithm":(run.get("planner") or {}).get("algorithm"),"replan_count":len(replans),"replan_ticks":[x.get("tick") for x in replans],"cost_terms":(run.get("planner") or {}).get("cost_terms")},
            "runtime_execution":{"three_twins":["MESA_NAV","WingMPET","MicroMetTwin"],"fast_steps":int(run.get("steps",0)),"fast_per_meso":None if not rows else int(round(run.get("steps",0)/max(run.get("meso_events",1),1))),"configured_fast_per_meso":None,"meso_events":int(run.get("meso_events",0)),"slow_events":int(run.get("slow_events",0)),"aggregation":"configured N Fast -> 1 Meso; configured L Meso -> 1 Slow"},
            "region":region,"mission":{"distance_to_goal_m":final.get("distance_to_goal_m"),"path_length_m":sum(float(r.get("segment_distance_m") or 0.0) for r in rows),"flight_time_s":final.get("flight_time_s"),"energy_total_j":final.get("energy_total_j"),"goal_reached":bool(float(final.get("goal_reached") or 0.0)>=0.5),"minimum_terrain_clearance_m":min([float(r.get("terrain_clearance_m")) for r in rows if _finite(r.get("terrain_clearance_m")) is not None],default=None),"minimum_obstacle_clearance_m":min([float(r.get("obstacle_clearance_m")) for r in rows if _finite(r.get("obstacle_clearance_m")) is not None],default=None),"maximum_cross_track_error_m":max([float(r.get("cross_track_error_m")) for r in rows if _finite(r.get("cross_track_error_m")) is not None],default=None)},
            "objective_attainment":{"fast_mean_pct":float(np.mean(fast)) if fast else None,"fast_final_pct":fast[-1] if fast else None,"meso_mean_pct":float(np.mean(meso)) if meso else None,"meso_final_pct":meso[-1] if meso else None,"slow_mean_pct":float(np.mean(slow)) if slow else None,"slow_final_pct":slow[-1] if slow else None,
                                    "final_fast":last_fast,"final_meso":last_meso,"final_slow":last_slow},
            "tdoc":{"final_conversions":int(_finite(final.get("tdoc_conversions")) or 0),"final_failures":int(_finite(final.get("tdoc_failures")) or 0)},
            "interpretation":"A* is the route selector/replanner. The scenario is a Layer-3 guidance/mission-accounting harness, not an actuator-level or 6-DOF autopilot certificate."}


def _region_terrain_surface(config:Layer3Config,region:Mapping[str,Any],*,grid_n:int=90)->dict[str,Any]:
    import rasterio
    from pyproj import Transformer
    from rasterio.windows import from_bounds
    raster=config.map_root/"terrain_obstacles.tif"; boundary=region["boundary"] if "boundary" in region else load_region_definition(config)["boundary"]; ring=(((boundary.get("geometry") or {}).get("coordinates") or [[]])[0])
    with rasterio.open(raster) as ds:
        tr=Transformer.from_crs("EPSG:4326",ds.crs,always_xy=True); projected=[tr.transform(float(lon),float(lat)) for lon,lat in ring]; xs=[p[0] for p in projected]; ys=[p[1] for p in projected]
        win=from_bounds(min(xs),min(ys),max(xs),max(ys),ds.transform).round_offsets().round_lengths().intersection(rasterio.windows.Window(0,0,ds.width,ds.height)); z=ds.read(1,window=win,out_shape=(int(grid_n),int(grid_n)),resampling=rasterio.enums.Resampling.bilinear).astype(float)
        if ds.nodata is not None:z[np.isclose(z,float(ds.nodata))]=np.nan
        left,bottom,right,top=rasterio.windows.bounds(win,ds.transform); x=np.linspace(left,right,int(grid_n)); y=np.linspace(top,bottom,int(grid_n)); xx,yy=np.meshgrid(x,y)
        return {"xx":xx,"yy":yy,"zz":z,"crs":str(ds.crs),"raster":str(raster.resolve()),"ring_projected":projected}


def write_3d_report(config:Layer3Config,run:Mapping[str,Any],rows:list[Mapping[str,Any]],summary:Mapping[str,Any],output:Path,*,terrain_grid_n:int=90)->None:
    import plotly.graph_objects as go
    reg=load_region_definition(config); terrain=_region_terrain_surface(config,reg,grid_n=terrain_grid_n); sx,sy=summary["region"]["start_projected_m"]; x=[sx+float(r.get("east_m") or 0) for r in rows]; y=[sy+float(r.get("north_m") or 0) for r in rows]; z=[float(r.get("altitude_msl_m") or summary["region"]["scenario_start_altitude_msl_m"]) for r in rows]
    fig=go.Figure(); fig.add_trace(go.Surface(x=terrain["xx"],y=terrain["yy"],z=terrain["zz"],opacity=.72,showscale=False,name="terrain_obstacles.tif")); ring=terrain["ring_projected"]; bz=float(np.nanmax(terrain["zz"]))+8; fig.add_trace(go.Scatter3d(x=[p[0] for p in ring],y=[p[1] for p in ring],z=[bz]*len(ring),mode="lines",name="region_v1.geojson boundary"))
    for j,rp in enumerate(((run.get("planner") or {}).get("replans") or [])):
        pts=((rp.get("path") or {}).get("local_enu_m") or []); alts=((rp.get("path") or {}).get("altitude_msl_m") or [])
        if pts:fig.add_trace(go.Scatter3d(x=[sx+p[0] for p in pts],y=[sy+p[1] for p in pts],z=alts,mode="lines",line={"dash":"dot","width":3},name=f"A* plan #{j+1} ({rp.get('reason')})",opacity=.5))
    fig.add_trace(go.Scatter3d(x=x,y=y,z=z,mode="lines+markers",line={"width":6},marker={"size":5,"color":[float(r.get("fast_objective_pct") or 0) for r in rows],"colorscale":"Viridis","colorbar":{"title":"Fast goal %"}},name="A*-guided executed scenario path",customdata=[[r.get("tick"),r.get("distance_to_goal_m"),r.get("terrain_clearance_m"),r.get("obstacle_clearance_m"),r.get("energy_total_j"),r.get("replanned_next")] for r in rows],hovertemplate="tick=%{customdata[0]}<br>goal=%{customdata[1]:.1f}m<br>terrain clr=%{customdata[2]:.1f}m<br>obstacle clr=%{customdata[3]:.1f}m<br>energy=%{customdata[4]:.1f}J<br>replan=%{customdata[5]}<extra>A*</extra>"))
    stride=max(1,len(rows)//10); ids=list(range(0,len(rows),stride)); fig.add_trace(go.Cone(x=[x[i] for i in ids],y=[y[i] for i in ids],z=[z[i] for i in ids],u=[float(rows[i].get("wind_e_m_s") or 0) for i in ids],v=[float(rows[i].get("wind_n_m_s") or 0) for i in ids],w=[float(rows[i].get("wind_u_m_s") or 0) for i in ids],sizemode="absolute",sizeref=12,showscale=False,name="MicroMet actual wind"))
    sp=summary["region"]["start_projected_m"]; dp=summary["region"]["destination_projected_m"]; fig.add_trace(go.Scatter3d(x=[sp[0],dp[0]],y=[sp[1],dp[1]],z=[z[0] if z else 0,z[-1] if z else 0],mode="markers+text",text=["START","DESTINATION"],name="GeoJSON endpoints"))
    fig.update_layout(title=f"Layer 3 A* closed-loop scenario | {summary['runtime_execution']['fast_steps']} Fast / {summary['runtime_execution']['meso_events']} Meso / {summary['runtime_execution']['slow_events']} Slow | replans={summary['planner']['replan_count']}",scene={"xaxis_title":"Easting m","yaxis_title":"Northing m","zaxis_title":"MSL altitude m","aspectmode":"data"},margin={"l":0,"r":0,"b":0,"t":80}); fig.write_html(output,include_plotlyjs=True,full_html=True)


def write_2d_report(rows:list[Mapping[str,Any]],summary:Mapping[str,Any],output:Path)->None:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    ticks=[r.get("tick") for r in rows]; fig=make_subplots(rows=8,cols=1,shared_xaxes=True,vertical_spacing=.035,subplot_titles=("Goal distance / progress","Fast-Meso-Slow objective attainment","Mission energy / time","Terrain-obstacle clearance / cross-track / risk","Center of Consistency / LCI","Twin epistemic weights","Directional Kij couplings","Fast-Meso-Slow / A* replan timeline"))
    fig.add_trace(go.Scatter(x=ticks,y=[r.get("distance_to_goal_m") for r in rows],mode="lines+markers",name="distance to goal m"),row=1,col=1); fig.add_trace(go.Scatter(x=ticks,y=[100*(r.get("goal_progress") or 0) for r in rows],mode="lines",name="Wing goal progress %",yaxis="y"),row=1,col=1)
    for key,name in [("fast_objective_pct","Fast goal %"),("meso_objective_pct","Meso goal %"),("slow_objective_pct","Slow goal %")]:fig.add_trace(go.Scatter(x=ticks,y=[r.get(key) for r in rows],mode="lines+markers",name=name),row=2,col=1)
    fig.add_trace(go.Scatter(x=ticks,y=[r.get("energy_total_j") for r in rows],mode="lines",name="energy J"),row=3,col=1); fig.add_trace(go.Scatter(x=ticks,y=[r.get("flight_time_s") for r in rows],mode="lines",name="flight time s"),row=3,col=1)
    for key,name in [("terrain_clearance_m","terrain clearance m"),("obstacle_clearance_m","obstacle clearance m"),("cross_track_error_m","cross-track m"),("risk","Layer3 risk")]:fig.add_trace(go.Scatter(x=ticks,y=[r.get(key) for r in rows],mode="lines",name=name),row=4,col=1)
    fig.add_trace(go.Scatter(x=ticks,y=[r.get("mismatch") for r in rows],mode="lines",name="CoC mismatch"),row=5,col=1); fig.add_trace(go.Scatter(x=ticks,y=[r.get("lci") for r in rows],mode="lines",name="LCI"),row=5,col=1)
    for k in sorted({k for r in rows for k in r if str(k).startswith("weight::")}):fig.add_trace(go.Scatter(x=ticks,y=[r.get(k) for r in rows],mode="lines",name=k.split("::",1)[1]),row=6,col=1)
    for k in sorted({k for r in rows for k in r if str(k).startswith("coupling::")}):fig.add_trace(go.Scatter(x=ticks,y=[r.get(k) for r in rows],mode="lines",name=k.split("::",1)[1]),row=7,col=1)
    fig.add_trace(go.Scatter(x=ticks,y=[0]*len(rows),mode="markers",name="Fast"),row=8,col=1); fig.add_trace(go.Scatter(x=[r["tick"] for r in rows if r.get("meso_event")],y=[1]*sum(1 for r in rows if r.get("meso_event")),mode="markers",marker={"symbol":"triangle-up","size":11},name="Meso"),row=8,col=1); fig.add_trace(go.Scatter(x=[r["tick"] for r in rows if r.get("slow_event")],y=[2]*sum(1 for r in rows if r.get("slow_event")),mode="markers",marker={"symbol":"diamond","size":12},name="Slow"),row=8,col=1); fig.add_trace(go.Scatter(x=[r["tick"] for r in rows if r.get("replanned_next")],y=[3]*sum(1 for r in rows if r.get("replanned_next")),mode="markers",marker={"symbol":"x","size":10},name="A* replan"),row=8,col=1)
    fig.update_yaxes(title_text="m / %",row=1,col=1); fig.update_yaxes(title_text="achievement %",row=2,col=1); fig.update_yaxes(title_text="J / s",row=3,col=1); fig.update_yaxes(title_text="safety",row=4,col=1); fig.update_yaxes(title_text="network",row=5,col=1); fig.update_yaxes(title_text="weight",row=6,col=1); fig.update_yaxes(title_text="K",row=7,col=1); fig.update_yaxes(tickvals=[0,1,2,3],ticktext=["Fast","Meso","Slow","A* replan"],row=8,col=1); fig.update_xaxes(title_text="Fast tick",row=8,col=1)
    fig.update_layout(height=1800,title=f"Layer 3 A* objective/loop report | Fast:Meso={summary['runtime_execution'].get('configured_fast_per_meso')} | objective Fast/Meso/Slow={summary['objective_attainment']['fast_final_pct']}/{summary['objective_attainment']['meso_final_pct']}/{summary['objective_attainment']['slow_final_pct']}%",hovermode="x unified",legend={"orientation":"h"}); fig.write_html(output,include_plotlyjs=True,full_html=True)


def write_timeseries(rows:list[Mapping[str,Any]],output:Path)->None:
    keys=sorted({str(k) for r in rows for k in r});
    with output.open("w",newline="",encoding="utf-8") as f:w=csv.DictWriter(f,fieldnames=keys);w.writeheader();w.writerows(rows)


def generate_scenario_reports(config:Layer3Config,request:Mapping[str,Any],*,steps:int|None=None,output_dir:str|Path="reports/scenario",terrain_grid_n:int=90)->tuple[dict[str,Any],ScenarioArtifacts]:
    out=Path(output_dir);out.mkdir(parents=True,exist_ok=True);run=run_layer3_scenario(config,request,steps=steps);rows=extract_timeseries(run);summary=build_summary(run,rows);summary["runtime_execution"]["configured_fast_per_meso"]=config.time.meso_fast_steps;summary["runtime_execution"]["configured_meso_per_slow"]=config.time.slow_meso_windows
    art=ScenarioArtifacts(out,out/"scenario_result.json",out/"scenario_summary.json",out/"scenario_timeseries.csv",out/"layer3_scenario_3d.html",out/"layer3_scenario_2d.html");write_json(art.result_json,run);write_json(art.summary_json,summary);write_timeseries(rows,art.timeseries_csv);write_3d_report(config,run,rows,summary,art.report_3d_html,terrain_grid_n=terrain_grid_n);write_2d_report(rows,summary,art.report_2d_html);return summary,art
