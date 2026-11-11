#!/usr/bin/env python3
"""Bounded disarmed real ArduPilot/JSBSim wind readback in a private directory."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time


def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def write(p,obj):p.write_text(json.dumps(obj,indent=2,allow_nan=False)+'\n')


def descendants(pid):
    try:children=[int(x) for x in Path(f'/proc/{pid}/task/{pid}/children').read_text().split()]
    except (FileNotFoundError,ProcessLookupError):return []
    return [p for c in children for p in [c,*descendants(c)]]


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',required=True,type=Path)
    a=p.parse_args();tdoc=Path(__file__).resolve().parents[1];root=tdoc.parent
    out=a.output_dir.resolve();out.relative_to(tdoc/'reports/online');out.mkdir(exist_ok=False)
    native=root/'layer6_v20261007_1032';fs=native/'FlightStack';ap=fs/'ardupilot'
    sys.path[:0]=[str(native/'src'),str(fs/'phoenix_environment_adapter_v2'),str(tdoc/'src')]
    from tddt_tdoc.config import TDOCConfig
    from tddt_tdoc.native_environment import prepare_binding
    from layer6.binding import environment_status
    from phoenix_env_adapter.bound import run_bound_runtime
    from pymavlink import mavutil
    spec=importlib.util.spec_from_file_location('protection403',tdoc/'scripts/freeze_r1_02_state.py')
    protection=importlib.util.module_from_spec(spec);spec.loader.exec_module(protection)
    baseline=root/'recovery_baselines/R3_05_20261029_fixture_retirement/PROTECTED_MANIFEST.json'
    before=protection.verify_manifest(baseline);assert before['status']=='PASS';write(out/'PROTECTION_BEFORE.json',before)
    active=[native/'runtime/tdoc_environment_binding.json',native/'runtime/bound_environment_state.json']
    active_hash={str(p):sha(p) if p.is_file() else None for p in active}
    binding_path=out/'BINDING.json';binding=prepare_binding(TDOCConfig.from_yaml(tdoc/'configs/tdoc.yaml'),output_path=binding_path)
    private=out/'private_autotest';private.mkdir()
    shutil.copytree(ap/'Tools/autotest/aircraft/phoenix_v3',private/'aircraft/phoenix_v3')
    defaults=out/'phoenix_v3.parm';shutil.copy2(ap/'Tools/autotest/default_params/phoenix_v3.parm',defaults)
    binary=ap/'build/sitl/bin/arduplane';js=fs/'jsbsim/build/src/JSBSim'
    home=binding['mission_origin'];location=','.join(str(home[k]) for k in ('latitude_deg','longitude_deg','terrain_elevation_msl_m','heading_deg'))
    argv=[str(binary),'--model','jsbsim:phoenix_v3','--home',location,'--speedup','1',
        '--base-port','26930','-I','83','--autotest-dir',str(private),'--defaults',str(defaults)]
    env=dict(os.environ,PATH=str(js.parent)+os.pathsep+os.environ['PATH'],PYTHONDONTWRITEBYTECODE='1',TMPDIR='/tmp')
    process=None;master=None;observed=set();executables={};hearts=[];parameters=[];sent=[];states=[]
    original_factory=mavutil.mavlink_connection
    with (out/'NATIVE_STDOUT.log').open('w') as log:
        try:
            process=subprocess.Popen(argv,cwd=out,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            deadline=time.monotonic()+35
            while time.monotonic()<deadline:
                if process.poll() is not None:raise RuntimeError('native startup exited')
                try:master=original_factory('tcp:127.0.0.1:26930',retries=0);break
                except (OSError,ConnectionError):time.sleep(.2)
            assert master is not None,'native TCP endpoint unavailable'
            heartbeat=master.wait_heartbeat(timeout=15);assert heartbeat is not None
            assert not heartbeat.base_mode & 128,'native probe must remain disarmed'
            master.mav.request_data_stream_send(master.target_system,master.target_component,0,10,1)
            # Wait beyond ArduPilot's actual 5-second simulated wind startup delay.
            while True:
                msg=master.recv_match(type=['HEARTBEAT','ATTITUDE'],blocking=True,timeout=5)
                if msg is None:raise RuntimeError('native source clock unavailable')
                if msg.get_type()=='HEARTBEAT':
                    assert not msg.base_mode & 128;hearts.append(msg.to_dict())
                elif msg.time_boot_ms>=7000:break
            observed.update(descendants(process.pid))
            for pid in [process.pid,*observed]:
                try:executables[str(pid)]=str(Path(f'/proc/{pid}/exe').resolve(strict=True))
                except (FileNotFoundError,ProcessLookupError,PermissionError):pass
            # Transparent instrumentation of real packets, without replacing values.
            original_recv=master.recv_msg
            def receive():
                msg=original_recv()
                if msg is not None:
                    if msg.get_type()=='HEARTBEAT':
                        assert not msg.base_mode & 128;hearts.append(msg.to_dict())
                    if msg.get_type()=='PARAM_VALUE':parameters.append({**msg.to_dict(),
                        'source_system':msg.get_srcSystem(),'source_component':msg.get_srcComponent(),
                        'received_monotonic':time.monotonic()})
                return msg
            master.recv_msg=receive
            # Restrict outgoing messages to parameter SET/GET (plus initial stream request).
            original_send=master.mav.send
            def send(message,*args,**kwargs):
                name=message.get_type();assert name in ('PARAM_SET','PARAM_REQUEST_READ'),name
                sent.append(message.to_dict());return original_send(message,*args,**kwargs)
            master.mav.send=send
            def factory(endpoint):
                assert endpoint=='tcp:127.0.0.1:26930';return master
            mavutil.mavlink_connection=factory
            state=run_bound_runtime(binding_path,out/'STATE.json',mavlink='tcp:127.0.0.1:26930',max_updates=3)
            states.append(state)
            status=environment_status(binding_path,out/'STATE.json');write(out/'GUI_ENVIRONMENT_PROJECTION.json',status)
            assert state['readback_verified'] and state['ok'] and status['ok']
            assert len(parameters)>=36 and len(sent)==36 and len(hearts)>=1
            assert str(binary) in executables.values() and str(js) in executables.values()
            assert sha(private/'aircraft/phoenix_v3/phoenix_v3.xml')==sha(ap/'Tools/autotest/aircraft/phoenix_v3/phoenix_v3.xml')
            write(out/'NATIVE_FORCING_RESULT.json',{'step':'R4.03','status':'PASS','argv':argv,
                'executables_observed_live':executables,'arduplane_sha256':sha(binary),'JSBSim_sha256':sha(js),
                'fourfoot_model_sha256':sha(private/'aircraft/phoenix_v3/phoenix_v3.xml'),
                'binding_id':binding['binding_id'],'binding_hash':binding['binding_hash'],'model_id':binding['model_id'],
                'forcing_file_sha256':binding['forcing_file_sha256'],'handoff_hash':binding['handoff_hash'],
                'verified_parameters':state['application']['parameters'],'unsupported_fields':state['unsupported_fields'],
                'real_parameter_messages':len(parameters),'parameter_sends':len(sent),'disarmed_heartbeats':len(hearts),
                'ARM_sent':False,'pilot_commands_sent':False,'GUI_started':False,'reset_active':False,
                'instantaneous_atmosphere_verified':False,'flight_ready':False,
                'timezone_status':state['timezone_status'],'UTC_mapping_verified':state['UTC_mapping_verified']})
        finally:
            mavutil.mavlink_connection=original_factory
            if master:master.close()
            if process:
                observed.update(descendants(process.pid))
                for pid in sorted(observed,reverse=True):
                    try:os.kill(pid,signal.SIGTERM)
                    except ProcessLookupError:pass
                if process.poll() is None:process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            write(out/'RECEIVED_PARAMETERS.json',parameters);write(out/'SENT_PARAMETER_MESSAGES.json',sent)
            write(out/'DISARMED_HEARTBEATS.json',hearts)
            after=protection.verify_manifest(baseline);write(out/'PROTECTION_AFTER.json',after);assert after['status']=='PASS'
            assert all((sha(p) if p.is_file() else None)==active_hash[str(p)] for p in active)
    print(json.dumps({'step':'R4.03','status':'PASS','real_native_readback':True,'flight_ready':False}))


if __name__=='__main__':main()
