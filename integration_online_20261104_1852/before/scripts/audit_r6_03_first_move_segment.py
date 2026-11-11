#!/usr/bin/env python3
"""Private disarmed neutral-command targeted cancellation through actual service/GUI/MAVLink."""
import argparse
import hashlib
import importlib.util
import json
import math
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
    factory=mavutil.mavlink_connection;sent=[];hearts=[];packets=[];all_messages=[];sessions=[];parameters=[];stop=[False]
    def instrument(endpoint,**kwargs):
        assert endpoint=='tcp:127.0.0.1:27040'
        deadline=time.monotonic()+20;master=None
        while time.monotonic()<deadline:
            try:master=factory(endpoint,retries=0,**kwargs);break
            except (OSError,ConnectionError):time.sleep(.1)
        assert master is not None,'private native connection unavailable'
        raw_write=master.write
        def transport(data):
            message=dialect.MAVLink(None).parse_char(bytes(data))
            assert message is not None
            kind=message.get_type()
            assert (kind=='PARAM_REQUEST_READ' or kind=='COMMAND_LONG' and message.command==511
                or kind=='MANUAL_CONTROL' and all(getattr(message,k)==0 for k in ('x','y','z','r','buttons'))
                or kind=='TIMESYNC' and message.tc1==0 and message.ts1>0),'non-neutral probe frame'
            if kind=='MANUAL_CONTROL':assert message.target==master.target_system
            sent.append({**message.to_dict(),'wire_hex':bytes(data).hex(),'sent_monotonic':time.monotonic(),
                'active_transaction_id':(context.virtual_state or {}).get('transaction_id'),
                'command_instance_id':(context.virtual_state or {}).get('command_instance_id')});return raw_write(data)
        master.write=transport;raw_recv=master.recv_msg
        def receive():
            message=raw_recv()
            if message is not None and message.get_type()!='BAD_DATA':
                all_messages.append({'type':message.get_type(),'wire_hex':bytes(message.get_msgbuf()).hex(),
                                     'source_system':message.get_srcSystem(),'source_component':message.get_srcComponent()})
            if message is not None and message.get_type()=='PARAM_VALUE' and message.param_id=='AHRS_EKF_TYPE':parameters.append(message.to_dict())
            if message is not None and message.get_type()=='HEARTBEAT':
                assert not message.base_mode & 128,'probe must remain disarmed'
                hearts.append({**message.to_dict(),'wire_hex':bytes(message.get_msgbuf()).hex()})
            return message
        master.recv_msg=receive;return master
    mavutil.mavlink_connection=instrument
    link=MavlinkLink({'mavlink':{'connection':'tcp:127.0.0.1:27040','heartbeat_timeout_seconds':15,'telemetry_history_capacity':256}})
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.bind(('127.0.0.1',27191));sock.setblocking(False)
    context=SimpleNamespace(link=link,virtual_sock=sock,virtual_state=None,endpoint='tcp:127.0.0.1:27040',
                            cfg={'mavlink':{'connection':'tcp:127.0.0.1:27040'}},throttle=0.,virtual_deadline=0.,
                            lbl_digital=SimpleNamespace(setText=lambda text:None))
    context._bound_environment_status=MethodType(gui.PhoenixGUI._bound_environment_status,context)
    context._virtual_status_payload=MethodType(gui.PhoenixGUI._virtual_status_payload,context)
    context._send_virtual_status=MethodType(gui.PhoenixGUI._send_virtual_status,context)
    context._send_virtual_history=MethodType(gui.PhoenixGUI._send_virtual_history,context)
    poll=MethodType(gui.PhoenixGUI._poll_virtual_pilot,context)
    context._send_pilot_command=MethodType(gui.PhoenixGUI._send_pilot_command,context)
    def connect():
        link.connect();write(out/'SENDER_CONTRACT.json',link.telemetry_source_contract)
        link.master.mav.param_request_read_send(link.target_system,link.target_component,b'AHRS_EKF_TYPE',-1)
        sessions.append(link.telemetry_receiver.history.session_id)
        original=link.telemetry_receiver.ingest
        def ingest(message):
            record=original(message)
            if record is not None:packets.append(record)
            return record
        link.telemetry_receiver.ingest=ingest
    signal.signal(signal.SIGTERM,lambda *_:stop.__setitem__(0,True))
    try:
        connect();write(out/'WORKER_READY.json',{'session_id':sessions[-1],'same_production_GUI_and_link':True})
        deadline=time.monotonic()+70;reconnected=False;detached=False;next_send=0.;pilot_started=False;inhibited_attempts=0
        while not stop[0] and time.monotonic()<deadline and not (out/'STOP_WORKER').exists():
            if (out/'RECONNECT_REQUEST').exists() and not reconnected:
                link.master.close();connect();reconnected=True
                write(out/'RECONNECTED.json',{'session_id':sessions[-1]})
            if (out/'DETACH_REQUEST').exists() and not detached:
                link.master.close();context.link=None;detached=True;write(out/'DETACHED.json',{'detached':True})
            if not detached:
                link.drain();assert link.armed is False
                current=link.received_channels()
                imu=current['SCALED_IMU']['sample'];att=current['ATTITUDE_QUATERNION']['sample']
                if imu is not None and att is not None and imu['fields']['time_boot_ms']>=8000 and link.mode=='QHOVER' and not (out/'FRAME_SAMPLE.json').exists():
                    write(out/'FRAME_SAMPLE.json',{'channels':current,'legacy_attitude':dict(link.attitude),'observed_mode':link.mode})
            poll()
            if link.segments.current_record() is not None and link.segments.current_record()['state']=='ACTIVE' and (out/'BLOCK_GUI_REQUEST').exists() and not (out/'BLOCK_GUI_DONE').exists():
                time.sleep(.18);write(out/'BLOCK_GUI_DONE.json',{'blocked_s':.18});(out/'BLOCK_GUI_DONE').touch()
            from layer6.segments import sync_gui_segment
            sync_gui_segment(context)
            if context.virtual_state is not None:pilot_started=True
            if not detached and pilot_started and time.monotonic()>=next_send:
                v=context.virtual_state or dict(roll=0,pitch=0,yaw=0,throttle=0,command_discrete=[])
                assert all(v[k]==0 for k in ('roll','pitch','yaw','throttle'))
                assert v.get('command_mode') is None and v.get('command_discrete')==[]
                if context._send_pilot_command(0,0,0,0) is False:inhibited_attempts+=1
                next_send=time.monotonic()+.04
            time.sleep(.003)
        assert len(hearts)>=2 and len(packets)>256 and all(p['param_value']==10. for p in parameters)
        assert inhibited_attempts>=3
        write(out/'WORKER_RESULT.json',{'status':'PASS','raw_packets':len(packets),'disarmed_heartbeats':len(hearts),
            'post_closure_inhibited_attempts':inhibited_attempts,'actual_GUI_methods':True,'QApplication_created':False})
    finally:
        mavutil.mavlink_connection=factory
        if link.master:link.master.close()
        write(out/'SEGMENT_RECORDS.json',list(link.segments.records.values()))
        sock.close()
        with (out/'ALL_RECEIVED_MESSAGES.jsonl').open('w') as stream:
            for item in all_messages:stream.write(json.dumps(item)+'\n')
        write(out/'PARAMETER_READBACK.json',parameters);write(out/'SENT_MESSAGES.json',sent);write(out/'DISARMED_HEARTBEATS.json',hearts)
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
    defaults.write_text(defaults.read_text()+'\n# Private R6.01 frame probe: disarmed QHOVER, including the initial RC mode switch.\nINITIAL_MODE 18\n'+''.join('FLTMODE%d 18\n'%i for i in range(1,7)))
    binary=ap/'build/sitl/bin/arduplane';js=fs/'jsbsim/build/src/JSBSim';home=binding['mission_origin']
    location=','.join(str(home[k]) for k in ('latitude_deg','longitude_deg','terrain_elevation_msl_m','heading_deg'))
    argv=[str(binary),'--model','jsbsim:phoenix_v3','--home',location,'--speedup','1','--base-port','27040',
          '-I','93','--autotest-dir',str(private),'--defaults',str(defaults)]
    env=dict(os.environ,TDDT_NATIVE_BINDING_FILE=str(out/'BINDING.json'),TDDT_NATIVE_ENVIRONMENT_STATE=str(out/'ENVIRONMENT_NOT_PUBLISHED.json'),PATH=str(js.parent)+os.pathsep+os.environ['PATH'],PYTHONDONTWRITEBYTECODE='1',TMPDIR='/tmp')
    process=gui_worker=client=intake=None;owned=set();executables={};pages=[];statuses=[];paused=set();frame_checks={}
    with (out/'NATIVE_STDOUT.log').open('w') as native_log,(out/'GUI_HANDLER.log').open('w') as gui_log,(out/'INTAKE.log').open('w') as intake_log:
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
            wait_file('WORKER_READY.json');frame=wait_file('FRAME_SAMPLE.json',20)
            from scipy.spatial.transform import Rotation
            channels=frame['channels'];q=channels['ATTITUDE_QUATERNION']['contract']['normalized']['fields']['quaternion_wxyz']
            assert q is not None
            rotation=Rotation.from_quat([q[1],q[2],q[3],q[0]])
            imu=channels['SCALED_IMU']['contract']['normalized']['fields']
            gravity=rotation.apply([imu['accel_'+a+'_m_s2'] for a in 'xyz'])
            assert max(abs(gravity[i]-[0,0,-9.80665][i]) for i in range(3))<.5,gravity
            body_pitch=math.asin(max(-1.,min(1.,-rotation.apply([1.,0.,0.])[2])));display_pitch=frame['legacy_attitude']['pitch']
            assert abs(abs(body_pitch-display_pitch)-1.5707963267948966)<.05,(body_pitch,display_pitch)
            frame_checks={'body_pitch_rad':float(body_pitch),'VTOL_display_pitch_rad':display_pitch,'body_display_difference_rad':float(abs(body_pitch-display_pitch)),
                'specific_force_rotated_NED_m_s2':[float(v) for v in gravity],'body_quaternion_view_separated':True}
            write(out/'FRAME_CHECKS.json',frame_checks)
            client=Layer6Client(endpoint='127.0.0.1:27192',gui_endpoint='127.0.0.1:27191')
            from layer6.status import SCHEMA as STATUS_SCHEMA,encode_status,MAX_BYTES
            from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter
            from tddt_tdoc.online_imu import measured_intervals,CHANNELS as IMU_CHANNELS
            config=TDOCConfig.from_yaml(tdoc/'configs/tdoc.yaml')
            adapter=Layer6OnlineAdapter(config,None)
            def status(include=False):
                value=client.gui_status(timeout=1.,include_imu_samples=include)
                assert value is not None and value.get('ok') and value['schema']==STATUS_SCHEMA and value['schema_version']==1,value
                assert len(encode_status(value))<=MAX_BYTES
                statuses.append(value);return value
            from tddt_tdoc.layer6_bridge import Layer6Bridge,Layer6BridgeError
            from layer6.segments import validate_result,contract
            plain=status()
            assert adapter._fresh(plain) and plain['armed'] is False
            try:adapter._ingress_from_status(plain)
            except RuntimeError as exc:assert 'forcing' in str(exc)
            else:raise AssertionError('unpublished environment must reject ingress')
            intake=subprocess.Popen([sys.executable,'-B','-m','layer6.service','--listen','127.0.0.1:27192','--gui','127.0.0.1:27191','--rate-hz','30'],cwd=out,env=env,stdout=intake_log,stderr=subprocess.STDOUT,start_new_session=True)
            ready_until=time.monotonic()+8
            while '[L6] intake' not in (out/'INTAKE.log').read_text():
                assert intake.poll() is None and time.monotonic()<ready_until;time.sleep(.02)
            bridge=Layer6Bridge(client,fresh=adapter._fresh,confirmed=adapter._confirmed)
            results=[];faults=[]
            for i,deadline_s in enumerate((2,15,60)):
                pre=status();tx='R603-neutral-'+str(i)
                packet={'transaction_id':tx,'layer6_package':{'transaction_id':tx,'u_cmd':[0,0,0,0],
                    'tau_apply':time.time(),'tau_deadline':time.time()+deadline_s,
                    'provenance':{'source':'R603_private_disarmed_external_transport_probe'}}}
                try:value=bridge.exchange(packet,before=pre,timeout_s=2,segment_s=.1)
                except Exception:
                    write(out/'PARTIAL_EXCHANGES.json',results)
                    write(out/'FAILED_FAST_STATUS.json',client.segment_status(transaction_id=tx,command_instance_id=client._last_instance_id if hasattr(client,'_last_instance_id') else '0'*32,timeout=.2));raise
                proof=value['layer7_input']['ExecutionEvidence']['segment_closure_proof']
                assert validate_result(proof,tx,value['ack'].raw['command_instance_id'],contract({'execution_segment_s':.1}))
                assert proof['duration_bounds_s']['upper_s']<=.1+1e-9 and not proof['physical_stabilization_verified']
                results.append({'deadline_s':deadline_s,'ack':dict(value['ack'].raw),'status':value['status'],'layer7_input':value['layer7_input']})
                time.sleep(.15)
                post=status();assert not post['digital_pilot_active'] and post['armed'] is False
            # Intentional GUI scheduling stall: actual cap proof must fail, not gain deadline allowance.
            (out/'BLOCK_GUI_REQUEST').touch();pre=status();tx='R603-neutral-stall'
            packet={'transaction_id':tx,'layer6_package':{'transaction_id':tx,'u_cmd':[0,0,0,0],'tau_apply':time.time(),'tau_deadline':time.time()+60}}
            try:bridge.exchange(packet,before=pre,timeout_s=2,segment_s=.1)
            except (Layer6BridgeError,ValueError) as exc:faults.append({'case':'GUI_stall_180ms','expected_non_success':True,'reason':str(exc)})
            else:raise AssertionError('stall must not be labelled successful')
            write(out/'SEGMENT_EXCHANGES.json',results);write(out/'EXPECTED_FAULTS.json',faults)
            owned.update(helper.descendants(process.pid))
            for pid in [process.pid,*owned]:
                try:executables[str(pid)]=str(Path(f'/proc/{pid}/exe').resolve(strict=True))
                except (FileNotFoundError,ProcessLookupError,PermissionError):pass
            assert str(binary) in executables.values() and str(js) in executables.values()
            (out/'STOP_WORKER').touch();gui_worker.wait(timeout=10);assert gui_worker.returncode==0
            sent=json.loads((out/'SENT_MESSAGES.json').read_text())
            wire=[json.loads(line) for line in (out/'ALL_RECEIVED_MESSAGES.jsonl').read_text().splitlines()]
            received_hash={hashlib.sha256(bytes.fromhex(r['wire_hex'])).hexdigest() for r in wire}
            sent_hash={hashlib.sha256(bytes.fromhex(r['wire_hex'])).hexdigest() for r in sent}
            for value in results:
                e=value['layer7_input']['ExecutionEvidence'];r=e['segment_closure_proof'];c=r['close'];first=r['first_event']
                assert first['wire_sha256'] in sent_hash and c['request_wire_sha256'] in sent_hash
                assert first['barrier_reply']['wire_sha256'] in received_hash and c['reply']['wire_sha256'] in received_hash
                for ref in [first['pre_source_observation'],first['plant_time_bounds']['upper_source_observation'],c['pre_source'],c['upper_source']]:assert ref['wire_sha256'] in received_hash
                assert not any(p['mavpackettype']=='MANUAL_CONTROL' and p['command_instance_id']==r['context']['instance_id'] and p['sent_monotonic']>=r['write_blocked_monotonic'] for p in sent)
            result={'step':'R6.03','status':'PASS','argv':argv,'executables_observed_live':executables,
                'model_id':binding['model_id'],'binding_hash':binding['binding_hash'],'handoff_hash':binding['handoff_hash'],
                'forcing_file_sha256':binding['forcing_file_sha256'],'arduplane_sha256':sha(binary),'JSBSim_sha256':sha(js),
                'fourfoot_model_sha256':sha(private/'aircraft/phoenix_v3/phoenix_v3.xml'),
                'duration_bounds_s':[v['layer7_input']['ExecutionEvidence']['native_execution_duration_bounds_s'] for v in results],
                'qualified_segments':len(results),'expected_faults':faults,'raw_messages':len(wire),'neutral_external_probe_only':True,
                'real_intake_GUI_MAVLink_JSBSim':True,'flight_ready':False,'GNSS_denied_flight_validated':False,
                'physical_stabilization_verified':False,'full_0_1s_exposure_claimed':False,'no_write_after_local_cut':True}
            write(out/'NATIVE_SEGMENT_RESULT.json',result)
        finally:
            if intake and intake.poll() is None:
                intake.terminate()
                try:intake.wait(timeout=5)
                except subprocess.TimeoutExpired:intake.kill();intake.wait(timeout=5)
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
            write(out/'PUBLIC_STATUS_RESPONSES.json',statuses)
            after=protection.verify_manifest(baseline);write(out/'PROTECTION_AFTER.json',after);assert after['status']=='PASS'
            current_hash={str(p):sha(p) if p.is_file() else None for p in active};write(out/'ACTIVE_RUNTIME_AFTER.json',current_hash);assert current_hash==active_hash
    print(json.dumps({'step':'R6.03','status':'PASS','flight_ready':False}))

if __name__=='__main__':main()
