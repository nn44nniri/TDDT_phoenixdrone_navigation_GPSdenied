"""Owned process cleanup and transport/control separation; no flight permission."""
import json,os,subprocess,sys,time,uuid
from pathlib import Path
from types import SimpleNamespace
import pytest
from layer6 import native_session as native
from layer6.client import Layer6Client,Layer6ClientError
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter


def test_stop_requires_client_ownership_and_close_does_not_stop_foreign_stack():
 c=Layer6Client.__new__(Layer6Client);c._owned_native_session=None
 with pytest.raises(Layer6ClientError,match='not owned'):c.stop_stack()
 with pytest.raises(Layer6ClientError,match='not owned'):c.restart_stack()


def test_reset_stops_owned_stack_even_if_cancel_failed():
 events=[];a=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter);a._owns_stack=True;a._active_tx='x'
 a._session_generation=0;a._option=lambda name,default=None:default
 a._client=SimpleNamespace(stop_stack=lambda:events.append('stop'),close=lambda:events.append('close'))
 a._cancel_active=lambda reason:(_ for _ in ()).throw(TimeoutError('lost GUI ACK'))
 result=a.reset_online_session('test')
 assert events==['stop','close'] and not result['ok'] and a._client is None


def test_cleanup_failure_retains_owner_and_retry_handle():
 a=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter);a._owns_stack=True;a._session_generation=0;a._option=lambda n,d=None:d;a._cancel_active=lambda _:None
 client=SimpleNamespace(stop_stack=lambda:(_ for _ in ()).throw(RuntimeError('busy')),close=lambda:pytest.fail('owner handle lost'))
 a._client=client;r=a.reset_online_session('test')
 assert not r['ok'] and r['cleanup_pending'] and a._client is client and a._owns_stack


def test_borrowed_transport_no_target_does_not_cancel_other_owner():
 a=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter);a._active_tx=None;a._client=SimpleNamespace();a._bridge=lambda:pytest.fail('broadcast cancel')
 assert a._cancel_active('close borrowed') is None


def test_managed_GUI_no_state_sends_no_manual_input():
 import ast
 from types import MethodType
 p=Path(__file__).resolve().parents[2]/'layer6_v20261007_1032/src/gui/phoenix_f310_gui.py'
 c=next(n for n in ast.parse(p.read_text()).body if isinstance(n,ast.ClassDef) and n.name=='PhoenixGUI')
 method=next(n for n in c.body if isinstance(n,ast.FunctionDef) and n.name=='_send_pilot_command');space={};exec(compile(ast.Module(body=[method],type_ignores=[]),'GUI-send','exec'),space)
 g=SimpleNamespace(tdoc_managed=True,virtual_state=None,link=SimpleNamespace(send_manual=lambda *a,**kw:pytest.fail('startup MANUAL sent')))
 assert MethodType(space['_send_pilot_command'],g)(0,0,0,0) is False


def test_actual_process_ownership_stop_preserves_untagged_sentinel(tmp_path,monkeypatch):
 monkeypatch.setattr(native,'RUNTIME',tmp_path)
 session=uuid.uuid4().hex;folder=tmp_path/'sessions'/session;folder.mkdir(parents=True)
 native.write(folder/'session.json',{'schema':native.SCHEMA,'session_id':session,'uid':os.getuid()})
 code='import time;time.sleep(30)'
 own=subprocess.Popen([sys.executable,'-B','-c',code],env=dict(os.environ,TDDT_NATIVE_SESSION_ID=session,TDDT_NATIVE_ROLE='test-owned'))
 foreign=subprocess.Popen([sys.executable,'-B','-c',code])
 try:
  deadline=time.monotonic()+2
  while time.monotonic()<deadline and not native.processes(session):time.sleep(.01)
  assert any(p['pid']==own.pid for p in native.processes(session))
  result=native.stop(folder);own.wait(timeout=2)
  assert result['stopped'] and foreign.poll() is None
 finally:
  for p in (own,foreign):
   if p.poll() is None:p.terminate()
   p.wait(timeout=2)


def test_registry_outside_owned_session_tree_rejected(tmp_path,monkeypatch):
 monkeypatch.setattr(native,'RUNTIME',tmp_path/'owned')
 with pytest.raises(ValueError):native.check_session(tmp_path/'foreign')


def test_no_global_process_kill_or_wipe_in_launcher():
 import run_flightstack as runner
 s=(Path(runner.__file__).parent/'run_flightstack.py').read_text()
 assert 'pkill' not in s and '"-w"' not in s and 'killpg' not in s
 assert 'native.stop(folder)' in s and 'session_id!=saved' in s


