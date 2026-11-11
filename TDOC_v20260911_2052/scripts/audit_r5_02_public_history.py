#!/usr/bin/env python3
"""Private disarmed SITL + real GUI read handler + public Layer6Client history."""
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


def write(path,obj):
    temporary=path.with_suffix(path.suffix+'.tmp');temporary.write_text(json.dumps(obj,indent=2,allow_nan=False)+'\n');temporary.replace(path)
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def load_module(path,name):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def worker(root,out):
    import socket
    from types import SimpleNamespace,MethodType
    native=root/'layer6_v20261007_1032'
    sys.path[:0]=[str(native/'src'),str(native/'src/gui')]
    import phoenix_f310_gui as gui
    from layer6.mavlink_link import MavlinkLink
    from pymavlink import mavutil
    from pymavlink.dialects.v20 import ardupilotmega as dialect
    assert gui.MavlinkLink is MavlinkLink
    factory=mavutil.mavlink_connection;sent=[];hearts=[];packets=[];sessions=[];stop=[False]
    def instrument(endpoint,**kwargs):
        assert endpoint=='tcp:127.0.0.1:26950'
        deadline=time.monotonic()+20;master=None
        while time.monotonic()<deadline:
            try:master=factory(endpoint,retries=0,**kwargs);break
            except (OSError,ConnectionError):time.sleep(.1)
        assert master is not None,'private native connection unavailable'
        raw_write=master.write
        def transport(data):
            message=dialect.MAVLink(None).parse_char(bytes(data))
            assert message is not None and message.get_type()=='COMMAND_LONG' and message.command==511
            sent.append({**message.to_dict(),'wire_hex':bytes(data).hex()});return raw_write(data)
        master.write=transport;raw_recv=master.recv_msg
        def receive():
            message=raw_recv()
            if message is not None and message.get_type()=='HEARTBEAT':
                assert not message.base_mode & 128,'probe must remain disarmed'
                hearts.append({**message.to_dict(),'wire_hex':bytes(message.get_msgbuf()).hex()})
            return message
        master.recv_msg=receive;return master
    mavutil.mavlink_connection=instrument
    link=MavlinkLink({'mavlink':{'connection':'tcp:127.0.0.1:26950','heartbeat_timeout_seconds':15,'telemetry_history_capacity':256}})
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.bind(('127.0.0.1',27051));sock.setblocking(False)
    context=SimpleNamespace(link=link,virtual_sock=sock,virtual_state=None)
    context._send_virtual_history=MethodType(gui.PhoenixGUI._send_virtual_history,context)
    poll=MethodType(gui.PhoenixGUI._poll_virtual_pilot,context)
    def connect():
        link.connect();sessions.append(link.telemetry_receiver.history.session_id)
        original=link.telemetry_receiver.ingest
        def ingest(message):
            record=original(message)
            if record is not None:packets.append(record)
            return record
        link.telemetry_receiver.ingest=ingest
    signal.signal(signal.SIGTERM,lambda *_:stop.__setitem__(0,True))
    try:
        connect();write(out/'WORKER_READY.json',{'session_id':sessions[-1],'same_production_GUI_and_link':True})
        deadline=time.monotonic()+40;reconnected=False;detached=False
        while not stop[0] and time.monotonic()<deadline and not (out/'STOP_WORKER').exists():
            if (out/'RECONNECT_REQUEST').exists() and not reconnected:
                link.master.close();connect();reconnected=True
                write(out/'RECONNECTED.json',{'session_id':sessions[-1]})
            if (out/'DETACH_REQUEST').exists() and not detached:
                link.master.close();context.link=None;detached=True;write(out/'DETACHED.json',{'detached':True})
            if not detached:link.drain();assert link.armed is False
            poll();time.sleep(.003)
        assert len(sent)==10 and len(sessions)==2 and sessions[0]!=sessions[1]
        assert len(hearts)>=2 and len(packets)>256
        write(out/'WORKER_RESULT.json',{'status':'PASS','sessions':sessions,'raw_packets':len(packets),
             'outgoing_interval_requests':len(sent),'only_COMMAND_LONG_511_sent':True,'disarmed_heartbeats':len(hearts),
             'GUI_handler':'PhoenixGUI._poll_virtual_pilot + _send_virtual_history',
             'QApplication_created':False,'pilot_or_control_tick_called':False})
    finally:
        mavutil.mavlink_connection=factory
        if link.master:link.master.close()
        sock.close();write(out/'SENT_MESSAGES.json',sent);write(out/'DISARMED_HEARTBEATS.json',hearts)
        with (out/'RECEIVED_CHANNELS.jsonl').open('w') as stream:
            for record in packets:stream.write(json.dumps(record,allow_nan=False)+'\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-dir',required=True,type=Path)
    parser.add_argument('--worker',action='store_true');args=parser.parse_args()
    tdoc=Path(__file__).resolve().parents[1];root=tdoc.parent;out=args.output_dir.resolve()
    out.relative_to(tdoc/'reports/online')
    if args.worker:worker(root,out);return
    out.mkdir(exist_ok=False);native=root/'layer6_v20261007_1032';fs=native/'FlightStack';ap=fs/'ardupilot'
    sys.path[:0]=[str(native/'src'),str(tdoc/'src')]
    from layer6.client import Layer6Client
    from layer6.telemetry_history import MAX_RESPONSE_BYTES,encode_response
    from tddt_tdoc.config import TDOCConfig
    from tddt_tdoc.native_environment import prepare_binding
    from pymavlink.dialects.v20 import ardupilotmega as dialect
    helper=load_module(tdoc/'scripts/audit_r4_03_native_forcing.py','r502_helpers')
    protection=load_module(tdoc/'scripts/freeze_r1_02_state.py','r502_protection')
    baseline=root/'recovery_baselines/R3_05_20261029_fixture_retirement/PROTECTED_MANIFEST.json'
    before=protection.verify_manifest(baseline);write(out/'PROTECTION_BEFORE.json',before);assert before['status']=='PASS'
    active=[native/'runtime/tdoc_environment_binding.json',native/'runtime/bound_environment_state.json']
    active_hash={str(p):sha(p) if p.is_file() else None for p in active};write(out/'ACTIVE_RUNTIME_BEFORE.json',active_hash)
    binding=prepare_binding(TDOCConfig.from_yaml(tdoc/'configs/tdoc.yaml'),output_path=out/'BINDING.json')
    private=out/'private_autotest';private.mkdir()
    shutil.copytree(ap/'Tools/autotest/aircraft/phoenix_v3',private/'aircraft/phoenix_v3')
    defaults=out/'phoenix_v3.parm';shutil.copy2(ap/'Tools/autotest/default_params/phoenix_v3.parm',defaults)
    binary=ap/'build/sitl/bin/arduplane';js=fs/'jsbsim/build/src/JSBSim';home=binding['mission_origin']
    location=','.join(str(home[k]) for k in ('latitude_deg','longitude_deg','terrain_elevation_msl_m','heading_deg'))
    argv=[str(binary),'--model','jsbsim:phoenix_v3','--home',location,'--speedup','1','--base-port','26950',
          '-I','85','--autotest-dir',str(private),'--defaults',str(defaults)]
    env=dict(os.environ,PATH=str(js.parent)+os.pathsep+os.environ['PATH'],PYTHONDONTWRITEBYTECODE='1',TMPDIR='/tmp')
    process=gui_worker=client=None;owned=set();executables={};pages=[]
    with (out/'NATIVE_STDOUT.log').open('w') as native_log,(out/'GUI_HANDLER.log').open('w') as gui_log:
        try:
            process=subprocess.Popen(argv,cwd=out,env=env,stdout=native_log,stderr=subprocess.STDOUT,start_new_session=True)
            gui_worker=subprocess.Popen([str(ap/'venv-ardupilot-debian13/bin/python'),'-B',str(Path(__file__).resolve()),
                 '--worker','--output-dir',str(out)],cwd=out,env=env,stdout=gui_log,stderr=subprocess.STDOUT,start_new_session=True)
            def wait_file(name,seconds=30):
                deadline=time.monotonic()+seconds
                while time.monotonic()<deadline:
                    assert process.poll() is None and gui_worker.poll() is None,'native/GUI-handler startup failed'
                    if (out/name).is_file():return json.loads((out/name).read_text())
                    time.sleep(.05)
                raise AssertionError('timed out waiting for '+name)
            wait_file('WORKER_READY.json');client=Layer6Client(endpoint='127.0.0.1:27052',gui_endpoint='127.0.0.1:27051')
            def read(**query):
                page=client.telemetry_history(timeout=1.,**query);assert page is not None,'public history timeout'
                assert len(encode_response(page))<=MAX_RESPONSE_BYTES
                pages.append(page);return page
            deadline=time.monotonic()+10
            while True:
                first=read(limit=64)
                if first['ok'] and first['evicted_total']>20:break
                assert time.monotonic()<deadline,'overflow not observed';time.sleep(.05)
            assert first['retention_gap'] and first['lost_unfiltered_samples']==first['oldest_sequence']-1
            assert first['capacity']==256 and first['retained_count']==256
            captured=list(first['samples']);page=first
            while page['has_more']:
                page=read(limit=64,after_seq=page['next_after_seq'],session_id=first['session_id'],through_seq=first['through_seq'])
                assert not page['retention_gap'],'pagination overtaken; evidence must not silently skip'
                captured.extend(page['samples'])
            assert [r['history_sequence'] for r in captured]==list(range(first['oldest_sequence'],first['through_seq']+1))
            empty=read(**{'after_seq':page['next_after_seq'],'session_id':page['session_id'],'through_seq':page['through_seq']})
            assert empty['samples']==[] and not empty['has_more']
            missing=read(channels=['HIGHRES_IMU']);assert missing['ok'] and missing['samples']==[]
            filtered=read(channels=['SCALED_IMU']);assert filtered['samples'] and all(s['type']=='SCALED_IMU' for s in filtered['samples'])
            old_session=first['session_id'];(out/'RECONNECT_REQUEST').touch();new=wait_file('RECONNECTED.json',20)
            changed=read(after_seq=first['next_after_seq'],session_id=old_session)
            assert changed['reason']=='session_changed' and not changed['ok'] and changed['session_id']==new['session_id']
            fresh=read();assert fresh['ok'] and fresh['session_id']!=old_session
            (out/'DETACH_REQUEST').touch();wait_file('DETACHED.json',5)
            assert read()['reason']=='not_connected'
            owned.update(helper.descendants(process.pid))
            for pid in [process.pid,*owned]:
                try:executables[str(pid)]=str(Path(f'/proc/{pid}/exe').resolve(strict=True))
                except (FileNotFoundError,ProcessLookupError,PermissionError):pass
            assert str(binary) in executables.values() and str(js) in executables.values()
            (out/'STOP_WORKER').touch();gui_worker.wait(timeout=10);assert gui_worker.returncode==0
            assert client.telemetry_history(timeout=.05) is None,'stopped peer cannot yield a valid empty history'
            raw=[json.loads(line) for line in (out/'RECEIVED_CHANNELS.jsonl').read_text().splitlines()]
            index={(r['session_id'],r['history_sequence']):r for r in raw}
            for page in pages:
                for record in page['samples']:
                    assert index[(record['session_id'],record['history_sequence'])]==record
                    message=dialect.MAVLink(None).parse_char(bytes.fromhex(record['wire_hex']))
                    assert message is not None and message.get_type()==record['type']
                    for field,value in record['fields'].items():
                        if value is not None:assert getattr(message,field)==value
                    if record['source_time'] is not None:assert record['source_time']['value']==getattr(message,record['source_time']['field'])
            result={'step':'R5.02','status':'PASS','argv':argv,'executables_observed_live':executables,
              'arduplane_sha256':sha(binary),'JSBSim_sha256':sha(js),
              'fourfoot_model_sha256':sha(private/'aircraft/phoenix_v3/phoenix_v3.xml'),
              'binding_id':binding['binding_id'],'binding_hash':binding['binding_hash'],'model_id':binding['model_id'],
              'handoff_hash':binding['handoff_hash'],'forcing_file_sha256':binding['forcing_file_sha256'],
              'public_API':'Layer6Client.telemetry_history','production_GUI_handler_used':True,
              'public_datagrams_verified':len(pages),'unique_snapshot_samples':len(captured),'real_raw_packets':len(raw),
              'source_time_preserved':True,'actual_retention_overflow_verified':True,'frozen_pagination_verified':True,
              'real_reconnect_session_change_verified':True,'disconnected_and_timeout_verified':True,
              'largest_response_bytes':max(len(encode_response(p)) for p in pages),'max_response_bytes':MAX_RESPONSE_BYTES,
              'QApplication_created':False,'pilot_commands_sent':False,'active_reset':False,
              'R5_stage_complete':False,'SI_frame_freshness_qualified':False,'GNSS_denied_flight_validated':False,'flight_ready':False}
            assert sha(private/'aircraft/phoenix_v3/phoenix_v3.xml')==sha(ap/'Tools/autotest/aircraft/phoenix_v3/phoenix_v3.xml')
            assert json.loads((out/'WORKER_RESULT.json').read_text())['status']=='PASS'
            write(out/'NATIVE_HISTORY_RESULT.json',result)
        finally:
            if client:client.close()
            if gui_worker and gui_worker.poll() is None:
                gui_worker.terminate()
                try:gui_worker.wait(timeout=5)
                except subprocess.TimeoutExpired:gui_worker.kill();gui_worker.wait(timeout=5)
            if process:
                owned.update(helper.descendants(process.pid))
                for pid in sorted(owned,reverse=True):
                    try:os.kill(pid,signal.SIGTERM)
                    except ProcessLookupError:pass
                if process.poll() is None:process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            write(out/'PUBLIC_HISTORY_RESPONSES.json',pages)
            after=protection.verify_manifest(baseline);write(out/'PROTECTION_AFTER.json',after);assert after['status']=='PASS'
            current_hash={str(p):sha(p) if p.is_file() else None for p in active};write(out/'ACTIVE_RUNTIME_AFTER.json',current_hash)
            assert current_hash==active_hash
    print(json.dumps({'step':'R5.02','status':'PASS','native_public_history_verified':True,'flight_ready':False}))


if __name__=='__main__':main()
