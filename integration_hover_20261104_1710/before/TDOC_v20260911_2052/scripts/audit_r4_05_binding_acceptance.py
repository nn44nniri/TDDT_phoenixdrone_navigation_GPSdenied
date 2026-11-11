#!/usr/bin/env python3
"""Accept current native forcing; reject unavailable/incompatible/stale bindings.

The dependency audit owns its isolated disarmed SITL processes. Fault injection
only creates/removes private report copies, never accepted knowledge or forcing.
"""
import argparse
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime,timedelta
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid


def write(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2,allow_nan=False)+'\n')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',required=True,type=Path)
    a=p.parse_args();tdoc=Path(__file__).resolve().parents[1];root=tdoc.parent
    out=a.output_dir.resolve();out.relative_to(tdoc/'reports/online');out.mkdir(exist_ok=False)
    spec=importlib.util.spec_from_file_location('native_clock_dependency',tdoc/'scripts/audit_r4_04_clock_alignment.py')
    dependency=importlib.util.module_from_spec(spec);spec.loader.exec_module(dependency)
    original_argv=sys.argv[:]
    try:
        sys.argv=[str(tdoc/'scripts/audit_r4_04_clock_alignment.py'),'--output-dir',str(out/'native_clock')]
        with (out/'LIVE_NATIVE.log').open('w') as stream,redirect_stdout(stream):dependency.main()
    finally:sys.argv=original_argv
    from tddt_tdoc.native_binding import read_binding,digest,_identity_parts
    from tddt_tdoc.native_time import project_native_time
    from layer6.binding import load_context,context_guard,environment_status
    import run_flightstack
    path=out/'native_clock/native_run/BINDING.json';binding=read_binding(path)
    state_path=out/'native_clock/native_run/STATE.json';state=json.loads(state_path.read_text())
    native=json.loads((out/'native_clock/native_run/NATIVE_FORCING_RESULT.json').read_text())
    assert native['status']=='PASS'
    # Real aging: keep the original raw evidence and wait, without editing times.
    received=state['clock_mapping']['observation']['received_monotonic_s']
    remaining=1.1-(time.monotonic()-received)
    if remaining>0:time.sleep(remaining)
    stale=environment_status(path,state_path)
    assert not stale['ok'] and stale['binding_consumed']
    write(out/'REAL_STALE_STATE_REJECTION.json',{'status':'PASS','original_readback_verified':state['readback_verified'],
        'source_times_modified':False,'elapsed_since_actual_receipt_s':time.monotonic()-received,'current_projection':stale})
    mutations=out/'private_invalid_bindings';mutations.mkdir()
    results=[]
    cases=['missing','malformed','old_revision','stale_model','wrong_forcing','wrong_handoff','changed_origin','hidden_rebase','implicit_UTC']
    for case in cases:
        target=mutations/(case+'.json');b=deepcopy(binding);b['binding_id']=uuid.uuid4().hex
        if case=='malformed':target.write_text('{invalid-json')
        elif case=='stale_model':
            target.write_bytes((tdoc/'reports/online/R4_04_20261030_clock_alignment_v6/native_final/native_run/BINDING.json').read_bytes())
        elif case!='missing':
            if case=='old_revision':b['revision']='native-unified-binding-r4.02-v1'
            elif case=='wrong_forcing':b['forcing']['sha256']=b['forcing_file_sha256']='0'*64
            elif case=='wrong_handoff':b['knowledge']['handoff_hash']=b['handoff_hash']='0'*64
            elif case=='changed_origin':
                for row in (b['origin']['initialization'],b['mission_origin']):row['latitude_deg']+=.001
            elif case in ('hidden_rebase','implicit_UTC'):
                epoch=b['effective_climate_epoch']
                if case=='hidden_rebase':epoch=(datetime.fromisoformat(epoch)+timedelta(seconds=60)).isoformat()
                else:
                    epoch+='Z'
                    for row in (b['forcing'],b['time_contract']):row.update(clock_domain='UTC_OFFSET_AWARE',timezone_status='offset_explicit',utc_mapping_verified=True)
                    for k in ('common_start','common_end'):b['forcing'][k]+='Z'
                    for row in b['forcing']['point_coverage'].values():
                        for k in ('start','end'):row[k]+='Z'
                b['effective_climate_epoch']=b['time_contract']['effective_climate_epoch']=b['forcing']['time_reference']['effective_epoch_exact']=epoch
                b['time_contract']['normalized_effective_epoch']=datetime.fromisoformat(epoch.replace('Z','+00:00')).isoformat()
            b['identity_hashes']=_identity_parts(b);b['context_sha256']=digest(b['identity_hashes'])
            b['binding_hash']=digest({k:v for k,v in b.items() if k!='binding_hash'});write(target,b)
        row={'case':case,'consumers':{}}
        operations={'load_context':lambda:load_context(target),'guard':lambda:context_guard(target),
            'Layer3_projection':lambda:project_native_time(target,10.),
            'launcher_plan':lambda:run_flightstack._commands(root/'layer6_v20261007_1032/FlightStack',target)}
        for name,operation in operations.items():
            try:operation()
            except (ValueError,FileNotFoundError) as exc:row['consumers'][name]={'rejected':True,'error':str(exc)}
            else:raise AssertionError('invalid binding accepted: '+case+'/'+name)
        status=environment_status(target,state_path)
        assert not status['ok'] and not status['binding_consumed'];row['consumers']['GUI']=status
        output=mutations/(case+'_STATE.json')
        argv=[sys.executable,'-B','-m','phoenix_env_adapter.cli','runtime','--binding',str(target),
            '--state-file',str(output),'--mavlink','invalid-binding-must-not-connect','--dry-run','--plant-time-s','10']
        done=subprocess.run(argv,capture_output=True,text=True,timeout=30)
        (mutations/(case+'_CLI.log')).write_text(done.stdout+done.stderr)
        assert done.returncode!=0 and not output.exists()
        row['consumers']['actual_CLI']={'exit_code':done.returncode,'state_published':False}
        results.append(row)
    write(out/'BINDING_REJECTION_MATRIX.json',results)
    # A retained guard cannot keep using an absent publication, even though the
    # real source files and all historical readback values still exist.
    raw=path.read_bytes();guard=context_guard(path);path.unlink()
    try:
        try:guard.check()
        except FileNotFoundError:pass
        else:raise AssertionError('cached guard accepted deleted publication')
        missing=environment_status(path,state_path)
        assert not missing['ok'] and not missing['binding_consumed']
        write(out/'REAL_PUBLICATION_DELETION_REJECTION.json',{'status':'PASS','private_publication_only':True,'projection':missing})
    finally:path.write_bytes(raw)
    assert read_binding(path)==binding
    result={'step':'R4.05','status':'PASS','binding_id':binding['binding_id'],'binding_hash':binding['binding_hash'],
        'model_id':binding['model_id'],'handoff_hash':binding['handoff_hash'],'forcing_file_sha256':binding['forcing_file_sha256'],
        'accepted_epoch_exact':binding['effective_climate_epoch'],'timezone_status':binding['time_contract']['timezone_status'],
        'live_positive_readback':True,'real_stale_rejection':True,'real_private_publication_deletion_rejected':True,
        'negative_binding_cases':len(results),'negative_consumers_per_case':6,
        'accepted_inputs_mutated':False,'ARM_sent':False,'flight_ready':False,
        'absolute_clock_sync_claimed':False,'TTL_relaxed':False,'legacy_fallback_used':False}
    write(out/'BINDING_ACCEPTANCE_RESULT.json',result);print(json.dumps(result))


if __name__=='__main__':main()
