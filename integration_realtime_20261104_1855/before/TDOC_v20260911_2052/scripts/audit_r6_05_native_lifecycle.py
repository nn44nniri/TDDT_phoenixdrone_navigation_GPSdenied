#!/usr/bin/env python3
"""Actual TDOC → three terminals → FCU/environment/Qt GUI, disarmed lifecycle acceptance."""
import argparse,hashlib,json,os,re,shutil,subprocess,sys,time
from pathlib import Path

def write(path,obj):path.write_text(json.dumps(obj,indent=2,ensure_ascii=False,allow_nan=False)+'\n')
def main():
 p=argparse.ArgumentParser();p.add_argument('--output-dir',required=True,type=Path);a=p.parse_args();tdoc=Path(__file__).resolve().parents[1];root=tdoc.parent;out=a.output_dir.resolve();out.relative_to(tdoc/'reports/online');out.mkdir(exist_ok=False)
 sys.path[:0]=[str(tdoc/'src'),str(root/'layer6_v20261007_1032/src')]
 from tddt_tdoc.config import TDOCConfig
 from tddt_tdoc.orchestrator import TDOCOrchestrator
 from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter
 from tddt_tdoc.layer6_bridge import Layer6Bridge
 from layer6 import native_session as native
 from layer6.command_correlation import verified_send_bounds
 cfg=TDOCConfig.from_yaml(tdoc/'configs/tdoc.yaml');orch=TDOCOrchestrator(cfg);adapter=orch.adapters[6]
 folders=[];statuses=[];calls=[];sent=[];borrowed=None
 sentinel=subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(240)'],env={k:v for k,v in os.environ.items() if k not in ('TDDT_NATIVE_SESSION_ID','TDDT_NATIVE_ROLE')})
 def status(client):
  s=client.gui_status(timeout=.8,include_imu_samples=True)
  assert adapter._fresh(s) and s['armed'] is False
  statuses.append(s);return s
 def wait_for(client,predicate,timeout=4):
  end=time.monotonic()+timeout
  while time.monotonic()<end:
   s=status(client)
   if predicate(s):return s
  raise AssertionError('native status predicate timeout')
 def windows(label,session):
  text=subprocess.run(['xwininfo','-root','-tree'],capture_output=True,text=True,timeout=5).stdout
  selected=[line for line in text.splitlines() if ('FlightStack' in line and session[:8] in line) or 'TDDT Layer-6 Digital Pilot' in line]
  (out/('X11_'+label+'.txt')).write_text('\n'.join(selected)+'\n')
  assert len([s for s in selected if 'FlightStack' in s])==3 and any('TDDT Layer-6 Digital Pilot' in s for s in selected)
  for index,line in enumerate(selected):
   match=re.search(r'\b(0x[0-9a-f]+)\b',line)
   if match:subprocess.run(['import','-window',match.group(1),str(out/(label+'_window_'+str(index)+'.png'))],capture_output=True,timeout=8)
 try:
  result=orch.prepare_online_transport_session();write(out/'TRANSPORT_PREPARATION.json',result)
  assert result.get('ok'),{k:result.get(k) for k in ('stage','error','environment_rejection')}
  client=adapter._client;first_id=client._owned_native_session;first=native.RUNTIME/'sessions'/first_id;folders.append(first)
  before=status(client);windows('START',first_id);first_processes=native.status(first);write(out/'START_PROCESSES.json',first_processes)
  class CaptureSocket:
   def __init__(self,s):self.socket=s
   def __getattr__(self,k):return getattr(self.socket,k)
   def sendto(self,data,peer):sent.append({'message':json.loads(data),'wire_hex':bytes(data).hex(),'sent_monotonic':time.monotonic()});return self.socket.sendto(data,peer)
  client._sock=CaptureSocket(client._sock)
  reuse=orch.prepare_online_transport_session();assert reuse['ok'] and client._owned_native_session==first_id
  assert {p['pid'] for p in native.status(first)['processes']}=={p['pid'] for p in first_processes['processes']}
  write(out/'REUSE.json',reuse)
  def command(tx):
   ack=client.submit_command([0,0,0,0],mode=None,transaction_id=tx,ttl_s=60,provenance={'source':'R605_disarmed_transport_probe'})
   adapter._active_tx=tx
   s=wait_for(client,lambda s:s.get('applied_transaction_id')==tx and verified_send_bounds(s,tx) is not None)
   assert s['command_correlation']['record']['context']['instance_id']==ack.raw['command_instance_id']
   return ack,s
  aa,sa=command('R605-A')
  borrowed=Layer6OnlineAdapter(cfg,orch.adapters[5]);br=borrowed.prepare_online_transport_session();assert br['ok'] and not borrowed._owns_stack
  assert borrowed.reset_online_session('R605_borrowed_close')['ok'];borrowed=None
  assert status(client)['applied_transaction_id']=='R605-A' and native.status(first)['process_ready']
  ca=client.cancel(transaction_id='R605-A',reason='R605_A_done');assert ca.ok and ca.raw['cancelled']
  old_request=next(r for r in reversed(sent) if r['message'].get('type')=='cancel')
  repeat=client.cancel(transaction_id='R605-A',reason='R605_A_repeat');assert repeat.ok and repeat.raw['cancelled']
  ab,sb=command('R605-B')
  client._sock.sendto(bytes.fromhex(old_request['wire_hex']),client.endpoint)
  replay=json.loads(client._sock.recvfrom(65535)[0]);assert replay['cancelled'] and replay['transaction_id']=='R605-A'
  assert status(client)['applied_transaction_id']=='R605-B'
  cb=client.cancel(transaction_id='R605-B',reason='R605_B_done');assert cb.ok and cb.raw['cancelled'];adapter._active_tx=None
  empty=client.cancel(transaction_id=None,reason='R605_empty_boot_probe');assert not empty.raw['cancelled']
  old_none=next(r for r in reversed(sent) if r['message'].get('type')=='cancel')
  old_submit=next(r for r in sent if r['message'].get('transaction_id')=='R605-A' and r['message'].get('type')!='cancel')
  cut=status(client)['manual_sent_monotonic'];time.sleep(.2);assert status(client)['manual_sent_monotonic']==cut
  calls.extend([{'case':'targeted_cancel','ack':dict(ca.raw)},{'case':'repeat_cancel','ack':dict(repeat.raw)},
   {'case':'old_operation_replay_with_newer_command','ack':replay,'newer_command_preserved':True},{'case':'borrowed_close_preserves_active_latch','ok':True},{'case':'B_cancel','ack':dict(cb.raw)}])
  tx='R605-deadline';ack=client.submit({'transaction_id':tx,'u_cmd':[0,0,0,0],'tau_apply':time.time(),'tau_deadline':time.time()+.8});adapter._active_tx=tx
  time.sleep(1.2);expired=status(client);assert not expired['digital_pilot_active'] and expired['status_quality']['manual_output_inhibited']
  calls.append({'case':'packet_deadline','status':expired});adapter._active_tx=None
  segment_results=[]
  for i in range(3):
   pre=status(client);tx='R605-segment-'+str(i);packet={'transaction_id':tx,'layer6_package':{'transaction_id':tx,'u_cmd':[0,0,0,0],'tau_apply':time.time(),'tau_deadline':time.time()+5}}
   try:
    e=Layer6Bridge(client,fresh=adapter._fresh,confirmed=adapter._confirmed).exchange(packet,before=pre,timeout_s=2,segment_s=.1)
    segment_results.append({'ok':True,'layer7_input':e['layer7_input']})
   except Exception as exc:segment_results.append({'ok':False,'error':repr(exc)})
  write(out/'SEGMENT_PROBES.json',segment_results);assert any(r['ok'] for r in segment_results),segment_results
  control=adapter.prepare_online_control_session();write(out/'CONTROL_BLOCKED.json',control)
  assert not control['ok'] and control['transport_ready'] and not control['actuation_executed']
  restarted=client.restart_stack();second_id=client._owned_native_session;assert second_id!=first_id;second=native.RUNTIME/'sessions'/second_id;folders.append(second)
  assert not native.status(first)['alive'] and sentinel.poll() is None
  after=orch.prepare_online_transport_session();write(out/'RESTART_PREPARATION.json',after);assert after['ok'],after.get('error')
  new=status(client);assert new['session_id']!=before['session_id'] and not new['digital_pilot_active'] and new['applied_transaction_id'] is None
  assert after['environment_contract']['binding_hash']==result['environment_contract']['binding_hash']
  windows('RESTART',second_id)
  ac,sc=command('R605-C')
  stale=[]
  for original in (old_none,old_submit):
   client._sock.sendto(bytes.fromhex(original['wire_hex']),client.endpoint)
   rejected=json.loads(client._sock.recvfrom(65535)[0]);assert rejected['ok'] is False and 'native_session_id' in str(rejected)
   assert status(client)['applied_transaction_id']=='R605-C';stale.append(rejected)
  write(out/'OLD_BOOT_REJECTIONS.json',stale)
  assert client.cancel(transaction_id='R605-C',reason='R605_final_C').raw['cancelled'];adapter._active_tx=None
  write(out/'NATIVE_LIFECYCLE_RESULT.json',{'step':'R6.05','status':'PASS','first_session':first_id,'second_session':second_id,
   'first_telemetry_session':before['session_id'],'second_telemetry_session':new['session_id'],
   'real_terminals_per_start':3,'real_QApplication':True,'required_native_roles':list(native.ROLES),'six_native_roles_ready':True,
   'model_id':after['environment_contract']['model_id'],'binding_hash':after['environment_contract']['binding_hash'],
   'handoff_hash':after['environment_contract']['handoff_hash'],'forcing_file_sha256':after['environment_contract']['forcing_file_sha256'],
   'segment_probe_successes':sum(r['ok'] for r in segment_results),'segment_probe_failures':sum(not r['ok'] for r in segment_results),
   'borrowed_close_preserved_owner':True,'unrelated_sentinel_preserved':sentinel.poll() is None,'flight_ready':False,'physical_HOLD_qualified':False})
 finally:
  if borrowed:borrowed.reset_online_session('audit_finally')
  cleanup=adapter.reset_online_session('R605_audit_cleanup');write(out/'CLEANUP.json',cleanup)
  write(out/'PUBLIC_STATUS_RESPONSES.json',statuses);write(out/'CANCELLATION_CASES.json',calls);write(out/'INTAKE_SENT_MESSAGES.json',sent)
  for folder in folders:
   target=out/('SESSION_'+folder.name);shutil.copytree(folder,target,ignore=shutil.ignore_patterns('eeprom.bin','*.BIN'),dirs_exist_ok=True)
   assert not native.status(folder)['alive']
  assert sentinel.poll() is None
  sentinel.terminate();sentinel.wait(timeout=3);orch.close()
if __name__=='__main__':main()
