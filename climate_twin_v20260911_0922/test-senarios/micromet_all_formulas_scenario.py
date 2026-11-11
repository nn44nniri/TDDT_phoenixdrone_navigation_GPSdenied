#!/usr/bin/env python3
"""Independent future-facing MicroMetTwin scenario.

Edit ``TWIN_INPUT`` or import ``run_twin(config)`` from another Python program.
The function returns plain Python dictionaries containing the user configuration,
all effective FMI inputs, all FMU outputs, semantic metadata, the Dynamic N-Twin
Layer-2 packet, and an implemented-formula coverage declaration.

The scenario intentionally enables the complete implemented MicroMet path:
Barnes two-pass analysis, temperature/dew-point/RH, terrain wind correction,
cloud/solar/longwave, pressure/precipitation, optional calibration, and the full
MicroMet-3DW diagnostic path (temporal interpolation, vertical fallback/profile,
vertical wind, gradients, temporal/material derivatives and continuity residual).
"""
from __future__ import annotations
import json, math, sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from climate_io import read_forecast, sources_at_time, build_fmi_inputs
from estimate_climate import _endpoint_wind_inputs, _sparse_profile_inputs
from fmu_client import MicroMetFMU
from fmi_semantics import annotate_values
from tddt_layer2 import ClimateDomainTwin

TWIN_INPUT={
  'files':{
    'fmu':'fmu/MicroMetTwin.fmu',
    'forecast':'climate_dataset/openmeteo_24h.csv'
  },
  'request':{
    'time':'2026-08-14T17:30:00',
    'latitude':40.017498782173554,
    'longitude':-105.28999780562658,
    'altitude_agl_m':100.0,
    'ground_elevation_m':1668.0
  },
  'terrain':{
    'slope_deg':12.5,
    'aspect_deg':225.0,
    'curvature_scaled':0.015,
    'dzdx':None,
    'dzdy':None,
    'curvature_radius_m':1000.0,
    'curvature_min':-0.5,
    'curvature_max':0.5,
    'use_dem_curvature':0.0
  },
  'micromet_parameters':{
    'barnes_filter_km':20.0,
    'barnes_second_pass_enabled':1.0,
    'barnes_second_pass_radius_factor':0.5,
    'lapse_rate':0.0065,
    'vapor_coefficient':0.00040,
    'gamma_s':0.58,
    'gamma_c':0.42,
    'precipitation_chi_km':0.25,
    'rh700':-1.0
  },
  'calibration':{
    'enabled':1.0,
    'temperature_bias_c':0.3613073477,
    'relative_humidity_bias_pct':-0.1191216869,
    'wind_speed_scale':1.11095,
    'wind_direction_bias_deg':7.522612264,
    'shortwave_scale':1.69731,
    'pressure_bias_pa':0.0,
    'shortwave_reference_blend':0.75
  },
  'wind_3dw':{
    'enabled':1.0,
    'fd_dx_m':100.0,
    'fd_dy_m':100.0,
    'fd_dz_m':10.0,
    'wind_profile_power':0.143
  },
  'tddt_layer2':{
    'context_id':'CRP_future_demo',
    'stage_id':'cruise',
    'operation_code':'climate_all_formulas_scenario'
  }
}

FORMULA_COVERAGE={
  'MicroMet_core':'equations 1-33 in micromet_formalism_v2.md',
  'MicroMet_operational_extension':'equations 34-43 in micromet_formalism_v2.md (43 = derived moist-air density)',
  'MicroMet_3DW_extension':'3DW equations 1-26 implemented by the FMU extension',
  'branches_enabled':['Barnes first+second pass','dewpoint/RH','terrain slope/aspect/curvature','terrain wind speed+direction','cloud','solar geometry','shortwave','longwave','forecast-anchored pressure','precipitation altitude correction','raw outputs','calibration','derived moist-air density','3DW temporal interpolation','3DW vertical profile/fallback','diagnostic vertical wind','velocity gradient','temporal derivative','material derivative','continuity residual']
}

def _path(p):
    p=Path(p); return p if p.is_absolute() else ROOT/p

def _terrain_gradients(slope_deg,aspect_deg):
    slope=math.radians(float(slope_deg)); aspect=math.radians(float(aspect_deg))
    g=math.tan(slope); angle=1.5*math.pi-aspect
    return g*math.cos(angle),g*math.sin(angle)

