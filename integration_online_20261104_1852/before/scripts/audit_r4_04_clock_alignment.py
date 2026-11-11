#!/usr/bin/env python3
"""Audit real native clock correlations under a non-UTC host timezone, disarmed."""
import argparse
from contextlib import redirect_stdout
import csv
from datetime import datetime,timedelta
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time


def sha(raw):return hashlib.sha256(raw).hexdigest()
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')


def accepted_reference(tdoc):
    path=tdoc/'runtime/knowledge/handoff/offline_latest.json.gz'
    raw=path.read_bytes();handoff=json.loads(gzip.decompress(raw));c=handoff['reuse_contract']['climate_time_alignment']
    forcing=Path(c['forcing_file']).read_bytes();rows=list(csv.DictReader(forcing.decode().splitlines()))
    return {'handoff_file_sha256':sha(raw),'handoff_hash':handoff['handoff_hash'],
        'forcing_file_sha256':sha(forcing),'epoch_exact':c['effective_climate_epoch'],
        'timestamps':[{'point':r['point'],'time':r['time']} for r in rows]}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',required=True,type=Path)
    a=p.parse_args();tdoc=Path(__file__).resolve().parents[1];root=tdoc.parent
    out=a.output_dir.resolve();out.relative_to(tdoc/'reports/online');out.mkdir(exist_ok=False)
    native=root/'layer6_v20261007_1032';sys.path[:0]=[str(tdoc/'src'),str(native/'src')]
    from layer6.timebase import validate_clock_mapping,_hash
    from layer6.binding import validate_wind_application
    from tddt_tdoc.native_binding import read_binding,verify_binding_inputs,digest
    from tddt_tdoc.config import TDOCConfig
    before=accepted_reference(tdoc);write(out/'ACCEPTED_TIME_REFERENCE_BEFORE.json',before)
    old_tz=os.environ.get('TZ');old_argv=sys.argv[:]
    spec=importlib.util.spec_from_file_location('real_native_forcing_dependency',tdoc/'scripts/audit_r4_03_native_forcing.py')
    dependency=importlib.util.module_from_spec(spec);spec.loader.exec_module(dependency)
    try:
        os.environ['TZ']='Pacific/Honolulu';time.tzset()
        sys.argv=[str(tdoc/'scripts/audit_r4_03_native_forcing.py'),'--output-dir',str(out/'native_run')]
        with (out/'BOUND_RUNTIME.log').open('w') as stream,redirect_stdout(stream):dependency.main()
    finally:
        sys.argv=old_argv
        if old_tz is None:os.environ.pop('TZ',None)
        else:os.environ['TZ']=old_tz
        time.tzset()
    binding=read_binding(out/'native_run/BINDING.json');verify_binding_inputs(binding,TDOCConfig.from_yaml(tdoc/'configs/tdoc.yaml'))
    states=[]
    for line in (out/'BOUND_RUNTIME.log').read_text().splitlines():
        try:value=json.loads(line)
        except json.JSONDecodeError:continue
        if isinstance(value,dict) and value.get('schema')=='Layer6-bound-environment-consumption-v1':states.append(value)
    assert len(states)==3
    observations=[]
    for state in states:
        # Validate historical evidence at its recorded publication instant, not as fresh now.
        validate_clock_mapping(binding,state,now=state['updated_monotonic'])
        validate_wind_application(state,now=state['updated_monotonic'])
        event=state['clock_mapping']['observation']
        assert event['raw_time_boot_ms']/1000.==state['plant_time_s']
        elapsed=state['plant_time_s']-binding['plant_epoch_s']
        expected=(datetime.fromisoformat(before['epoch_exact'])+timedelta(seconds=elapsed)).isoformat()
        assert state['simulation_time_dataset']==state['environment_query']['time']==expected
        assert state['mission_elapsed_s']==elapsed and state['runtime_clock_mapping_verified']
        assert not state['runtime_clock_synchronization_verified'] and not state['UTC_mapping_verified']
        assert state['timezone_status']=='unspecified' and not expected.endswith('Z')
        assert datetime.fromisoformat(expected).tzinfo is None
        observations.append({'raw_source':event,'mapping_hash':state['clock_mapping']['mapping_hash'],
            'dataset_query':expected,'sampled_monotonic':state['sampled_monotonic'],
            'published_monotonic':state['updated_monotonic'],'rate_diagnostic':state['clock_mapping']['measured_rate_diagnostic']})
    assert all(a['raw_source']['plant_time_s']<b['raw_source']['plant_time_s'] for a,b in zip(observations,observations[1:]))
    assert binding['effective_climate_epoch']==before['epoch_exact']
    assert binding['forcing']['time_reference']['source_times_sha256']==digest(before['timestamps'])
    assert accepted_reference(tdoc)==before;write(out/'ACCEPTED_TIME_REFERENCE_AFTER.json',accepted_reference(tdoc))
    write(out/'OBSERVED_CLOCK_MAPPINGS.json',observations)
    # Exercise the real workflow -> Layer3 boundary -> MicroMet path on the
    # captured source times, without advancing the native plant or a mission.
    workflow_spec=importlib.util.spec_from_file_location('workflow_clock_audit',tdoc/'scripts/run_training_workflows.py')
    workflow=importlib.util.module_from_spec(workflow_spec);workflow_spec.loader.exec_module(workflow)
    from layer3.config import Layer3Config
    from layer3.contracts import SignalContractRegistry
    from layer3.tdoc_adapter import TDOCAdapter
    from layer3.runtime import Layer3FMURuntime,RuntimeRequest
    from layer3.alignment import TimeSynchronizer
    from tddt_tdoc.native_time import stamp_climate_time
    from phoenix_env_adapter.bound import BoundEnvironment
    l3=Layer3Config.from_yaml(Path(os.environ['TDDT_LAYER3_ROOT'])/'configs/configuration_boulder.yaml')
    contracts=SignalContractRegistry(l3,TDOCAdapter(l3));runtime=Layer3FMURuntime.__new__(Layer3FMURuntime)
    runtime.config=l3;runtime.contracts=contracts;provider=BoundEnvironment(out/'native_run/BINDING.json')
    chain=[]
    forcing=tuple(datetime.fromisoformat(binding['forcing'][key]) for key in ('common_start','common_end'))
    for state in states:
        req={'climate':{},'context':{}}
        workflow._apply_online_climate_time(req,forcing,999.,{'ok':True,'effective_climate_epoch':before['epoch_exact']},
            native_binding_path=out/'native_run/BINDING.json',plant_time_s=state['plant_time_s'])
        q=state['environment_query'];req['climate'].update(input=binding['forcing_file'],dem=binding['origin']['terrain_file']['path'])
        for name,value,unit,frame in [('latitude',q['latitude'],'deg','WGS84'),('longitude',q['longitude'],'deg','WGS84'),
                ('elevation_sea_level',q['altitude_msl_m'],'m','MSL')]:
            req['climate'][name]={'value':value,'unit':unit,'frame':frame,'semantic':name}
        request=RuntimeRequest(reference_time_s=state['plant_time_s'],regime_id=None,climate=req['climate'],context=req['context'])
        args=runtime._climate_args(request);actual=provider.estimate(args)
        assert actual['request']['time']==state['simulation_time_dataset'] and actual['fmu_outputs']['formula_valid']==1.
        packet=stamp_climate_time({'twin_id':'MicroMetTwin','timestamp':actual['request']['time']},req['context'])
        aligned=TimeSynchronizer(l3,contracts.tdoc).align(packet,state['plant_time_s'],context=req['context'])
        assert aligned.age_s==0. and aligned.source_time_s==state['plant_time_s'] and not aligned.warning
        chain.append({'plant_time_s':state['plant_time_s'],'environment_adapter_time':state['simulation_time_dataset'],
            'workflow_time':req['climate']['time']['value'],'Layer3_actual_FMU_query':actual['request']['time'],
            'Layer3_CoC_source_time_s':aligned.source_time_s,'Layer3_CoC_age_s':aligned.age_s,
            'time_reference':packet['timestamp_reference'],'formula_valid':actual['fmu_outputs']['formula_valid']})
    write(out/'WORKFLOW_LAYER3_MICROMET_TIME_CHAIN.json',{'status':'PASS','samples':chain,
        'actual_FMU_queries':True,'full_Layer3_cycle_or_flight_claimed':False})
    from copy import deepcopy
    rejected=[]
    for reason,change in [('implicit_UTC',lambda s:s.update(simulation_time_dataset=s['simulation_time_dataset']+'Z')),
        ('foreign_host_boot',lambda s:s['clock_mapping'].update(monotonic_domain_id='foreign-boot')),
        ('hidden_rebase',lambda s:s['clock_mapping'].update(first_packet_rebase=True))]:
        altered=deepcopy(states[-1]);change(altered);m=altered['clock_mapping']
        m['mapping_hash']=_hash({k:v for k,v in m.items() if k!='mapping_hash'})
        try:validate_clock_mapping(binding,altered,now=altered['updated_monotonic'])
        except ValueError as exc:rejected.append({'case':reason,'rejected':True,'reason':str(exc)})
        else:raise AssertionError('invalid clock evidence accepted: '+reason)
    write(out/'NEGATIVE_CLOCK_PROJECTIONS.json',rejected)
    result={'step':'R4.04','status':'PASS','binding_id':binding['binding_id'],'binding_hash':binding['binding_hash'],
        'model_id':binding['model_id'],'model_artifacts':len(binding['model_dependencies']['artifacts']),
        'host_timezone_for_audit':'Pacific/Honolulu','accepted_epoch_exact':before['epoch_exact'],
        'source_timestamp_count':len(before['timestamps']),'source_timestamp_digest':digest(before['timestamps']),
        'timezone_status':'unspecified','UTC_conversion_performed':False,'first_packet_rebase':False,
        'real_native_updates':len(states),'source_clock':'ArduPilot time_boot_ms',
        'monotonic_clock':'host-boot-identified receipt clock; rate not assumed',
        'absolute_clock_synchronization_claimed':False,'ARM_sent':False,'flight_ready':False,
        'protected_handoff_and_forcing_unchanged':True,'runtime_mapping_valid_at_publication':True,
        'historical_states_are_not_current_readiness':True,'negative_projections_rejected':len(rejected)}
    write(out/'CLOCK_ALIGNMENT_RESULT.json',result);print(json.dumps(result))


if __name__=='__main__':main()
