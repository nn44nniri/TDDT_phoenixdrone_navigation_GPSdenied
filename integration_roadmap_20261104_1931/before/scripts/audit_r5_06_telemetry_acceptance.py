#!/usr/bin/env python3
"""Private disarmed read-only SITL, actual-wire drop/replay faults and public API."""
import argparse,hashlib,importlib.util,json,os,shutil,signal,subprocess,sys,time,uuid
from pathlib import Path

def write(p,obj):
 t=p.with_suffix(p.suffix+'.tmp');t.write_text(json.dumps(obj,indent=2,allow_nan=False)+'\n');t.replace(p)
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def module(p,n):
 s=importlib.util.spec_from_file_location(n,p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m


def worker(root,out):
 import socket
 from types import SimpleNamespace,MethodType
 native=root/'layer6_v20261007_1032';sys.path[:0]=[str(native/'src'),str(native/'src/gui')]
 import phoenix_f310_gui as gui
 from layer6.mavlink_link import MavlinkLink
 from pymavlink import mavutil
 from pymavlink.dialects.v20 import ardupilotmega as dialect
 factory=mavutil.mavlink_connection;sent=[];wire=[];records=[];hearts=[];params=[];sessions=[];faults=[];pending=[];drops=set();stop=[False]
 def connect_factory(endpoint,**kw):
  assert endpoint=='tcp:127.0.0.1:26990';until=time.monotonic()+20;master=None
  while time.monotonic()<until:
   try:master=factory(endpoint,retries=0,**kw);break
   except (OSError,ConnectionError):time.sleep(.1)
  assert master is not None
  old_write=master.write
  def send(data):
   msg=dialect.MAVLink(None).parse_char(bytes(data));assert msg is not None
   assert msg.get_type()=='PARAM_REQUEST_READ' or msg.get_type()=='COMMAND_LONG' and msg.command==511
   sent.append({**msg.to_dict(),'wire_hex':bytes(data).hex()});return old_write(data)
  master.write=send;old_recv=master.recv_msg
  def receive():
   if pending:
    event=pending.pop(0);msg=dialect.MAVLink(None).parse_char(bytes.fromhex(event['wire_hex']))
    faults.append({**event,'delivered_monotonic':time.monotonic()});return msg
   msg=old_recv()
   if msg is not None and msg.get_type()!='BAD_DATA':
    row={'type':msg.get_type(),'wire_hex':bytes(msg.get_msgbuf()).hex(),'received_monotonic':time.monotonic(),
         'test_proxy_dropped':msg.get_type() in drops}
    wire.append(row)
    if msg.get_type()=='HEARTBEAT':
     assert not msg.base_mode & 128;hearts.append(msg.to_dict())
    if msg.get_type()=='PARAM_VALUE' and msg.param_id=='AHRS_EKF_TYPE':params.append(msg.to_dict())
    if row['test_proxy_dropped']:return None
   return msg
  master.recv_msg=receive;return master
 mavutil.mavlink_connection=connect_factory
 link=MavlinkLink({'mavlink':{'connection':'tcp:127.0.0.1:26990','heartbeat_timeout_seconds':15,'telemetry_history_capacity':256}})
 sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.bind(('127.0.0.1',27111));sock.setblocking(False)
 ctx=SimpleNamespace(link=link,virtual_sock=sock,virtual_state=None,virtual_deadline=0.,endpoint='tcp:127.0.0.1:26990',
  cfg={'mavlink':{'connection':'tcp:127.0.0.1:26990'}},throttle=0.)
 for name in ('_bound_environment_status','_virtual_status_payload','_send_virtual_status','_send_virtual_history'):
  setattr(ctx,name,MethodType(getattr(gui.PhoenixGUI,name),ctx))
 poll=MethodType(gui.PhoenixGUI._poll_virtual_pilot,ctx)
 def connect():
  link.connect();link.master.mav.param_request_read_send(link.target_system,link.target_component,b'AHRS_EKF_TYPE',-1)
  sessions.append(link.telemetry_receiver.history.session_id);original=link.telemetry_receiver.ingest
  def ingest(msg):
   r=original(msg)
   if r is not None:records.append(r)
   return r
  link.telemetry_receiver.ingest=ingest
 signal.signal(signal.SIGTERM,lambda *_:stop.__setitem__(0,True))
 try:
  connect();write(out/'SENDER_CONTRACT.json',link.telemetry_source_contract)
  write(out/'WORKER_READY.json',{'session_id':sessions[-1]});until=time.monotonic()+100;last=None
  while not stop[0] and time.monotonic()<until and not(out/'STOP_WORKER').exists():
   request=out/'FAULT_REQUEST.json';ack=None
   if request.is_file():
    command=json.loads(request.read_text())
    if command['id']!=last:
     last=command['id'];op=command['op'];ack={'id':last,'op':op,'session_id':sessions[-1]}
     if op=='reconnect_missing_imu':drops.add('SCALED_IMU');link.master.close();connect();ack['session_id']=sessions[-1]
     elif op=='drop_imu':drops.add('SCALED_IMU')
     elif op=='restore':drops.clear()
     elif op=='replay_older_imu':
      candidates=[r for r in wire if r['type']=='SCALED_IMU' and not r['test_proxy_dropped']]
      assert len(candidates)>=8
      old=candidates[-8];event={'kind':'test_proxy_replay_actual_older_wire_unmodified','wire_hex':old['wire_hex'],'original_received_monotonic':old['received_monotonic']}
      pending.append(event);ack.update(event)
     elif op=='reconnect':drops.clear();link.master.close();connect();ack['session_id']=sessions[-1]
     else:raise AssertionError(op)
   link.drain();assert link.armed is False
   if ack is not None:write(out/('FAULT_ACK_'+last+'.json'),ack)
   poll();time.sleep(.003)
  assert len(sessions)==3 and len(set(sessions))==3
  assert len(sent)==30 and sum(m['mavpackettype']=='PARAM_REQUEST_READ' for m in sent)==3
  assert len(params)>=3 and all(p['param_value']==10. for p in params)
  assert faults and any(r['test_proxy_dropped'] for r in wire)
  write(out/'WORKER_RESULT.json',{'status':'PASS','sessions':sessions,'raw_ingested_packets':len(records),
   'native_wire_messages':len(wire),'test_proxy_drops':sum(r['test_proxy_dropped'] for r in wire),
   'unmodified_older_wire_replays':len(faults),'pilot_or_arm_or_mode_or_PWM_sent':False,
   'real_GUI_handler_used':True,'QApplication_created':False,'disarmed_heartbeats':len(hearts)})
 finally:
  if link.master:link.master.close()
  sock.close();mavutil.mavlink_connection=factory
  for name,items in [('ALL_RECEIVED_MESSAGES.jsonl',wire),('RECEIVED_CHANNELS.jsonl',records)]:
   with (out/name).open('w') as stream:
    for row in items:stream.write(json.dumps(row,allow_nan=False)+'\n')
  write(out/'FAULT_PROXY_EVENTS.json',faults);write(out/'SENT_MESSAGES.json',sent);write(out/'PARAMETER_READBACK.json',params);write(out/'DISARMED_HEARTBEATS.json',hearts)


def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-dir',required=True,type=Path);parser.add_argument('--worker',action='store_true');a=parser.parse_args()
 tdoc=Path(__file__).resolve().parents[1];root=tdoc.parent;out=a.output_dir.resolve();out.relative_to(tdoc/'reports/online')
 if a.worker:worker(root,out);return
 out.mkdir(exist_ok=False);native=root/'layer6_v20261007_1032';fs=native/'FlightStack';ap=fs/'ardupilot'
 sys.path[:0]=[str(native/'src'),str(tdoc/'src')]
 from layer6.client import Layer6Client
 from layer6.status import validate_observation_status
 from layer6.feedback import build_execution_evidence
 from tddt_tdoc.config import TDOCConfig
 from tddt_tdoc.native_environment import prepare_binding
 from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter
 from tddt_tdoc.online_imu import measured_intervals,canonical_imu
 from pymavlink.dialects.v20 import ardupilotmega as dialect
 helper=module(tdoc/'scripts/audit_r4_03_native_forcing.py','r506_native_helpers');freeze=module(tdoc/'scripts/freeze_r1_02_state.py','r506_freeze')
 baseline=root/'recovery_baselines/R3_05_20261029_fixture_retirement/PROTECTED_MANIFEST.json'
 before=freeze.verify_manifest(baseline);write(out/'PROTECTION_BEFORE.json',before);assert before['status']=='PASS'
 active=[native/'runtime/tdoc_environment_binding.json',native/'runtime/bound_environment_state.json'];active_hash={str(p):sha(p) if p.is_file() else None for p in active};write(out/'ACTIVE_RUNTIME_BEFORE.json',active_hash)
 cfg=TDOCConfig.from_yaml(tdoc/'configs/tdoc.yaml');binding=prepare_binding(cfg,output_path=out/'BINDING.json')
 private=out/'private_autotest';private.mkdir();shutil.copytree(ap/'Tools/autotest/aircraft/phoenix_v3',private/'aircraft/phoenix_v3')
 defaults=out/'phoenix_v3.parm';shutil.copy2(ap/'Tools/autotest/default_params/phoenix_v3.parm',defaults)
 defaults.write_text(defaults.read_text()+'\n# Private disarmed observation probe.\nINITIAL_MODE 18\n'+''.join('FLTMODE%d 18\n'%i for i in range(1,7)))
 binary=ap/'build/sitl/bin/arduplane';js=fs/'jsbsim/build/src/JSBSim';origin=binding['mission_origin'];location=','.join(str(origin[k]) for k in ('latitude_deg','longitude_deg','terrain_elevation_msl_m','heading_deg'))
 argv=[str(binary),'--model','jsbsim:phoenix_v3','--home',location,'--speedup','1','--base-port','26990','-I','89','--autotest-dir',str(private),'--defaults',str(defaults)]
 env=dict(os.environ,TDDT_NATIVE_BINDING_FILE=str(out/'BINDING.json'),TDDT_NATIVE_ENVIRONMENT_STATE=str(out/'ENVIRONMENT_NOT_PUBLISHED.json'),PATH=str(js.parent)+os.pathsep+os.environ['PATH'],PYTHONDONTWRITEBYTECODE='1',TMPDIR='/tmp')
 proc=gui_worker=client=None;owned=set();paused=set();executables={};statuses=[];histories=[];cases={}
 with (out/'NATIVE_STDOUT.log').open('w') as native_log,(out/'GUI_HANDLER.log').open('w') as gui_log:
  try:
   proc=subprocess.Popen(argv,cwd=out,env=env,stdout=native_log,stderr=subprocess.STDOUT,start_new_session=True)
   gui_worker=subprocess.Popen([str(ap/'venv-ardupilot-debian13/bin/python'),'-B',str(Path(__file__).resolve()),'--worker','--output-dir',str(out)],cwd=out,env=env,stdout=gui_log,stderr=subprocess.STDOUT,start_new_session=True)
   def wait_file(name,seconds=30):
    until=time.monotonic()+seconds
    while time.monotonic()<until:
     assert proc.poll() is None and gui_worker.poll() is None,'private native/GUI failed'
     if(out/name).is_file():return json.loads((out/name).read_text())
     time.sleep(.02)
    raise AssertionError('wait timeout '+name)
   wait_file('WORKER_READY.json')
   client=Layer6Client(endpoint='127.0.0.1:27112',gui_endpoint='127.0.0.1:27111');adapter=Layer6OnlineAdapter(cfg,None)
   def status():
    v=client.gui_status(timeout=2.,include_imu_samples=True);assert v and v.get('ok'),v;statuses.append(v);return v
   def history(**kw):
    v=client.telemetry_history(timeout=1.,**kw);assert v is not None;histories.append(v);return v
   def fault(op):
    token=uuid.uuid4().hex;write(out/'FAULT_REQUEST.json',{'id':token,'op':op});return wait_file('FAULT_ACK_'+token+'.json',10)
   def healthy(seconds=20):
    until=time.monotonic()+seconds
    while time.monotonic()<until:
     v=status()
     if adapter._fresh(v) and v['mode']=='QHOVER':return v
     time.sleep(.04)
    write(out/'FAILED_HEALTHY_STATUS.json',v);raise AssertionError('fresh healthy observations not observed')
   def invalid(v):
    assert not adapter._fresh(v) and adapter._state(v)['runtime_ready']==0 and not adapter._snapshot(v)['ready']
    for label,call in [('TDOC_ingress',lambda:adapter._ingress_from_status(v)),('ExecutionEvidence',lambda:build_execution_evidence({'u_cmd':[0,0,0,0]},v))]:
     try:call()
     except (ValueError,RuntimeError) as exc:cases.setdefault('consumer_rejections',[]).append({'consumer':label,'reason':str(exc)})
     else:raise AssertionError('invalid telemetry accepted by '+label)
   def projection(v):
    # Observation-only probe of the actual TDOC projection, not a full forcing ingress.
    data={'ok':True,'released':True,'source':adapter.SOURCE,'state':adapter._state(v),'gui_state':v,'imu_samples':v['imu_samples']}
    return data,canonical_imu(data)
   first=healthy();time.sleep(.25);first=healthy();assert len(first['imu_samples'])>=6
   data,p=projection(first);cursor=first['imu_samples'][-5]['plant_time_s'];steps=measured_intervals(data,cursor,p,.025)
   assert steps and len(steps)>=4
   cases['healthy']={'fresh':True,'measured_replay_intervals':len(steps),'source_clock_preserved':True}
   try:adapter._ingress_from_status(first)
   except RuntimeError as exc:
    assert 'forcing' in str(exc);cases['unpublished_environment_rejected']=True
   else:raise AssertionError('environment gate bypassed')
   fault('reconnect_missing_imu');time.sleep(.25);missing=status();invalid(missing)
   assert missing['imu'] is None and missing['imu_samples']==[] and 'SCALED_IMU' not in missing['telemetry_metadata']
   page=history(channels=['SCALED_IMU']);assert page['samples']==[]
   cases['missing']={'readiness_rejected':True,'IMU_is_none':True,'history_empty_without_substitute':True}
   fault('restore');recovered=healthy();old=projection(recovered)[0]
   paused.update([proc.pid,*helper.descendants(proc.pid)])
   for pid in paused:os.kill(pid,signal.SIGSTOP)
   time.sleep(.75);stale=status();invalid(stale)
   assert stale['telemetry_metadata']['SCALED_IMU']['freshness']['reason']=='stale'
   cases['stale']={'readiness_rejected':True,'actual_pause_s':.75,'source_time_unchanged':stale['imu']['time_boot_ms']==recovered['imu']['time_boot_ms']}
   # Queued final packets may be drained at the pause boundary; raw bytes are verified below.
   for pid in paused:os.kill(pid,signal.SIGCONT)
   paused.clear();before_gap=healthy();cursor=before_gap['imu']['plant_time_s'];fault('drop_imu');time.sleep(.18);during_gap=status();invalid(during_gap)
   fault('restore');after_gap=healthy();gap_data,gap_packet=projection(after_gap)
   try:measured_intervals(gap_data,cursor,gap_packet,.025)
   except ValueError as exc:
    assert 'interval' in str(exc);cases['gap']={'current_sample_fresh_after_restore':True,'replay_rejected':True,'reason':str(exc),'cursor_s':cursor,'current_s':gap_packet['plant_time_s']}
   else:raise AssertionError('actual source gap accepted for integration')
   until=time.monotonic()+8
   while True:
    page=history(limit=1)
    if page['retention_gap']:break
    assert time.monotonic()<until,'retention overflow not observed';time.sleep(.04)
   assert page['lost_unfiltered_samples']>0
   cases['retention_gap']={'reported':True,'lost_unfiltered_samples':page['lost_unfiltered_samples']}
   before_reverse=history(limit=1);ack=fault('replay_older_imu');reverse=status();invalid(reverse)
   assert reverse['imu'] is None and reverse['imu_samples']==[]
   assert reverse['telemetry_metadata']['SCALED_IMU']['freshness']['reason']=='invalid_source_clock'
   page=history(after_seq=before_reverse['latest_sequence'],session_id=before_reverse['session_id'],channels=['SCALED_IMU'],limit=64)
   candidates=[s for s in page['samples'] if s['wire_hex']==ack['wire_hex'] and s['source_time_relation']=='reverse'];assert candidates
   replay=candidates[0];decoded=dialect.MAVLink(None).parse_char(bytes.fromhex(ack['wire_hex']))
   assert replay['source_time']['value']==decoded.time_boot_ms and replay['wire_sha256']==hashlib.sha256(bytes.fromhex(ack['wire_hex'])).hexdigest()
   time.sleep(.2);still=status();invalid(still)
   assert still['telemetry_metadata']['SCALED_IMU']['freshness']['reason']=='invalid_source_clock'
   cases['reverse']={'unmodified_actual_older_wire_replayed_by_test_proxy':True,'source_time_preserved':True,
    'wire_sha256':replay['wire_sha256'],'replayed_time_boot_ms':decoded.time_boot_ms,'readiness_rejected':True,'invalid_clock_latched_after_forward_samples':True}
   old_session=still['session_id'];fault('reconnect');last=healthy();assert last['session_id']!=old_session
   cases['reconnect']={'new_session':True,'fresh_observations_recovered':True,'FCU_clock_rebased':False}
   changed=history(after_seq=1,session_id=old_session);assert changed['reason']=='session_changed' and not changed['ok']
   owned.update(helper.descendants(proc.pid))
   for pid in [proc.pid,*owned]:
    try:executables[str(pid)]=str(Path(f'/proc/{pid}/exe').resolve(strict=True))
    except (OSError,PermissionError):pass
   assert str(binary) in executables.values() and str(js) in executables.values()
   (out/'STOP_WORKER').touch();gui_worker.wait(timeout=10);assert gui_worker.returncode==0
   assert client.gui_status(timeout=.05) is None and client.telemetry_history(timeout=.05) is None
   cases['timeout']={'no_valid_empty_response':True}
   raw=[json.loads(line) for line in (out/'ALL_RECEIVED_MESSAGES.jsonl').read_text().splitlines()];hashes={hashlib.sha256(bytes.fromhex(r['wire_hex'])).hexdigest():r for r in raw}
   records=[json.loads(line) for line in (out/'RECEIVED_CHANNELS.jsonl').read_text().splitlines()]
   for record in records:
    assert record['wire_sha256'] in hashes
    msg=dialect.MAVLink(None).parse_char(bytes.fromhex(record['wire_hex']));assert msg.get_type()==record['type']
    if record['source_time'] is not None:assert record['source_time']['value']==getattr(msg,record['source_time']['field'])
   assert all(not v['flight_ready'] for v in statuses)
   write(out/'NATIVE_ACCEPTANCE_RESULT.json',{'step':'R5.06','status':'PASS','argv':argv,'executables_observed_live':executables,
    'model_id':binding['model_id'],'binding_id':binding['binding_id'],'binding_hash':binding['binding_hash'],
    'handoff_hash':binding['handoff_hash'],'forcing_file_sha256':binding['forcing_file_sha256'],
    'arduplane_sha256':sha(binary),'JSBSim_sha256':sha(js),'fourfoot_model_sha256':sha(private/'aircraft/phoenix_v3/phoenix_v3.xml'),
    'cases':cases,'raw_packets_verified':len(records),'native_wire_messages':len(raw),'status_responses':len(statuses),'history_responses':len(histories),
    'fault_scope':'private receiver-side drop and unmodified replay only; no sensor timestamp editing',
    'environment_not_published':True,'full_production_ingress_or_mission_started':False,
    'AHRS_EKF_TYPE_readback':10,'absolute_source_age_verified':False,'GNSS_denied_flight_validated':False,
    'pilot_commands_sent':False,'active_reset':False,'flight_ready':False})
  finally:
   for pid in paused:
    try:os.kill(pid,signal.SIGCONT)
    except ProcessLookupError:pass
   if client:client.close()
   if gui_worker and gui_worker.poll() is None:
    gui_worker.terminate()
    try:gui_worker.wait(timeout=5)
    except subprocess.TimeoutExpired:gui_worker.kill();gui_worker.wait(timeout=5)
   if proc:
    owned.update(helper.descendants(proc.pid))
    for pid in sorted(owned,reverse=True):
     try:os.kill(pid,signal.SIGTERM)
     except ProcessLookupError:pass
    if proc.poll() is None:proc.terminate()
    try:proc.wait(timeout=5)
    except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
   write(out/'PUBLIC_STATUS_RESPONSES.json',statuses);write(out/'PUBLIC_HISTORY_RESPONSES.json',histories);write(out/'CASES.json',cases)
   after=freeze.verify_manifest(baseline);write(out/'PROTECTION_AFTER.json',after);assert after['status']=='PASS'
   current={str(p):sha(p) if p.is_file() else None for p in active};write(out/'ACTIVE_RUNTIME_AFTER.json',current);assert current==active_hash
 print(json.dumps({'step':'R5.06','status':'PASS','flight_ready':False}))

if __name__=='__main__':main()