def run_twin(config:dict)->dict:
    cfg=deepcopy(config); req=cfg['request']; ter=cfg['terrain']; mp=cfg['micromet_parameters']; cal=cfg['calibration']; w3=cfg['wind_3dw']
    fmu_path=_path(cfg['files']['fmu']); forecast_path=_path(cfg['files']['forecast'])
    requested=datetime.fromisoformat(req['time']); rows=read_forecast(forecast_path); sources=sources_at_time(rows,requested)
    altitude_msl=float(req['ground_elevation_m'])+float(req['altitude_agl_m'])
    inputs=build_fmi_inputs(sources,latitude=float(req['latitude']),longitude=float(req['longitude']),elevation_m=altitude_msl,requested_time=requested,
        slope_deg=float(ter['slope_deg']),aspect_deg=float(ter['aspect_deg']),curvature=float(ter['curvature_scaled']),barnes_filter_km=float(mp['barnes_filter_km']),
        lapse_rate_c_per_m=float(mp['lapse_rate']),vapor_coefficient_per_m=float(mp['vapor_coefficient']),gamma_s=float(mp['gamma_s']),gamma_c=float(mp['gamma_c']),precipitation_chi_km=float(mp['precipitation_chi_km']))
    dzdx,dzdy=_terrain_gradients(ter['slope_deg'],ter['aspect_deg'])
    if ter.get('dzdx') is not None: dzdx=float(ter['dzdx'])
    if ter.get('dzdy') is not None: dzdy=float(ter['dzdy'])
    inputs.update({'terrain_dzdx':dzdx,'terrain_dzdy':dzdy,'derive_slope_aspect_from_gradients':1.0,
      'barnes_second_pass_enabled':float(mp['barnes_second_pass_enabled']),'barnes_second_pass_radius_factor':float(mp['barnes_second_pass_radius_factor']),
      'rh700':float(mp['rh700']),'curvature_radius_m':float(ter['curvature_radius_m']),'curvature_min':float(ter['curvature_min']),'curvature_max':float(ter['curvature_max']),'use_dem_curvature':float(ter['use_dem_curvature']),
      'calibration_enabled':float(cal['enabled']),'temperature_bias_c':float(cal['temperature_bias_c']),'relative_humidity_bias_pct':float(cal['relative_humidity_bias_pct']),
      'wind_speed_scale':float(cal['wind_speed_scale']),'wind_direction_bias_deg':float(cal['wind_direction_bias_deg']),'shortwave_scale':float(cal['shortwave_scale']),'pressure_bias_pa':float(cal['pressure_bias_pa']),'shortwave_reference_blend':float(cal['shortwave_reference_blend']),
      'wind_3dw_enabled':float(w3['enabled']),'altitude_agl_m':float(req['altitude_agl_m']),'fd_dx_m':float(w3['fd_dx_m']),'fd_dy_m':float(w3['fd_dy_m']),'fd_dz_m':float(w3['fd_dz_m']),'wind_profile_power':float(w3['wind_profile_power'])})
    with MicroMetFMU(fmu_path) as fmu:
        inputs.update(_endpoint_wind_inputs(fmu,rows,requested,latitude=float(req['latitude']),longitude=float(req['longitude']),altitude_msl_m=altitude_msl,slope_deg=float(ter['slope_deg']),aspect_deg=float(ter['aspect_deg']),curvature=float(ter['curvature_scaled']),barnes_filter_km=float(mp['barnes_filter_km'])))
        inputs.update(_sparse_profile_inputs(fmu,rows,requested,latitude=float(req['latitude']),longitude=float(req['longitude']),altitude_msl_m=altitude_msl,slope_deg=float(ter['slope_deg']),aspect_deg=float(ter['aspect_deg']),curvature=float(ter['curvature_scaled']),barnes_filter_km=float(mp['barnes_filter_km'])))
        outputs=fmu.step(inputs)
    base={'request':{'time':requested.isoformat(),'latitude':float(req['latitude']),'longitude':float(req['longitude']),'altitude_agl_m':float(req['altitude_agl_m']),'altitude_msl_m':altitude_msl,'ground_elevation_m':float(req['ground_elevation_m']),'terrain_source':'scenario-explicit'},'source_count':len(sources),'fmu_outputs':outputs}
    packet=ClimateDomainTwin().packet_from_estimate(base,**cfg['tddt_layer2'])
    semantic=annotate_values(outputs,requested.isoformat(),formula_valid=outputs.get('formula_valid'),wind_3dw_valid=outputs.get('wind_3dw_valid'))
    return {'ok':bool(outputs.get('formula_valid',0)>=0.5),'configuration':cfg,'effective_fmi_inputs':inputs,'outputs':outputs,'semantic_outputs':semantic,'tddt_layer2_packet':packet,'formula_coverage':FORMULA_COVERAGE,'all_outputs_finite':all(math.isfinite(float(v)) for v in outputs.values())}

def main():
    result=run_twin(TWIN_INPUT)
    print(json.dumps(result,indent=2,ensure_ascii=False,allow_nan=False))
if __name__=='__main__':main()
