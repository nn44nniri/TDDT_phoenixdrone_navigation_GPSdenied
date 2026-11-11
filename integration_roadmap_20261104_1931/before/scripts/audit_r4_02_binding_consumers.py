#!/usr/bin/env python3
"""R4.02 production CLI/launcher-plan audit, without starting GUI or FCU."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.native_environment import prepare_binding
from tddt_tdoc.native_binding import verify_binding_inputs


def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-dir',required=True,type=Path)
    args=parser.parse_args();tdoc=Path(__file__).resolve().parents[1];root=tdoc.parent
    out=args.output_dir.resolve();out.relative_to(tdoc/'reports/online');out.mkdir(exist_ok=False)
    cfg=TDOCConfig.from_yaml(tdoc/'configs/tdoc.yaml');native=cfg.path_value('TDDT_LAYER6_ROOT',required=True)
    spec=importlib.util.spec_from_file_location('protect402',tdoc/'scripts/freeze_r1_02_state.py')
    protection=importlib.util.module_from_spec(spec);spec.loader.exec_module(protection)
    baseline=root/'recovery_baselines/R3_05_20261029_fixture_retirement/PROTECTED_MANIFEST.json'
    before=protection.verify_manifest(baseline);assert before['status']=='PASS';write(out/'PROTECTION_BEFORE.json',before)
    active=native/'runtime/tdoc_environment_binding.json';active_sha=sha(active) if active.is_file() else None
    active_state=native/'runtime/bound_environment_state.json';state_sha=sha(active_state) if active_state.is_file() else None
    binding_path=out/'BINDING.json';binding=prepare_binding(cfg,output_path=binding_path)
    verify_binding_inputs(binding,cfg)
    sys.path.insert(0,str(native/'src'))
    import run_flightstack
    from layer6.binding import environment_status
    commands=run_flightstack._commands(native/'FlightStack',binding_path)
    assert len(commands)==3 and all(str(binding_path) in c for c in commands)
    assert not any(x in ''.join(commands) for x in ('2025-05-20','--dataset-tar','--time ','examples/region'))
    write(out/'LAUNCH_COMMANDS_NOT_EXECUTED.json',commands)
    envroot=native/'FlightStack/phoenix_environment_adapter_v2'
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONPATH=os.pathsep.join(
        [str(envroot),str(native/'src'),str(tdoc/'src'),os.environ.get('PYTHONPATH','')]))
    def call(arguments,label):
        argv=[sys.executable,'-B','-m','phoenix_env_adapter.cli',*arguments]
        r=subprocess.run(argv,cwd=envroot,env=env,text=True,capture_output=True,timeout=60)
        (out/(label+'.log')).write_text(r.stdout+r.stderr)
        assert r.returncode==0,(label,r.stderr)
        state=json.loads(r.stdout);assert state['binding_id']==binding['binding_id'] and state['binding_hash']==binding['binding_hash']
        assert state['binding_consumed'] and not state['readback_verified'] and not state['ok']
        return state
    prepared=call(['prepare','--binding',str(binding_path),'--output',str(out/'prepared')],'REAL_BOUND_PREPARE_CLI')
    states=[]
    for sec in (0.,900.,3600.):
        statefile=out/('state_'+str(int(sec))+'.json')
        state=call(['runtime','--binding',str(binding_path),'--state-file',str(statefile),
            '--mavlink','unused-no-socket-in-dry-run','--dry-run','--plant-time-s',str(sec)],'REAL_BOUND_RUNTIME_'+str(int(sec)))
        assert state['timezone_status']=='unspecified' and not state['UTC_mapping_verified']
        assert not state['simulation_time_dataset'].endswith('Z')
        assert state['forcing_path']==binding['forcing_file'] and not state['legacy_dataset_used']
        states.append(state)
    observed=environment_status(binding_path,out/'state_3600.json')
    assert observed['binding_consumed'] and not observed['ok'];write(out/'GUI_CONSUMPTION_PROJECTION.json',observed)
    from terrain_truth import TerrainTruth
    home=binding['mission_origin']
    with TerrainTruth(binding['origin']['terrain_file']['path']) as terrain:
        x,y=terrain._xy(home['longitude_deg'],home['latitude_deg'])
        old=terrain._read_nearest(terrain.elevation_band,x,y)
        ground=terrain.sample(home['longitude_deg'],home['latitude_deg']).ground_elevation_m
    assert old>home['altitude_msl_m'] and abs(ground-home['terrain_elevation_msl_m'])<1e-6
    assert abs(prepared['environment_query']['altitude_agl_m']-home['cg_height_agl_m'])<1e-6
    write(out/'TERRAIN_DEPENDENCY_REPAIR.json',{'same_dem':binding['origin']['terrain_file'],
        'old_nearest_ground_msl_m':old,'old_computed_CG_AGL_m':home['altitude_msl_m']-old,
        'bilinear_ground_msl_m':ground,'tdoc_bound_ground_msl_m':home['terrain_elevation_msl_m'],
        'unchanged_aircraft_CG_MSL_m':home['altitude_msl_m'],'current_CG_AGL_m':prepared['environment_query']['altitude_agl_m'],
        'ground_or_aircraft_clamped':False,'FMU_binary_rebuilt':False})
    after=protection.verify_manifest(baseline);assert after['status']=='PASS';write(out/'PROTECTION_AFTER.json',after)
    assert (sha(active) if active.is_file() else None)==active_sha
    assert (sha(active_state) if active_state.is_file() else None)==state_sha
    result={'step':'R4.02','status':'PASS','binding_id':binding['binding_id'],'binding_hash':binding['binding_hash'],
        'model_id':binding['model_id'],'model_artifacts':len(binding['model_dependencies']['artifacts']),
        'handoff_hash':binding['handoff_hash'],'forcing_file_sha256':binding['forcing_file_sha256'],
        'time_queries':[s['simulation_time_dataset'] for s in states],'timezone_status':'unspecified',
        'clock_domain':binding['time_contract']['clock_domain'],'UTC_conversion_performed':False,
        'actual_MicroMet_formula_valid':[s['model_outputs']['formula_valid'] for s in states],
        'actual_MicroMet_wind_valid':[s['model_outputs']['wind_3dw_valid'] for s in states],
        'source_time_basis':'explicit isolated queries of the production runtime path; not observed flight',
        'launcher_commands_built_but_not_executed':True,'canonical_CLI_uses_binding_only':True,
        'terrain_consumer_aligned_with_accepted_origin':True,'protected_baseline_unchanged':True,
        'active_binding_changed':False,'native_stack_started':False,'MAVLink_socket_opened':False,
        'ARM_sent':False,'parameter_write_sent':False,'forcing_refreshed':False,'offline_training_started':False,
        'forcing_application_verified':False,'runtime_clock_synchronization_verified':False,'flight_ready':False,'next_microstep':'R4.03'}
    write(out/'BINDING_CONSUMPTION_RESULT.json',result);print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
