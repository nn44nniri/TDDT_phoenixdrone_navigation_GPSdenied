#!/usr/bin/env python3
"""Bounded, disarmed production receiver audit against private ArduPilot/JSBSim."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',required=True,type=Path)
    args=parser.parse_args();tdoc=Path(__file__).resolve().parents[1];root=tdoc.parent
    out=args.output_dir.resolve();out.relative_to(tdoc/'reports/online');out.mkdir(exist_ok=False)
    native=root/'layer6_v20261007_1032';fs=native/'FlightStack';ap=fs/'ardupilot'
    sys.path[:0]=[str(native/'src'),str(tdoc/'src')]
    from layer6.mavlink_link import MavlinkLink
    from layer6.telemetry import REQUEST_HZ
    from tddt_tdoc.config import TDOCConfig
    from tddt_tdoc.native_environment import prepare_binding
    from pymavlink import mavutil
    from pymavlink.dialects.v20 import ardupilotmega as dialect
    spec=importlib.util.spec_from_file_location('r403_helpers',tdoc/'scripts/audit_r4_03_native_forcing.py')
    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
    spec=importlib.util.spec_from_file_location('r501_protection',tdoc/'scripts/freeze_r1_02_state.py')
    protection=importlib.util.module_from_spec(spec);spec.loader.exec_module(protection)
    baseline=root/'recovery_baselines/R3_05_20261029_fixture_retirement/PROTECTED_MANIFEST.json'
    before=protection.verify_manifest(baseline);helper.write(out/'PROTECTION_BEFORE.json',before)
    assert before['status']=='PASS'
    active=[native/'runtime/tdoc_environment_binding.json',native/'runtime/bound_environment_state.json']
    active_hash={str(p):helper.sha(p) if p.is_file() else None for p in active}
    binding=prepare_binding(TDOCConfig.from_yaml(tdoc/'configs/tdoc.yaml'),output_path=out/'BINDING.json')
    private=out/'private_autotest';private.mkdir()
    shutil.copytree(ap/'Tools/autotest/aircraft/phoenix_v3',private/'aircraft/phoenix_v3')
    defaults=out/'phoenix_v3.parm';shutil.copy2(ap/'Tools/autotest/default_params/phoenix_v3.parm',defaults)
    binary=ap/'build/sitl/bin/arduplane';js=fs/'jsbsim/build/src/JSBSim'
    home=binding['mission_origin'];location=','.join(str(home[k]) for k in ('latitude_deg','longitude_deg','terrain_elevation_msl_m','heading_deg'))
    argv=[str(binary),'--model','jsbsim:phoenix_v3','--home',location,'--speedup','1',
          '--base-port','26940','-I','84','--autotest-dir',str(private),'--defaults',str(defaults)]
    env=dict(os.environ,PATH=str(js.parent)+os.pathsep+os.environ['PATH'],PYTHONDONTWRITEBYTECODE='1',TMPDIR='/tmp')
    process=None;master=None;owned=set();executables={};hearts=[];sent=[];packets=[]
    factory=mavutil.mavlink_connection
    with (out/'NATIVE_STDOUT.log').open('w') as log:
        try:
            process=subprocess.Popen(argv,cwd=out,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            deadline=time.monotonic()+35
            while time.monotonic()<deadline:
                if process.poll() is not None:raise RuntimeError('native startup exited')
                try:master=factory('tcp:127.0.0.1:26940',retries=0,source_system=255,source_component=191);break
                except (OSError,ConnectionError):time.sleep(.2)
            assert master is not None,'private native TCP endpoint unavailable'
            # Transparent instrumentation; the production receiver still decodes real bytes.
            # pymavlink replaces master.mav when auto-detecting the wire version.
            # Observe transport writes so the whitelist survives that replacement.
            original_write=master.write
            def write(data):
                message=dialect.MAVLink(None).parse_char(bytes(data))
                assert message is not None,'outgoing undecodable transport frame'
                assert message.get_type()=='COMMAND_LONG' and message.command==511,'pilot/other outgoing message forbidden'
                sent.append({**message.to_dict(),'wire_hex':bytes(data).hex()});return original_write(data)
            master.write=write
            original_recv=master.recv_msg
            def receive():
                message=original_recv()
                if message is not None and message.get_type()=='HEARTBEAT':
                    assert not message.base_mode & 128,'audit must remain disarmed'
                    hearts.append({**message.to_dict(),'source_system':message.get_srcSystem(),
                                   'source_component':message.get_srcComponent(),'wire_hex':bytes(message.get_msgbuf()).hex()})
                return message
            master.recv_msg=receive
            def use_private_connection(endpoint,**kw):
                assert endpoint=='tcp:127.0.0.1:26940';return master
            mavutil.mavlink_connection=use_private_connection
            link=MavlinkLink({'mavlink':{'connection':'tcp:127.0.0.1:26940','heartbeat_timeout_seconds':15}})
            link.connect()
            assert not any(v['received'] for v in link.received_channels().values()),'requests cannot become samples'
            original_ingest=link.telemetry_receiver.ingest
            def ingest(message):
                record=original_ingest(message)
                if record is not None:packets.append(record)
                return record
            link.telemetry_receiver.ingest=ingest
            deadline=time.monotonic()+12
            while time.monotonic()<deadline:
                assert process.poll() is None,'native process exited during receive'
                link.drain();assert link.armed is False
                owned.update(helper.descendants(process.pid))
                time.sleep(.02)
            for pid in [process.pid,*owned]:
                try:executables[str(pid)]=str(Path(f'/proc/{pid}/exe').resolve(strict=True))
                except (FileNotFoundError,ProcessLookupError,PermissionError):pass
            snapshot=link.received_channels();helper.write(out/'CHANNEL_SNAPSHOT.json',snapshot)
            helper.write(out/'INTERVAL_REQUESTS.json',link.telemetry_requests)
            assert all(snapshot[n]['available'] and snapshot[n]['received_count']>=3 for n in REQUEST_HZ),snapshot
            assert len(sent)==5 and len(hearts)>=2
            assert str(binary) in executables.values() and str(js) in executables.values()
            # Independently decode archived wire bytes and compare every retained field.
            for record in packets:
                raw=bytes.fromhex(record['wire_hex']);message=dialect.MAVLink(None).parse_char(raw)
                assert message is not None and message.get_type()==record['type']
                assert (message.get_srcSystem(),message.get_srcComponent())==(record['source_system'],record['source_component'])
                assert record['valid'],record['issues']
                for field,value in record['fields'].items():
                    if value is not None:assert getattr(message,field)==value,(record['type'],field)
                if record['source_time'] is not None:
                    assert record['source_time']['value']==getattr(message,record['source_time']['field'])
                assert not record['navigation_truth_injection'] and not record['guidance_state_qualified']
            assert helper.sha(private/'aircraft/phoenix_v3/phoenix_v3.xml')==helper.sha(ap/'Tools/autotest/aircraft/phoenix_v3/phoenix_v3.xml')
            helper.write(out/'NATIVE_TELEMETRY_RESULT.json',{'step':'R5.01','status':'PASS','argv':argv,
                'executables_observed_live':executables,'arduplane_sha256':helper.sha(binary),'JSBSim_sha256':helper.sha(js),
                'fourfoot_model_sha256':helper.sha(private/'aircraft/phoenix_v3/phoenix_v3.xml'),
                'binding_id':binding['binding_id'],'binding_hash':binding['binding_hash'],'model_id':binding['model_id'],
                'forcing_file_sha256':binding['forcing_file_sha256'],'handoff_hash':binding['handoff_hash'],
                'sample_counts':{n:s['received_count'] for n,s in snapshot.items()},'real_packet_count':len(packets),
                'wire_independent_redecode_verified':True,'disarmed_heartbeats':len(hearts),'outgoing_interval_requests':len(sent),
                'only_COMMAND_LONG_511_sent':True,'ARM_sent':False,'pilot_commands_sent':False,'GUI_started':False,
                'active_reset':False,'SI_frame_freshness_qualified':False,'R5_stage_complete':False,'flight_ready':False})
        finally:
            mavutil.mavlink_connection=factory
            if master:master.close()
            if process:
                owned.update(helper.descendants(process.pid))
                for pid in sorted(owned,reverse=True):
                    try:os.kill(pid,signal.SIGTERM)
                    except ProcessLookupError:pass
                if process.poll() is None:process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            with (out/'RECEIVED_CHANNELS.jsonl').open('w') as stream:
                for record in packets:stream.write(json.dumps(record,allow_nan=False)+'\n')
            helper.write(out/'SENT_MESSAGES.json',sent);helper.write(out/'DISARMED_HEARTBEATS.json',hearts)
            after=protection.verify_manifest(baseline);helper.write(out/'PROTECTION_AFTER.json',after);assert after['status']=='PASS'
            assert all((helper.sha(p) if p.is_file() else None)==active_hash[str(p)] for p in active)
    print(json.dumps({'step':'R5.01','status':'PASS','real_native_receiver_verified':True,'flight_ready':False}))


if __name__=='__main__':main()
