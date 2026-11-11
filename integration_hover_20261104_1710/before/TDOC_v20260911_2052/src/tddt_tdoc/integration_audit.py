"""Read-only site audit joining independent Stage-8, 9 and 10 evidence.

It verifies cross-package identity, not simulator, native controller, or flight
certification. Does not import external packages, start SITL, or mutate KStore.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

SCHEMA="TDDT-TDOC-CrossLayerSiteAudit-v1"
ROOTS={
    "TDDT_TWINS_ROOT":"twins_layer2_v20260820_1233",
    "TDDT_CLIMATE_ROOT":"climate_twin_v20260911_0922",
    "TDDT_UNIT_ADAPTER_ROOT":"TDOC_ISO_Universal_Unit_Conversion_Standard20260824_0914",
    "TDDT_BOULDER_ROOT":"boulder_multilayer_map_builder_v20260814_1215",
    "TDDT_SARG_PACKAGE_ROOT":"SARG_v20260821_0945",
    "TDDT_LAYER3_ROOT":"layer3_v20260911_1131",
    "TDDT_LAYER4_ROOT":"layer4_v20260907_0608",
    "TDDT_LAYER5_ROOT":"layer5_v20260911_1510",
    "TDDT_LAYER6_ROOT":"layer6_v20261007_1032",
    "TDDT_LAYER7_ROOT":"layer7_v20260901_0921",
}


def _sha(p: Path) -> str:
    h=hashlib.sha256()
    with p.open('rb') as f:
        while True:
            data=f.read(1024*1024)
            if not data: break
            h.update(data)
    return h.hexdigest()


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(folder.resolve(strict=True))
        return True
    except (ValueError,OSError,RuntimeError):
        return False


def _json(file: Path) -> dict[str,Any]:
    v=json.loads(file.read_text(encoding='utf-8'))
    if not isinstance(v,dict): raise ValueError('report is not an object')
    return v


def audit(root: str|Path, *, stage8_report: str|Path|None,
          stage9_report: str|Path|None, stage10_report: str|Path|None,
          environ:dict[str,str]|None=None, require_native:bool=False) -> dict[str,Any]:
    root=Path(root).expanduser().resolve()
    env=environ if environ is not None else os.environ
    blockers=[]
    checks={}
    def check(key:str, good:bool, detail:str=""):
        checks[key]={"ok":bool(good),"detail":str(detail)}
        if not good: blockers.append(key+(":"+str(detail) if detail else ""))

    def verified_file(key:str,p:Path,expected:Any,allow_root:Path):
        if not isinstance(expected,str) or len(expected)!=64 or any(c not in '0123456789abcdef' for c in expected):
            check(key,False,'invalid_expected_sha256');return
        if not p.is_file() or not _inside(p,allow_root):
            check(key,False,'absent_or_outside_authoritative_root');return
        check(key,_sha(p)==expected,'sha256_match' if _sha(p)==expected else 'sha256_mismatch')

    check('tdoc_folder',(root/'TDOC_v20260911_2052'/'src'/'tddt_tdoc').is_dir())
    for env_key,folder in ROOTS.items():
        p=root/folder
        check('source_root_'+env_key,p.is_dir(),folder)
        if env.get(env_key):
            check('address_authority_'+env_key,Path(env[env_key]).resolve()==p.resolve(),folder)
    check('retired_fmu_not_active',not env.get('EXT_FMU_SIM_ROOT'))
    check('ptlib_not_active',not env.get('TDDT_PTLIB_ROOT'))
    repair_markers={
        'stage5_native_pilot_boundary':root/ROOTS['TDDT_LAYER5_ROOT']/'src/layer5/native_pilot_boundary.py',
        'stage6_native_pair':root/ROOTS['TDDT_LAYER7_ROOT']/'src/tddt_layer7/native_pair.py',
        'stage7_iso_cross_layer':root/ROOTS['TDDT_UNIT_ADAPTER_ROOT']/'src/tdoc_unit_converter/boundary.py',
        'stage8_forcing_binding':root/ROOTS['TDDT_CLIMATE_ROOT']/'scripts/validate_stage8_binding.py',
        'stage9_spatial_boundary':root/ROOTS['TDDT_BOULDER_ROOT']/'scripts/validate_stage9_site_map.py',
        'stage10_reference_integrity':root/ROOTS['TDDT_SARG_PACKAGE_ROOT']/'scripts/validate_stage10_site_sarg.py',
    }
    for key,path in repair_markers.items():
        check('repair_present_'+key,path.is_file() and _inside(path,root),'patched_source_required')

    reports={}
    for key,path in [('stage8',stage8_report),('stage9',stage9_report),('stage10',stage10_report)]:
        if not path:
            check(key+'_report',False,'missing_site_report');continue
        try:
            rp=Path(path).expanduser().resolve(strict=True)
            if not _inside(rp,root/'reports'):
                check(key+'_report',False,'outside_site_reports');continue
            reports[key]=_json(rp)
            check(key+'_report',True,'loaded_site_report')
        except (OSError,ValueError,json.JSONDecodeError) as exc:
            check(key+'_report',False,type(exc).__name__)
    s8=reports.get('stage8') or {};s9=reports.get('stage9') or {};s10=reports.get('stage10') or {}
    check('stage8_scope',s8.get('status')=='FORCING_EPOCH_BOUND_DIAGNOSTIC_ONLY' and s8.get('schema')=='TDDT-Climate-ForcingBinding-v1')
    check('stage9_scope',s9.get('status')=='MAP_CRP_SPATIAL_DIAGNOSTIC_PASS_UNCERTIFIED' and s9.get('schema')=='TDDT-Boulder-SpatialBinding-v1')
    check('stage10_scope',s10.get('status')=='SARG_REFERENCE_CONTRACT_DIAGNOSTIC_PASS_UNCERTIFIED' and s10.get('schema')=='TDDT-SARG-ReferenceBoundary-v1')

    if s8:
        forcing=root/'internet_datasets/climate_twin/openmeteo_24h.csv'
        climate_fmu=root/ROOTS['TDDT_CLIMATE_ROOT']/'fmu/MicroMetTwin.fmu'
        check('stage8_forcing_path',Path(s8.get('forcing_file') or '/').resolve()==forcing.resolve())
        check('stage8_fmu_path',Path(s8.get('fmu_file') or '/').resolve()==climate_fmu.resolve())
        verified_file('stage8_forcing_sha256',forcing,s8.get('forcing_sha256'),root/'internet_datasets/climate_twin')
        verified_file('stage8_fmu_sha256',climate_fmu,s8.get('fmu_sha256'),root/ROOTS['TDDT_CLIMATE_ROOT'])
        check('stage8_epoch_mapping',s8.get('mission_epoch_utc')==s8.get('mapped_forecast_time_utc') if s8.get('plant_elapsed_s')==0 else bool(s8.get('mapped_forecast_time_utc')))
        check('stage8_window',bool(s8.get('common_start_utc')) and bool(s8.get('common_end_utc')) and s8['common_start_utc']<=s8['mapped_forecast_time_utc']<=s8['common_end_utc'])
        check('stage8_wind_contract',s8.get('wind_frame')=='NED' and s8.get('wind_bearing_convention')=='METEOROLOGICAL_FROM_DEG_CLOCKWISE_FROM_NORTH')
        check('stage8_unauthenticated_native_explicit',s8.get('native_environment_confirmed') is False and s8.get('temperature_applied_in_jsbsim') is False and s8.get('humidity_applied_in_jsbsim') is False)

    if s9:
        b=root/ROOTS['TDDT_BOULDER_ROOT'];dataset=b/'outputs/region_v1_map'; region=b/'region/region_v1.geojson'
        check('stage9_region_path',Path(s9.get('region_file') or '/').resolve()==region.resolve())
        verified_file('stage9_region_sha256',region,s9.get('region_sha256'),b)
        check('stage9_crs',s9.get('crs')=='EPSG:32613')
        raster_files=s9.get('raster_files') or {}
        required=['terrain_obstacles.tif','climate_contexts.tif','magnetic_anomaly.tif','CRP/crp_cluster_map.tif']
        check('stage9_raster_set',all(k in raster_files for k in required))
        for rel in required:
            r=raster_files.get(rel) or {}
            verified_file('stage9_raster_'+rel,dataset/rel,r.get('sha256'),dataset)
            check('stage9_grid_'+rel,r.get('crs')=='EPSG:32613' and r.get('dimensions')==s9.get('grid_width_height') and isinstance(r.get('affine'),list) and len(r['affine'])==9)
        check('stage9_crp_count',(s9.get('crp') or {}).get('cluster_count')==32)
        check('stage9_native_agl_not_inferred',s9.get('native_agl_verified') is False and s9.get('vertical_datum_authenticated') is False)
        for name,point in (s9.get('points') or {}).items():
            check('stage9_point_'+name,isinstance(point.get('terrain_elevation_m'),(int,float)) and point.get('terrain_vertical_datum_verified') is False)

    if s10:
        base=root/ROOTS['TDDT_SARG_PACKAGE_ROOT'];sargdir=base/'generated/SARG'
        check('stage10_dataset_path',Path(s10.get('dataset_dir') or '/').resolve()==sargdir.resolve())
        for rel,sha in (s10.get('dataset_files_sha256') or {}).items():
            # Region may be copied into the generated dataset; no path escape.
            if '/' in rel or '\\' in rel or rel.startswith('.'):
                check('stage10_file_'+rel,False,'unsafe_name');continue
            verified_file('stage10_file_'+rel,sargdir/rel,sha,sargdir)
        check('stage10_complete_index',s10.get('record_count')==1920 and s10.get('pattern_count')==1920 and s10.get('index_validated') is True and s10.get('phase_coverage_exact') is True)
        check('stage10_grid_cardinality',(s10.get('regime_count'),s10.get('phase_count'),s10.get('crp_count'),s10.get('crp_catalog_count'))==(10,6,32,32))
        check('stage10_source_binding_done',s10.get('source_fmu_hash_binding_checked') is True)
        fmumap={'mesa_nav_fmu':'fmus/MESA_NAV.fmu','wingmpet_fmu':'fmus/WingMPET.fmu'}
        hashes=s10.get('source_fmu_hash_checks') or {}
        for key,rel in fmumap.items():
            meta=hashes.get(key) or {}
            check('stage10_source_'+key+'_frozen',meta.get('verified_against_frozen_sarg_metadata') is True)
            verified_file('stage10_source_'+key+'_sha256',root/ROOTS['TDDT_TWINS_ROOT']/rel,meta.get('sha256'),root/ROOTS['TDDT_TWINS_ROOT'])
        check('stage10_drone_config_frozen',(hashes.get('drone_config') or {}).get('verified_against_frozen_sarg_metadata') is True)
        check('stage10_no_mutation',s10.get('data_mutated') is False and s10.get('sarg_rebuilt') is False)
        check('stage10_reference_not_navigation_truth',s10.get('reference_is_current_navigation_state') is False)
        for i,item in enumerate(s10.get('status_samples') or []):
            if not isinstance(item,dict):
                check('stage10_status_sample_'+str(i),False,'invalid_sample')
            else:
                check('stage10_status_sample_'+str(i),item.get('reference_admissible') is not True or item.get('status')==0,'nonzero_status_must_not_be_admissible')

    if s9 and s10:
        check('stage9_stage10_region_binding',s9.get('region_sha256')==s10.get('region_file_sha256'))
        check('stage9_stage10_crp_binding',(s9.get('crp') or {}).get('cluster_count')==s10.get('crp_count'))
    for tag,r in reports.items():
        check(tag+'_not_flight_authority',r.get('flight_authorized') is False and r.get('native_controller_certified') is False)
    diagnostic_ok=not blockers
    status='CROSS_LAYER_REFERENCE_BINDING_PASS_UNCERTIFIED' if diagnostic_ok else 'BLOCKED_CROSS_LAYER_CONTRACT'
    if diagnostic_ok and require_native:
        status='BLOCKED_NATIVE_CONTROLLER_AND_ENVIRONMENT_CERTIFICATION_UNAVAILABLE'
    return {"schema":SCHEMA,"test":"STAGE11_TDOC_INTEGRATION_READ_ONLY_AUDIT","status":status,
            "diagnostic_binding_comparable":diagnostic_ok,"blockers":blockers,"checks":checks,
            "stage_reports_loaded":sorted(reports),"invariants":{
                "pilot_reference_is_not_motor_command":True,
                "pwm_is_not_measured_rpm":True,
                "sarg_is_not_current_navigation_state":True,
                "native_environment_confirmation_not_inferred":True,
                "layer6_cadence_is_not_tdoc_decision_cadence":True},
            "native_controller_certified":False,"native_environment_confirmed":False,
            "native_agl_verified":False,"dynamic_validation_certified":False,
            "operational_commit_allowed":False,"flight_authorized":False,
            "scope":"SOURCE_AND_FROZEN_REFERENCE_IDENTITY_ONLY_NO_NATIVE_FLIGHT_AUTHORITY"}


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root',required=True,help='TDDT integrated root')
    for k in ('stage8','stage9','stage10'): ap.add_argument('--'+k+'-report',required=True)
    ap.add_argument('--require-native',action='store_true')
    ap.add_argument('--output')
    args=ap.parse_args(argv)
    result=audit(args.root,stage8_report=args.stage8_report,stage9_report=args.stage9_report,
                 stage10_report=args.stage10_report,require_native=args.require_native)
    output=json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)
    if args.output:
        p=Path(args.output);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(output+'\n',encoding='utf-8')
    print(output)
    return 0 if result['status']=='CROSS_LAYER_REFERENCE_BINDING_PASS_UNCERTIFIED' else 2

if __name__=='__main__':raise SystemExit(main())