def test_workflow_starts_transport_before_control_readiness():
 p=Path(__file__).resolve().parents[1]/'scripts/run_training_workflows.py';s=p.read_text()
 start=s.index("transport_preparation=orch.prepare_online_transport_session()")
 assert start<s.index('control_readiness=orch.online_control_readiness()',start)

@pytest.mark.parametrize('current_forcing_valid',[True,False])
def test_channel_continuity_cannot_bypass_current_forcing(monkeypatch,current_forcing_valid):
 from tddt_tdoc.config import TDOCConfig
 from layer6 import status as st
 cfg=TDOCConfig.from_yaml(Path(__file__).resolve().parents[1]/'configs/tdoc.yaml')
 # Counter-only forcing fixture has no real Native session or history endpoint.
 # Continuous history is verified separately with actual encoded wire records.
 cfg.data['layers']['layer6']['online']['execution_profile']='legacy_unit_fixture'
 a=Layer6OnlineAdapter(cfg,SimpleNamespace());calls=[];counter=[0]
 a._option=lambda k,d=None:{'require_bound_environment':False,'start_if_needed':False,'require_live_channels':True,'require_fcu_estimator_ready':False,'startup_timeout_s':.15}.get(k,d)
 def sample(**kw):
  counter[0]+=1
  return {'plant_time_s':counter[0],'mode':'MANUAL','status_quality':{'native_session_id':'unit'},'latitude_deg':40,'longitude_deg':-105,'altitude_msl_m':1800}
 a._client=SimpleNamespace(ensure_mavlink=lambda **kw:None,wait_fcu_ready=sample,gui_status=sample,
  native_session_status=lambda:{'process_ready':True,'session_id':'unit'})
 a._get_client=lambda:a._client;a._fresh=lambda _:True
 monkeypatch.setattr(st,'validate_observation_status',lambda *a,**kw:{'ok':True}) # Isolate this counter; R5 tests the real channel validator.
 def ingress(s):
  calls.append(s['plant_time_s'])
  if len(calls)<3 or not current_forcing_valid:raise ValueError('forcing write pending')
  return {'simulation_time_s':s['plant_time_s']}
 a._ingress_from_status=ingress
 result=a.prepare_online_transport_session()
 assert bool(result['ok']) is current_forcing_valid
 if current_forcing_valid:assert len(calls)==3
 else:assert result['stage']=='live_channels'


def test_old_GUI_boot_cannot_clear_or_replace_current_state(monkeypatch):
 from test_r6_01_targeted_cancel import gui,state,request
 g,sent,_=gui();monkeypatch.setenv('TDDT_NATIVE_SESSION_ID','b'*32)
 current={**state(),'native_session_id':'b'*32};g.virtual_sock.queue=[current];g._poll_virtual_pilot();assert g.virtual_state is not None
 for typ in ('state','release','cancel','action'):
  old={**request(seq=50),'type':typ,'native_session_id':'a'*32,'action':'arm'}
  g.virtual_sock.queue=[old];g._poll_virtual_pilot();assert g.virtual_state['transaction_id']==current['transaction_id']
 assert g.virtual_sock.sent[-1][0]['cancelled'] is False


def test_virtual_sender_carries_native_boot_for_state_and_cancel(monkeypatch):
 from layer6.virtual_pilot import VirtualPilotClient
 from layer6.contracts import PilotCommand
 from test_r5_02_public_history import FakeSocket
 monkeypatch.setenv('TDDT_NATIVE_SESSION_ID','a'*32)
 v=VirtualPilotClient.__new__(VirtualPilotClient);v.addr=('127.0.0.1',999);v.seq=0;v.sock=FakeSocket([])
 v.state(PilotCommand(0,0,0,0),transaction_id='boot',command_instance_id='b'*32)
 assert v.sock.sent[0][0]['native_session_id']=='a'*32


def test_audit_FMU_archives_do_not_replace_active_binary_guard(tmp_path):
 from tddt_tdoc.orchestrator import vendored_external_binaries
 (tmp_path/'reports').mkdir();(tmp_path/'reports'/'audit.fmu').write_bytes(b'evidence')
 assert vendored_external_binaries(tmp_path)==[]
 assert vendored_external_binaries(tmp_path,[tmp_path/'reports'])
 (tmp_path/'src').mkdir();(tmp_path/'src'/'bad.fmu').write_bytes(b'active')
 assert vendored_external_binaries(tmp_path)==[str(tmp_path/'src'/'bad.fmu')]
