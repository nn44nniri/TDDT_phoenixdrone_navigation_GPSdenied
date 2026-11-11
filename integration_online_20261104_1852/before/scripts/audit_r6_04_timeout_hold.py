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
    native=root/'layer6_v20261007_1032';mode=json.loads((out/'MODE.json').read_text())['mode']
    sys.path[:0]=[str(native/'src'),str(native/'src/gui')]
    import phoenix_f310_gui as gui
    from layer6.mavlink_link import MavlinkLink
    from pymavlink import mavutil
    from pymavlink.dialects.v20 import ardupilotmega as dialect
    assert gui.MavlinkLink is MavlinkLink
    factory=mavutil.mavlink_connection;sent=[];hearts=[];packets=[];all_messages=[];sessions=[];parameters=[];stop=[False]
    def instrument(endpoint,**kwargs):
        assert endpoint=='tcp:127.0.0.1:27050'
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
                or kind=='MANUAL_CONTROL' and message.y in (0,120) and all(getattr(message,k)==0 for k in ('x','z','r','buttons'))
                or kind=='TIMESYNC' and message.tc1==0 and message.ts1>0),'non-neutral probe frame'
            if kind=='MANUAL_CONTROL':assert message.target==master.target_system
            sent.append({**message.to_dict(),'wire_hex':bytes(data).hex(),'sent_monotonic':time.monotonic(),
                'active_transaction_id':(context.virtual_state or {}).get('transaction_id'),
                'command_instance_id':(context.virtual_state or {}).get('command_instance_id')})
            if kind=='MANUAL_CONTROL':
                tx=(context.virtual_state or {}).get('transaction_id')
                if tx and not (out/('WRITE_'+tx+'.json')).exists():write(out/('WRITE_'+tx+'.json'),sent[-1])
            return raw_write(data)
        master.write=transport;raw_recv=master.recv_msg
        def receive():
            message=raw_recv()
            if message is not None and message.get_type()!='BAD_DATA':
                all_messages.append({'type':message.get_type(),'wire_hex':bytes(message.get_msgbuf()).hex(),
                                     'source_system':message.get_srcSystem(),'source_component':message.get_srcComponent(),'received_monotonic':time.monotonic()})
            if message is not None and message.get_type()=='PARAM_VALUE' and True:parameters.append(message.to_dict())
            if message is not None and message.get_type()=='HEARTBEAT':
                assert not message.base_mode & 128,'probe must remain disarmed'
                hearts.append({**message.to_dict(),'wire_hex':bytes(message.get_msgbuf()).hex()})
            return message
        master.recv_msg=receive;return master
    mavutil.mavlink_connection=instrument
    link=MavlinkLink({'mavlink':{'connection':'tcp:127.0.0.1:27050','heartbeat_timeout_seconds':15,'telemetry_history_capacity':256}})
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.bind(('127.0.0.1',27211));sock.setblocking(False)
    context=SimpleNamespace(link=link,virtual_sock=sock,virtual_state=None,endpoint='tcp:127.0.0.1:27050',
                            cfg={'mavlink':{'connection':'tcp:127.0.0.1:27050'}},throttle=0.,virtual_deadline=0.,
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
        for name in ('RC_OVERRIDE_TIME','FS_GCS_ENABL','FS_SHORT_ACTN','FS_LONG_ACTN','FS_LONG_TIMEOUT','RC1_MIN','RC1_MAX','RC1_TRIM'):
            link.master.mav.param_request_read_send(link.target_system,link.target_component,name.encode(),-1)
        link.master.mav.command_long_send(link.target_system,link.target_component,511,0,dialect.MAVLINK_MSG_ID_RC_CHANNELS,50000,0,0,0,0,0)
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
                if imu is not None and att is not None and imu['fields']['time_boot_ms']>=8000 and link.mode==mode and not (out/'FRAME_SAMPLE.json').exists():
                    write(out/'FRAME_SAMPLE.json',{'channels':current,'legacy_attitude':dict(link.attitude),'observed_mode':link.mode})
            poll()
            if link.segments.current_record() is not None and link.segments.current_record()['state']=='ACTIVE' and (out/'BLOCK_GUI_REQUEST').exists() and not (out/'BLOCK_GUI_DONE').exists():
                time.sleep(.18);write(out/'BLOCK_GUI_DONE.json',{'blocked_s':.18});(out/'BLOCK_GUI_DONE').touch()
            from layer6.segments import sync_gui_segment
            sync_gui_segment(context)
            if context.virtual_state is not None:pilot_started=True
            if not detached and pilot_started and time.monotonic()>=next_send:
                v=context.virtual_state or dict(roll=0,pitch=0,yaw=0,throttle=0,command_discrete=[])
                assert v['roll'] in (0,.12) and all(v[k]==0 for k in ('pitch','yaw','throttle'))
                assert v.get('command_mode') is None and v.get('command_discrete')==[]
                if context._send_pilot_command(0,int(round(v['roll']*1000)),0,0) is False:inhibited_attempts+=1
                next_send=time.monotonic()+.04
            time.sleep(.003)
        assert len(hearts)>=2 and len(packets)>256 and any(p['param_id']=='AHRS_EKF_TYPE' and p['param_value']==10. for p in parameters)
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
    parser.add_argument('--worker',action='store_true');parser.add_argument('--mode',choices=['QHOVER','QSTABILIZE','FBWA'],default='QHOVER');args=parser.parse_args()
    tdoc=Path(__file__).resolve().parents[1];root=tdoc.parent;out=args.output_dir.resolve()
    out.relative_to(tdoc/'reports/online')
    if args.worker:worker(root,out);return
    out.mkdir(exist_ok=False);write(out/'MODE.json',{'mode':args.mode});native=root/'layer6_v20261007_1032';fs=native/'FlightStack';ap=fs/'ardupilot'
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
    mode_number={'QHOVER':18,'QSTABILIZE':17,'FBWA':5}[args.mode]
    defaults.write_text(defaults.read_text()+'\n# Private R6.01 frame probe: disarmed QHOVER, including the initial RC mode switch.\nINITIAL_MODE '+str(mode_number)+'\n'+''.join('FLTMODE%d %d\n'%(i,mode_number) for i in range(1,7)))
    binary=ap/'build/sitl/bin/arduplane';js=fs/'jsbsim/build/src/JSBSim';home=binding['mission_origin']
    location=','.join(str(home[k]) for k in ('latitude_deg','longitude_deg','terrain_elevation_msl_m','heading_deg'))
    argv=[str(binary),'--model','jsbsim:phoenix_v3','--home',location,'--speedup','1','--base-port','27050',
          '-I','94','--autotest-dir',str(private),'--defaults',str(defaults)]
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
            client=Layer6Client(endpoint='127.0.0.1:27212',gui_endpoint='127.0.0.1:27211')
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
            plain=status();assert adapter._fresh(plain) and plain['armed'] is False and plain['mode']==args.mode
            from layer6.status import validate_observation_status
            from layer6.client import Layer6RemoteError
            from tddt_tdoc.layer6_bridge import Layer6Bridge,Layer6BridgeError
            policy={'scope':'sampled disarmed fourfoot ground quiescence only','groundspeed_max_m_s':.2,'velocity_norm_max_m_s':.2,
                'body_rate_max_rad_s':.1,'altitude_span_max_m':.1,'quaternion_drift_max_rad':.03,'motor_output_max':1000,
                'minimum_source_span_s':1.,'minimum_samples':4,'mode':args.mode,'airborne_HOLD_qualified':False}
            write(out/'ACCEPTANCE_POLICY.json',policy)
            try:adapter._ingress_from_status(plain)
            except RuntimeError as exc:assert 'forcing' in str(exc)
            else:raise AssertionError('unpublished forcing must not authorize control')
            intake=subprocess.Popen([sys.executable,'-B','-m','layer6.service','--listen','127.0.0.1:27212','--gui','127.0.0.1:27211','--rate-hz','30'],cwd=out,env=env,stdout=intake_log,stderr=subprocess.STDOUT,start_new_session=True)
            ready_until=time.monotonic()+8
            while '[L6] intake' not in (out/'INTAKE.log').read_text():
                assert intake.poll() is None and time.monotonic()<ready_until;time.sleep(.02)
            cases=[];windows=[]
            def ground_window(case):
                values=[];violations=[];started=time.monotonic()
                while time.monotonic()-started<1.4 or len(values)<4:
                    v=status();assert validate_observation_status(v)['ok'] and v['mode']==args.mode and v['armed'] is False
                    if v['landed_state']!='ON_GROUND':violations.append('not_on_ground')
                    if v['groundspeed_m_s']>policy['groundspeed_max_m_s']:violations.append('groundspeed')
                    if math.sqrt(sum(v['velocity_'+a+'_m_s']**2 for a in 'ned'))>policy['velocity_norm_max_m_s']:violations.append('velocity')
                    if max(abs(v['attitude'][a+'_rate']) for a in ('roll','pitch','yaw'))>policy['body_rate_max_rad_s']:violations.append('body_rate')
                    if max(v['servo_output_raw']['servo'+str(i)+'_raw'] for i in (3,4))>policy['motor_output_max']:violations.append('motor_output')
                    assert not v['digital_pilot_active'] and v['status_quality']['manual_output_inhibited'] and not v['status_quality']['physical_HOLD_verified']
                    values.append(v);time.sleep(.12)
                span=values[-1]['plant_time_s']-values[0]['plant_time_s']
                if span<policy['minimum_source_span_s']:violations.append('source_span')
                alt=[v['altitude_msl_m'] for v in values]
                if max(alt)-min(alt)>policy['altitude_span_max_m']:violations.append('altitude_drift')
                q=values[0]['attitude']['quaternion_wxyz'];angles=[2*math.acos(min(1.,abs(sum(a*b for a,b in zip(q,v['attitude']['quaternion_wxyz']))))) for v in values]
                if max(angles)>policy['quaternion_drift_max_rad']:violations.append('quaternion_drift')
                window={'case':case,'sample_count':len(values),'source_span_s':span,'altitude_span_m':max(alt)-min(alt),'quaternion_drift_rad':max(angles),
                    'received_start_monotonic':started,'received_end_monotonic':time.monotonic(),'violations':sorted(set(violations)),
                    'observed_ground_quiescent':not violations,'airborne_HOLD_qualified':False,'physical_stabilization_verified':False,'statuses':values}
                windows.append(window);return window
            # Service/GUI admission deadline, with real nonzero lateral stick and zero throttle, disarmed.
            tx='R604-deadline';deadline=time.time()+.7
            packet={'transaction_id':tx,'u_cmd':[.12,0,0,0],'tau_apply':time.time(),'tau_deadline':deadline}
            ack=client.submit(packet);wait_file('WRITE_'+tx+'.json',5);time.sleep(1.)
            post=status();stop=post['status_quality']['last_timeout_stop']
            assert stop and stop['transaction_id']==tx and stop['target_absent'] and stop['hold_status']=='HOLD_UNVERIFIED'
            try:client.submit(packet)
            except Layer6RemoteError:pass
            else:raise AssertionError('expired packet cannot be reaccepted')
            time.sleep(3.3);ground_window('packet_deadline')
            cases.append({'case':'packet_deadline','tx':tx,'instance':ack.raw['command_instance_id'],'deadline_unix':deadline,'stop':stop,'expired_resubmit_rejected':True})
            # Stop only the service; GUI and native plant continue. Resume must drain the actual local stop notice before any refresh.
            tx='R604-GUI-TTL';packet={'transaction_id':tx,'u_cmd':[.12,0,0,0],'tau_apply':time.time(),'tau_deadline':time.time()+15}
            ack=client.submit(packet);wait_file('WRITE_'+tx+'.json',5);os.kill(intake.pid,signal.SIGSTOP);paused.add(intake.pid)
            time.sleep(.7);post=status();stop=post['status_quality']['last_timeout_stop']
            assert stop['transaction_id']==tx and stop['reason']=='GUI_TTL_expired' and stop['target_absent']
            os.kill(intake.pid,signal.SIGCONT);paused.remove(intake.pid);time.sleep(.25)
            cancelled=client.cancel(transaction_id=tx,reason='R604_TTL_probe_complete');assert cancelled.ok and cancelled.raw['cancelled']
            time.sleep(3.3);ground_window('GUI_TTL_service_paused')
            cases.append({'case':'GUI_TTL_service_paused','tx':tx,'instance':ack.raw['command_instance_id'],'stop':stop,'cancel_ack':dict(cancelled.raw),'resumed_service_did_not_replay':True})
            # A confirmation budget shorter than a 0.1s segment cannot be claimed as execution success.
            pre=status();tx='R604-confirmation';packet={'transaction_id':tx,'layer6_package':{'transaction_id':tx,'u_cmd':[0,0,0,0],'tau_apply':time.time(),'tau_deadline':time.time()+15}}
            bridge=Layer6Bridge(client,fresh=adapter._fresh,confirmed=adapter._confirmed)
            try:bridge.exchange(packet,before=pre,timeout_s=.01,segment_s=.1)
            except (Layer6BridgeError,TimeoutError) as exc:error=str(exc)
            else:raise AssertionError('10ms budget cannot prove a 0.1s execution segment')
            time.sleep(3.3);ground_window('confirmation_timeout')
            cases.append({'case':'confirmation_timeout','tx':tx,'expected_non_success':True,'reason':error,'physical_HOLD_inferred':False})
            write(out/'TIMEOUT_CASES.json',cases);write(out/'GROUND_WINDOWS.json',windows)
            owned.update(helper.descendants(process.pid))
            for pid in [process.pid,*owned]:
                try:executables[str(pid)]=str(Path(f'/proc/{pid}/exe').resolve(strict=True))
                except (FileNotFoundError,ProcessLookupError,PermissionError):pass
            assert str(binary) in executables.values() and str(js) in executables.values()
            (out/'STOP_WORKER').touch();gui_worker.wait(timeout=10);assert gui_worker.returncode==0
            sent=json.loads((out/'SENT_MESSAGES.json').read_text());wire=[json.loads(line) for line in (out/'ALL_RECEIVED_MESSAGES.jsonl').read_text().splitlines()]
            decoded=[(row,dialect.MAVLink(None).parse_char(bytes.fromhex(row['wire_hex']))) for row in wire]
            params={p['param_id']:p['param_value'] for p in json.loads((out/'PARAMETER_READBACK.json').read_text())}
            assert params['RC_OVERRIDE_TIME']==3 and params['FS_GCS_ENABL']==0 and params['AHRS_EKF_TYPE']==10,params
            rc=[(row,msg) for row,msg in decoded if msg is not None and msg.get_type()=='RC_CHANNELS'];assert len(rc)>50
            override=[]
            for case in cases[:2]:
                writes=[p for p in sent if p['mavpackettype']=='MANUAL_CONTROL' and p['command_instance_id']==case['instance']]
                assert writes and all(p['z']==0 and p['y']==120 for p in writes)
                cut=case['stop']['received_monotonic'];assert not any(p['sent_monotonic']>=cut for p in writes)
                observed_inputs=[(row,msg) for row,msg in rc if writes[0]['sent_monotonic']<=row['received_monotonic']<=writes[-1]['sent_monotonic']+.4 and msg.chan1_raw!=params['RC1_TRIM']]
                assert observed_inputs,'nonzero lateral command must be visible in actual FCU RC input'
                after=[(row,msg) for row,msg in rc if row['received_monotonic']>writes[-1]['sent_monotonic']]
                retained=[(row,msg) for row,msg in after if msg.chan1_raw!=params['RC1_TRIM']]
                reverted=[(row,msg) for row,msg in after if msg.chan1_raw==params['RC1_TRIM']]
                assert retained and reverted
                last_retained=next((pair for pair in reversed(retained) if pair[0]['received_monotonic']<reverted[0][0]['received_monotonic']),None)
                assert last_retained
                # Source-clock interval for FCU reversion; this is not a receipt-clock duration claim.
                last_send_bounds=[m.time_boot_ms*.001 for row,m in rc if row['received_monotonic']<=writes[-1]['sent_monotonic']]
                assert last_send_bounds
                interval={'last_retained_source_s':last_retained[1].time_boot_ms*.001,'first_baseline_source_s':reverted[0][1].time_boot_ms*.001,
                    'last_send_pre_RC_source_s':last_send_bounds[-1], 'RC_OVERRIDE_TIME_readback_s':params['RC_OVERRIDE_TIME'],
                    'retention_after_GUI_cut_observed':any(row['received_monotonic']>cut for row,msg in retained),'FCU_release_is_not_GUI_cancel':True}
                assert interval['retention_after_GUI_cut_observed']
                override.append({'case':case['case'],'instance':case['instance'],'stop_monotonic':cut,'actual_writes':len(writes),**interval})
            write(out/'FCU_RC_TIMEOUT_EVIDENCE.json',override)
            # Dense actual wire rates within each capture window; no sparse-sample safety claim.
            from layer6.timeout_hold import hold_outcome
            for window in windows:
                rates=[max(abs(m.rollspeed),abs(m.pitchspeed),abs(m.yawspeed)) for row,m in decoded if m is not None and m.get_type()=='ATTITUDE_QUATERNION' and window['received_start_monotonic']<=row['received_monotonic']<=window['received_end_monotonic']]
                assert rates
                window['dense_body_rate_samples']=len(rates);window['maximum_dense_body_rate_rad_s']=max(rates)
                if max(rates)>policy['body_rate_max_rad_s']:window['violations']=sorted(set(window['violations']+['dense_body_rate']))
                window['observed_ground_quiescent']=not window['violations']
                window['HOLD_outcome']=hold_outcome(reason='R604_observed_plant_window')
                assert window['HOLD_outcome']['status']=='HOLD_UNVERIFIED' and not window['HOLD_outcome']['safe_to_continue']
            write(out/'GROUND_WINDOWS.json',windows)
            result={'step':'R6.04','status':'PASS','mode':args.mode,'argv':argv,'executables_observed_live':executables,
                'model_id':binding['model_id'],'binding_hash':binding['binding_hash'],'handoff_hash':binding['handoff_hash'],'forcing_file_sha256':binding['forcing_file_sha256'],
                'arduplane_sha256':sha(binary),'JSBSim_sha256':sha(js),'fourfoot_model_sha256':sha(private/'aircraft/phoenix_v3/phoenix_v3.xml'),
                'parameters':params,'native_cases':len(cases),'ground_windows':len(windows),'raw_messages':len(wire),'GUI_replay_after_stop':False,
                'scope':'timeout non-replay and no false physical HOLD; unsafe/unknown ground windows explicitly rejected','ground_quiescent_windows_qualified':sum(w['observed_ground_quiescent'] for w in windows),'ground_windows_with_violations':sum(bool(w['violations']) for w in windows),'dense_plant_rate_checks':True,'physical_stabilization_verified':False,'airborne_HOLD_qualified':False,
                'flight_ready':False,'GNSS_denied_flight_validated':False,'no_ARM_DISARM_mode_PWM_or_override_writes':True,'QApplication_created':False}
            write(out/'NATIVE_TIMEOUT_HOLD_RESULT.json',result)
        finally:
            for pid in paused:
                try:os.kill(pid,signal.SIGCONT)
                except ProcessLookupError:pass
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
    print(json.dumps({'step':'R6.04','mode':args.mode,'status':'PASS','flight_ready':False}))

if __name__=='__main__':main()
