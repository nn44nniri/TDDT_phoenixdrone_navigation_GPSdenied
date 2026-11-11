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
import threading
import select
import socket
import uuid


def write(path,obj):
    temporary=path.with_suffix(path.suffix+'.tmp');temporary.write_text(json.dumps(obj,indent=2,allow_nan=False)+'\n');temporary.replace(path)
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def load_module(path,name):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m



class PublicProxy:
    """Private actual-UDP relay; drops/replays bytes without editing ACKs."""
    def __init__(self):
        self.front=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);self.front.bind(('127.0.0.1',27173))
        self.up=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);self.up.bind(('127.0.0.1',0))
        self.routes={};self.events=[];self.lock=threading.Lock();self.stop=False;self.drop_next=0
        self.thread=threading.Thread(target=self.loop,daemon=True);self.thread.start()
    def event(self,kind,data,**kw):
        row={'kind':kind,'monotonic':time.monotonic(),'wire_hex':data.hex(),'message':json.loads(data),**kw}
        with self.lock:self.events.append(row)
        return row
    def loop(self):
        while not self.stop:
            ready,_,_=select.select([self.front,self.up],[],[],.02)
            for sock in ready:
                data,peer=sock.recvfrom(65535);msg=json.loads(data);nonce=msg.get('intake_request_id')
                if sock is self.front:
                    self.routes[nonce]=peer;self.event('request',data)
                    self.up.sendto(data,('127.0.0.1',27172))
                else:
                    with self.lock:
                        drop=msg.get('type')=='cancel_ack' and self.drop_next>0
                        if drop:self.drop_next-=1
                    self.event('dropped_public_ACK' if drop else 'response',data)
                    if not drop and nonce in self.routes:self.front.sendto(data,self.routes[nonce])
    def drop_one(self):
        with self.lock:self.drop_next=1
    def last_cancel_request(self):
        with self.lock:return next(r for r in reversed(self.events) if r['kind']=='request' and r['message'].get('type')=='cancel')
    def replay(self,row):
        data=bytes.fromhex(row['wire_hex']);self.event('replayed_request',data);self.up.sendto(data,('127.0.0.1',27172))
    def close(self):
        self.stop=True;self.thread.join(timeout=2);self.front.close();self.up.close()

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
        assert endpoint=='tcp:127.0.0.1:27030'
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
    link=MavlinkLink({'mavlink':{'connection':'tcp:127.0.0.1:27030','heartbeat_timeout_seconds':15,'telemetry_history_capacity':256}})
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.bind(('127.0.0.1',27171));sock.setblocking(False)
    class GuiTap:
        def __init__(self):self.drop=0;self.hold=0;self.held=[];self.pending=[];self.events=[];self.last_ack=None
        def recvfrom(self,n):
            if self.pending:
                data,addr=self.pending.pop(0);self.events.append({'kind':'released_GUI_cancel','wire_hex':data.hex(),'peer':addr,'monotonic':time.monotonic()});return data,addr
            data,addr=sock.recvfrom(n);msg=json.loads(data)
            if msg.get('type')=='cancel':
                self.events.append({'kind':'GUI_cancel_request','wire_hex':data.hex(),'peer':addr,'monotonic':time.monotonic()})
                if self.hold:
                    self.hold-=1;self.held.append((data,addr));self.events.append({'kind':'held_GUI_cancel','wire_hex':data.hex(),'peer':addr,'monotonic':time.monotonic()});raise BlockingIOError
            return data,addr
        def sendto(self,data,addr):
            msg=json.loads(data)
            if msg.get('type')=='cancel_ack':
                self.last_ack=(bytes(data),addr)
                self.events.append({'kind':'dropped_GUI_ACK' if self.drop else 'GUI_ACK','wire_hex':bytes(data).hex(),'peer':addr,'monotonic':time.monotonic()})
                if self.drop:self.drop-=1;return len(data)
            return sock.sendto(data,addr)
    tap=GuiTap()
    context=SimpleNamespace(link=link,virtual_sock=tap,virtual_state=None,endpoint='tcp:127.0.0.1:27030',
                            cfg={'mavlink':{'connection':'tcp:127.0.0.1:27030'}},throttle=0.,virtual_deadline=0.,
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
        deadline=time.monotonic()+150;last_fault=None;reconnected=False;detached=False;next_send=0.;pilot_started=False;inhibited_attempts=0
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
            fault_file=out/'GUI_FAULT_REQUEST.json'
            if fault_file.is_file():
                fault=json.loads(fault_file.read_text())
                if fault['id']!=last_fault:
                    last_fault=fault['id'];op=fault['op']
                    if op=='drop_ack':tap.drop=1
                    elif op=='hold_cancel':tap.hold=1
                    elif op=='release_held':assert tap.held;tap.pending.extend(tap.held);tap.held=[]
                    elif op=='replay_last_ack':
                        assert tap.last_ack;data,addr=tap.last_ack;sock.sendto(data,addr)
                        tap.events.append({'kind':'replayed_old_GUI_ACK','wire_hex':data.hex(),'peer':addr,'monotonic':time.monotonic()})
                    else:raise AssertionError(op)
                    write(out/('GUI_FAULT_ACK_'+last_fault+'.json'),fault)
            poll()
            if context.virtual_state is not None:pilot_started=True
            if not detached and pilot_started and time.monotonic()>=next_send:
                v=context.virtual_state or dict(roll=0,pitch=0,yaw=0,throttle=0,command_discrete=[])
                assert all(v[k]==0 for k in ('roll','pitch','yaw','throttle'))
                assert v.get('command_mode') is None and v.get('command_discrete')==[]
                if context._send_pilot_command(0,0,0,0) is False:inhibited_attempts+=1
                next_send=time.monotonic()+.04
            time.sleep(.003)
        assert sum(p['mavpackettype'] in ('COMMAND_LONG','PARAM_REQUEST_READ') for p in sent)==20 and all(p['param_value']==10. for p in parameters) and len(parameters)>=2 and len(sessions)==2 and sessions[0]!=sessions[1]
        assert len(hearts)>=2 and len(packets)>256
        assert sum(p['mavpackettype']=='MANUAL_CONTROL' for p in sent)>=6
        assert sum(p['mavpackettype']=='TIMESYNC' for p in sent)==10
        assert inhibited_attempts>=5
        write(out/'WORKER_RESULT.json',{'status':'PASS','sessions':sessions,'raw_packets':len(packets),
             'outgoing_interval_requests':sum(p['mavpackettype']=='COMMAND_LONG' for p in sent),'parameter_reads':2,'only_interval_requests_parameter_reads_neutral_MANUAL_CONTROL_and_TIMESYNC_sent':True,'disarmed_heartbeats':len(hearts),
             'GUI_handler':'PhoenixGUI._poll_virtual_pilot + _send_virtual_history',
             'QApplication_created':False,'control_tick_called':False,'real_GUI_send_pilot_helper_called':True,'only_neutral_pilot':True,'post_cancel_inhibited_send_attempts':inhibited_attempts})
    finally:
        mavutil.mavlink_connection=factory
        if link.master:link.master.close()
        sock.close()
        with (out/'ALL_RECEIVED_MESSAGES.jsonl').open('w') as stream:
            for item in all_messages:stream.write(json.dumps(item)+'\n')
        write(out/'GUI_FAULT_EVENTS.json',tap.events);write(out/'PARAMETER_READBACK.json',parameters);write(out/'SENT_MESSAGES.json',sent);write(out/'DISARMED_HEARTBEATS.json',hearts)
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
    defaults.write_text(defaults.read_text()+'\n# Private R6.02 frame probe: disarmed QHOVER, including the initial RC mode switch.\nINITIAL_MODE 18\n'+''.join('FLTMODE%d 18\n'%i for i in range(1,7)))
    binary=ap/'build/sitl/bin/arduplane';js=fs/'jsbsim/build/src/JSBSim';home=binding['mission_origin']
    location=','.join(str(home[k]) for k in ('latitude_deg','longitude_deg','terrain_elevation_msl_m','heading_deg'))
    argv=[str(binary),'--model','jsbsim:phoenix_v3','--home',location,'--speedup','1','--base-port','27030',
          '-I','92','--autotest-dir',str(private),'--defaults',str(defaults)]
    env=dict(os.environ,TDDT_NATIVE_BINDING_FILE=str(out/'BINDING.json'),TDDT_NATIVE_ENVIRONMENT_STATE=str(out/'ENVIRONMENT_NOT_PUBLISHED.json'),PATH=str(js.parent)+os.pathsep+os.environ['PATH'],PYTHONDONTWRITEBYTECODE='1',TMPDIR='/tmp')
    process=gui_worker=client=intake=proxy=None;owned=set();executables={};pages=[];statuses=[];paused=set();frame_checks={}
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
            proxy=PublicProxy()
            client=Layer6Client(endpoint='127.0.0.1:27173',gui_endpoint='127.0.0.1:27171',timeout=1.2)
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
            plain=status();buffered=status(True)
            assert plain['imu_samples']==[] and not plain['imu_history']['requested']
            assert len(buffered['imu_samples'])>=40 and buffered['imu_history']['requested']
            assert adapter._fresh(buffered),buffered['telemetry_age_s']
            assert buffered['attitude']['frame']=='BODY_FRD_TO_NED' and buffered['imu']['frame']=='BODY_FRD'
            assert buffered['attitude']['source']=='MAVLink/ATTITUDE_QUATERNION'
            assert abs(buffered['attitude']['pitch']-buffered['display_attitude']['pitch'])>1.5
            state=adapter._state(buffered);assert state['imu_time_s']==buffered['imu']['plant_time_s']
            assert all(isinstance(state[k],(int,float)) for k in ('velocity_n_m_s','velocity_e_m_s','velocity_d_m_s','roll_rad','pitch_rad','yaw_rad'))
            last=buffered['imu_samples'][-40]['plant_time_s']
            steps=measured_intervals({'source':adapter.SOURCE,'imu_samples':buffered['imu_samples']},last,
                {'plant_time_s':buffered['imu']['plant_time_s'],'values':{target:buffered['imu'][original] for target,original in IMU_CHANNELS.items()}},.025)
            assert steps is not None and len(steps)>=30
            write(out/'TDOC_STATUS_CONSUMPTION.json',{'status':'PASS','fresh_check':True,'body_state_projected':True,
                'actual_replay_intervals':len(steps),'replay_elapsed_s':sum(dt for dt,_ in steps),
                'observation_only':True,'flight_ready':False})
            assert all(buffered[k] is None for k in ('applied_transaction_id','manual_sent_monotonic','command_first_sent_monotonic','command_first_sent_plant_time_s'))
            try:adapter._ingress_from_status(buffered)
            except RuntimeError as exc:
                assert 'forcing' in str(exc);write(out/'ENVIRONMENT_GATE_REJECTION.json',{'rejected':True,'reason':str(exc)})
            else:raise AssertionError('unpublished environment cannot pass production ingress')

            # Fault acceptance on actual service/GUI UDP; all pilot commands neutral.
            from layer6.command_correlation import verified_send_bounds
            from layer6.client import Layer6TimeoutError,Layer6RemoteError
            intake=subprocess.Popen([sys.executable,'-B','-m','layer6.service','--listen','127.0.0.1:27172',
                '--gui','127.0.0.1:27171','--rate-hz','5'],cwd=out,env=env,stdout=intake_log,stderr=subprocess.STDOUT,start_new_session=True)
            ready_until=time.monotonic()+10
            while '[L6] intake' not in (out/'INTAKE.log').read_text():
                assert intake.poll() is None and time.monotonic()<ready_until;time.sleep(.02)
            command_statuses=[];acks=[];cancel_checks=[];matrix=[];expected_failures=[]
            def fault(op):
                token=uuid.uuid4().hex;write(out/'GUI_FAULT_REQUEST.json',{'id':token,'op':op})
                return wait_file('GUI_FAULT_ACK_'+token+'.json',5)
            def command(tx):
                ack=client.submit_command([0,0,0,0],mode=None,transaction_id=tx,ttl_s=15.,
                    provenance={'source':'R6.02_private_disarmed_neutral_fault_probe'})
                acks.append(dict(ack.raw));time.sleep(.3);deadline=time.monotonic()+2.
                while time.monotonic()<deadline:
                    v=status();corr=v['command_correlation'];rec=corr.get('record')
                    if (rec and corr.get('available') and rec['context']['transaction_id']==tx
                        and rec['context']['instance_id']==ack.raw['command_instance_id'] and verified_send_bounds(v,tx) is not None):
                        assert v['armed'] is False;command_statuses.append(v);return v
                    time.sleep(.02)
                write(out/'FAILED_COMMAND_STATUS.json',v);raise AssertionError('command did not qualify')
            def cancel(tx):
                ack=client.cancel(transaction_id=tx,reason='R6_02_same_operation');cancel_checks.append(dict(ack.raw));return ack
            def fail_cancel(tx,exception,label):
                try:cancel(tx)
                except exception as exc:expected_failures.append({'case':label,'exception':type(exc).__name__,'reason':str(exc)})
                else:raise AssertionError('fault became a false successful cancellation: '+label)
            def stopped():
                a=status();time.sleep(.16);b=status()
                assert a['applied_transaction_id'] is None and b['applied_transaction_id'] is None
                assert a['manual_sent_monotonic']==b['manual_sent_monotonic']
            def alive(tx,instance):
                a=status();time.sleep(.16);b=status()
                assert b['applied_transaction_id']==tx and b['command_correlation']['record']['context']['instance_id']==instance
                assert b['manual_sent_monotonic']>a['manual_sent_monotonic']
                return b
            def passed(name,**evidence):matrix.append({'case':name,'status':'PASS',**evidence})
            a=command('R602-repeat');first=cancel('R602-repeat');op=proxy.last_cancel_request()
            again=cancel('R602-repeat');assert again.raw['cancelled'] and not again.raw['idempotent_replay']
            assert again.raw['gui_ack']['request_id']!=first.raw['gui_ack']['request_id'];stopped()
            proxy.replay(op);time.sleep(.08);stopped()
            passed('repeat_and_duplicate_RPC',original_operation=op['message'],fresh_repeat_GUI_ACK=True)
            command('R602-gui-loss');fault('drop_ack');fail_cancel('R602-gui-loss',Layer6RemoteError,'lost_GUI_ACK')
            old_op=proxy.last_cancel_request();fault('drop_ack');fault('replay_last_ack')
            fail_cancel('R602-gui-loss',Layer6RemoteError,'stale_GUI_ACK_cannot_replace_new_ACK')
            assert proxy.last_cancel_request()['message']==old_op['message']
            recovered=cancel('R602-gui-loss');assert recovered.raw['cancelled'] and recovered.raw['operation_attempts']==3
            assert recovered.raw['intake_request_id']==old_op['message']['intake_request_id'];stopped()
            passed('lost_and_stale_GUI_ACK',same_public_operation=True,actual_fresh_GUI_recovery=True)
            d=command('R602-public-loss');proxy.drop_one();fail_cancel(None,Layer6TimeoutError,'lost_public_ACK')
            dropped=next(e for e in reversed(proxy.events) if e['kind']=='dropped_public_ACK')
            e=command('R602-newer-public');eid=e['command_correlation']['record']['context']['instance_id']
            recovered=cancel(None)
            assert recovered.raw['cancelled'] and recovered.raw['idempotent_replay']
            assert recovered.raw['bound_transaction_id']=='R602-public-loss'
            assert recovered.raw['gui_ack']==dropped['message']['gui_ack']
            alive('R602-newer-public',eid);cancel('R602-newer-public');stopped()
            passed('lost_public_ACK_None_retry_with_newer_command',original_target_preserved=True,
                original_GUI_proof_exactly_replayed=True,newer_instance=eid)
            command('R602-pending-none');fault('drop_ack');fail_cancel(None,Layer6RemoteError,'pending_None_GUI_ACK_lost')
            g=command('R602-newer-pending');gid=g['command_correlation']['record']['context']['instance_id']
            negative=cancel(None)
            assert negative.ok and negative.raw['cancelled'] is False and negative.raw['bound_transaction_id']=='R602-pending-none'
            assert negative.raw['reason']=='newer_command_active_original_cancel_unconfirmed'
            alive('R602-newer-pending',gid);cancel('R602-newer-pending');stopped()
            passed('unconfirmed_old_None_with_newer_command',cancelled=False,newer_instance=gid)
            command('R602-delayed-GUI');fault('hold_cancel');fail_cancel('R602-delayed-GUI',Layer6RemoteError,'held_GUI_request')
            i=command('R602-newer-delayed');iid=i['command_correlation']['record']['context']['instance_id']
            fault('release_held');alive('R602-newer-delayed',iid)
            confirmed=cancel('R602-newer-delayed');assert confirmed.raw['cancelled'];stopped()
            passed('delayed_old_GUI_request_and_ACK',newer_preserved=True,newer_instance=iid)
            j=command('R602-reused');old=cancel('R602-reused');old_rpc=proxy.last_cancel_request()
            j2=command('R602-reused');newid=j2['command_correlation']['record']['context']['instance_id']
            proxy.replay(old_rpc);time.sleep(.1);alive('R602-reused',newid)
            assert old.raw['command_instance_id']!=newid
            new=cancel('R602-reused');assert new.raw['command_instance_id']==newid and new.raw['cancelled'];stopped()
            passed('same_transaction_new_instance_old_duplicate_RPC',old_instance=old.raw['command_instance_id'],newer_instance=newid)
            records=[v['command_correlation']['record'] for v in command_statuses];assert len(records)==10
            assert len(expected_failures)==5
            intake.terminate();intake.wait(timeout=5);assert intake.returncode==0
            write(out/'COMMAND_CORRELATION_STATUSES.json',command_statuses);write(out/'INTAKE_ACKS.json',acks)
            write(out/'CANCELLATION_CHECKS.json',{'status':'PASS','matrix':matrix,'expected_non_successes':expected_failures,
                'acks':cancel_checks,'false_successes':0,'same_live_service_GUI_scope':True,
                'physical_stabilization_verified':False,'intake_rate_hz':5.,'pilot_target_rate_hz':25.,'flight_ready':False})

            def read(**query):
                page=client.telemetry_history(timeout=1.,**query);assert page is not None,'public history timeout'
                assert len(encode_response(page))<=MAX_RESPONSE_BYTES
                pages.append(page);return page
            deadline=time.monotonic()+15
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
            # Stop only the owned private native processes; GUI read path stays alive.
            paused.update([process.pid,*helper.descendants(process.pid)])
            for pid in paused:os.kill(pid,signal.SIGSTOP)
            warm=read(limit=1);time.sleep(.75)
            stale=read(after_seq=max(0,warm['latest_sequence']-12),session_id=warm['session_id'])
            assert stale['samples'] and all(not c['freshness']['fresh'] and c['freshness']['reason']=='stale' for c in stale['sample_contracts'])
            assert stale['latest_sequence']==warm['latest_sequence']
            write(out/'LIVE_STALE_READBACK.json',stale)
            stale_status=status(True)
            assert not adapter._fresh(stale_status) and stale_status['telemetry_age_s']['SCALED_IMU']>.15
            write(out/'LIVE_STALE_STATUS.json',stale_status)
            for pid in paused:os.kill(pid,signal.SIGCONT)
            paused.clear()
            old_session=first['session_id'];(out/'RECONNECT_REQUEST').touch();new=wait_file('RECONNECTED.json',20)
            changed=read(after_seq=first['next_after_seq'],session_id=old_session)
            assert changed['reason']=='session_changed' and not changed['ok'] and changed['session_id']==new['session_id']
            fresh=read();assert fresh['ok'] and fresh['session_id']!=old_session
            time.sleep(.15)
            (out/'DETACH_REQUEST').touch();wait_file('DETACHED.json',5)
            assert read()['reason']=='not_connected'
            disconnected=status(True);assert not disconnected['mavlink_connected'] and disconnected['imu'] is None
            assert disconnected['imu_samples']==[] and not adapter._fresh(disconnected)
            owned.update(helper.descendants(process.pid))
            for pid in [process.pid,*owned]:
                try:executables[str(pid)]=str(Path(f'/proc/{pid}/exe').resolve(strict=True))
                except (FileNotFoundError,ProcessLookupError,PermissionError):pass
            assert str(binary) in executables.values() and str(js) in executables.values()
            (out/'STOP_WORKER').touch();gui_worker.wait(timeout=10);assert gui_worker.returncode==0
            assert client.telemetry_history(timeout=.05) is None,'stopped peer cannot yield a valid empty history'
            assert client.gui_status(timeout=.05,include_imu_samples=True) is None
            all_wire=[json.loads(line) for line in (out/'ALL_RECEIVED_MESSAGES.jsonl').read_text().splitlines()]
            by_hash={hashlib.sha256(bytes.fromhex(item['wire_hex'])).hexdigest():item for item in all_wire}
            for value in statuses:
                for meta in value['telemetry_metadata'].values():
                    item=by_hash[meta['wire_sha256']];message=dialect.MAVLink(None).parse_char(bytes.fromhex(item['wire_hex']))
                    assert message is not None
                    if meta['source_time'] is not None:assert meta['source_time']['value']==getattr(message,meta['source_time']['field'])
                if value['mavlink_connected']:
                    meta=value['telemetry_metadata']['GLOBAL_POSITION_INT']
                    message=dialect.MAVLink(None).parse_char(bytes.fromhex(by_hash[meta['wire_sha256']]['wire_hex']))
                    assert value['velocity_n_m_s']==message.vx*.01 and value['velocity_d_m_s']==message.vz*.01
            assert all(not v['flight_ready'] for v in statuses)
            assert any(v['status_quality']['execution_correlation_available'] for v in statuses)
            raw=[json.loads(line) for line in (out/'RECEIVED_CHANNELS.jsonl').read_text().splitlines()]
            index={(r['session_id'],r['history_sequence']):r for r in raw}
            for page in pages:
                for contract,record in zip(page.get('sample_contracts',[]),page['samples']):
                    assert contract['wire_sha256']==record['wire_sha256'] and contract['history_sequence']==record['history_sequence']
                    if record['type']=='SCALED_IMU':
                        assert abs(contract['normalized']['fields']['accel_x_m_s2']-record['fields']['xacc']*9.80665/1000.)<1e-12
                        assert contract['normalized']['frame']=='BODY_FRD'
                    assert contract['freshness']['source_age_s'] is None and not contract['freshness']['source_latency_verified']
                for record in page['samples']:
                    assert index[(record['session_id'],record['history_sequence'])]==record
                    message=dialect.MAVLink(None).parse_char(bytes.fromhex(record['wire_hex']))
                    assert message is not None and message.get_type()==record['type']
                    for field,value in record['fields'].items():
                        if value is not None:assert getattr(message,field)==value
                    if record['source_time'] is not None:assert record['source_time']['value']==getattr(message,record['source_time']['field'])
            result={'step':'R6.02','status':'PASS','argv':argv,'executables_observed_live':executables,
              'arduplane_sha256':sha(binary),'JSBSim_sha256':sha(js),
              'fourfoot_model_sha256':sha(private/'aircraft/phoenix_v3/phoenix_v3.xml'),
              'binding_id':binding['binding_id'],'binding_hash':binding['binding_hash'],'model_id':binding['model_id'],
              'handoff_hash':binding['handoff_hash'],'forcing_file_sha256':binding['forcing_file_sha256'],
              'frame_checks':frame_checks,'real_stale_contract_verified':True,'SI_frame_contract_verified':True,'freshness_scope':'receipt_and_source_progress_only',
              'status_API':'Layer6Client.gui_status(include_imu_samples=...)','status_schema':STATUS_SCHEMA,
              'status_responses_verified':len(statuses),'status_imu_samples':len(buffered['imu_samples']),
              'maximum_status_bytes':max(len(encode_status(v)) for v in statuses),'TDOC_status_consumer_verified':True,
              'TDOC_IMU_replay_intervals':len(steps),'unpublished_environment_rejected':True,
              'public_API':'Layer6Client.telemetry_history','production_GUI_handler_used':True,
              'public_datagrams_verified':len(pages),'unique_snapshot_samples':len(captured),'real_raw_packets':len(raw),
              'source_time_preserved':True,'actual_retention_overflow_verified':True,'frozen_pagination_verified':True,
              'real_reconnect_session_change_verified':True,'disconnected_and_timeout_verified':True,
              'largest_response_bytes':max(len(encode_response(p)) for p in pages),'max_response_bytes':MAX_RESPONSE_BYTES,
              'QApplication_created':False,'neutral_pilot_commands_sent':True,'arming_mode_or_PWM_commands_sent':False,'active_reset':False,
              'R5_stage_complete':False,'absolute_source_age_verified':False,'local_freshness_contract_verified':True,'GNSS_denied_flight_validated':False,'flight_ready':False}
            assert sha(private/'aircraft/phoenix_v3/phoenix_v3.xml')==sha(ap/'Tools/autotest/aircraft/phoenix_v3/phoenix_v3.xml')
            assert json.loads((out/'WORKER_RESULT.json').read_text())['status']=='PASS'
            sent=json.loads((out/'SENT_MESSAGES.json').read_text());sent_hash={hashlib.sha256(bytes.fromhex(v['wire_hex'])).hexdigest() for v in sent}
            for record in records:
                assert record['wire_sha256'] in sent_hash and record['barrier_reply']['wire_sha256'] in by_hash
                for ref in [record['pre_source_observation'],record['plant_time_bounds']['upper_source_observation'],*record['post_samples'].values()]:
                    assert ref['wire_sha256'] in by_hash
            for ack in cancel_checks:
                if ack.get('cancelled') is True:
                    instance=ack['command_instance_id'];cut=ack['gui_ack']['received_monotonic']
                    assert not any(p['mavpackettype']=='MANUAL_CONTROL' and p['command_instance_id']==instance
                        and p['sent_monotonic']>=cut for p in sent)
            events=json.loads((out/'GUI_FAULT_EVENTS.json').read_text())
            assert sum(v['kind']=='dropped_GUI_ACK' for v in events)==3
            assert any(v['kind']=='held_GUI_cancel' for v in events) and any(v['kind']=='released_GUI_cancel' for v in events)
            assert any(v['kind']=='replayed_old_GUI_ACK' for v in events)
            assert sum(v['kind']=='dropped_public_ACK' for v in proxy.events)==1
            result.update(command_generations=10,cancellation_fault_matrix=matrix,expected_non_successes=expected_failures,
                false_successes=0,GUI_ACK_drops=3,public_ACK_drops=1,actual_held_and_replayed_packets=True,
                package_API_ready=adapter.preflight()['ok'],service_and_GUI_restart_validated=False,
                physical_stabilization_verified=False,R6_stage_complete=False)
            write(out/'NATIVE_CANCEL_FAULT_RESULT.json',result)
        finally:
            for pid in paused:
                try:os.kill(pid,signal.SIGCONT)
                except ProcessLookupError:pass
            if intake and intake.poll() is None:
                intake.terminate()
                try:intake.wait(timeout=5)
                except subprocess.TimeoutExpired:intake.kill();intake.wait(timeout=5)
            if client:client.close()
            if proxy:
                proxy.close();write(out/'PUBLIC_PROXY_EVENTS.json',proxy.events)
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
            write(out/'PUBLIC_HISTORY_RESPONSES.json',pages);write(out/'PUBLIC_STATUS_RESPONSES.json',statuses)
            after=protection.verify_manifest(baseline);write(out/'PROTECTION_AFTER.json',after);assert after['status']=='PASS'
            current_hash={str(p):sha(p) if p.is_file() else None for p in active};write(out/'ACTIVE_RUNTIME_AFTER.json',current_hash)
            assert current_hash==active_hash
    print(json.dumps({'step':'R6.02','status':'PASS','cancellation_fault_acceptance_verified':True,'flight_ready':False}))


if __name__=='__main__':main()
