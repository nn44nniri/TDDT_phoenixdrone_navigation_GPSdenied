"""Expiry fences actual GUI methods; HOLD remains unverified without plant evidence."""
from copy import deepcopy
import time
from types import SimpleNamespace
import pytest
from test_r6_01_targeted_cancel import gui,state,PEER,A,B,executor
from layer6.contracts import DecisionEnvelope,PilotCommand
from layer6.validation import validate
from layer6.timeout_hold import hold_outcome,expire_gui_state
from layer6.virtual_pilot import VirtualPilotClient
from test_r5_02_public_history import FakeSocket
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter

@pytest.mark.parametrize('trigger',['TTL','deadline','invalid_deadline'])
def test_expiry_fences_latch_and_blocks_old_state_action_and_fallback(trigger):
 g,sent,actions=gui();g.virtual_sock.queue=[state()];g._poll_virtual_pilot()
 if trigger=='TTL':g.virtual_deadline=time.monotonic()-1
 else:g.virtual_state['admission_deadline_unix']=time.time()-1 if trigger=='deadline' else float('nan')
 g._poll_virtual_pilot();assert g.virtual_state is None and g._cancel_output_inhibited
 assert g._last_timeout_stop['ok'] and not g._last_timeout_stop['physical_stabilization_verified']
 assert g._send_pilot_command(0,0,300,0) is False and sent==[]
 g.virtual_sock.queue=[state(seq=100),{**state(seq=101),'type':'action','action':'qhover'}]
 g._poll_virtual_pilot();assert g.virtual_state is None and sent==[] and actions==[]
 g.virtual_sock.queue=[state('new',B,102)];g._poll_virtual_pilot()
 assert g.virtual_state['transaction_id']=='new' and not g._cancel_output_inhibited

def test_arriving_refresh_cannot_reanimate_an_already_expired_generation():
 g,sent,_=gui();g.virtual_sock.queue=[state()];g._poll_virtual_pilot();g.virtual_deadline=time.monotonic()-1
 g.virtual_sock.queue=[state(seq=2)];g._poll_virtual_pilot()
 assert g.virtual_state is None and not g._control_gate.admit(state(seq=100),PEER)

@pytest.mark.parametrize('ttl',[0,-1,True,float('nan'),float('inf'),'1'])
def test_invalid_TTL_never_leaves_an_executable_state(ttl):
 g,sent,_=gui();g.virtual_sock.queue=[{**state(),'ttl_s':ttl}];g._poll_virtual_pilot()
 assert g.virtual_state is None and g._send_pilot_command(0,0,300,0) is False and sent==[]

def test_service_deadline_expiry_uses_targeted_cancel_and_preserves_retry_identity():
 e,v=executor();old=e.active_instance_id;e.active.tau_deadline=time.time()-1
 assert e.tick() is False and e.active is None and e.active_instance_id is None
 assert v.events==[('cancel',{'transaction_id':'A','command_instance_id':old,'reason':'packet_invalid_or_deadline_expired'})]
 assert e._cancel_target==('A',old) and e.last_timeout_stop['cancelled']

def test_deadline_cancel_missing_ACK_remains_unconfirmed_and_never_replays():
 e,v=executor();v.ack=False;old=e.active_instance_id;e.active.tau_deadline=time.time()-1
 assert not e.tick() and not e.tick() and e.active is None
 assert e._cancel_target==('A',old) and not e.last_timeout_stop['cancelled'] and not e._cancel_confirmed

@pytest.mark.parametrize('field',['tau_apply','tau_deadline'])
@pytest.mark.parametrize('value',[float('nan'),float('inf'),-float('inf')])
def test_nonfinite_packet_times_reject(field,value):
 e=DecisionEnvelope('x',PilotCommand(0,0,0,0));setattr(e,field,value)
 assert not validate(e).ok and 'nonfinite_packet_time' in validate(e).reasons

def test_release_is_targeted_and_cannot_clear_newer_or_other_session():
 g,sent,_=gui();g.virtual_sock.queue=[state()];g._poll_virtual_pilot()
 g.virtual_sock.queue=[{'type':'release','seq':2}];g._poll_virtual_pilot();assert g.virtual_state is not None
 g.virtual_sock.queue=[{**state('new',B,3),'type':'state'}];g._poll_virtual_pilot()
 g.virtual_sock.queue=[{**state(seq=4),'type':'release'}];g._poll_virtual_pilot();assert g.virtual_state['command_instance_id']==B
 g.virtual_sock.queue=[{**state('new',B,5),'type':'release'}];g._poll_virtual_pilot()
 assert g.virtual_state is None and g._send_pilot_command(0,0,300,0) is False

def test_virtual_release_carries_actual_last_sent_state_target():
 c=VirtualPilotClient.__new__(VirtualPilotClient);c.addr=PEER;c.seq=0;c.sock=FakeSocket([])
 c.state(PilotCommand(0,0,0,0),transaction_id='x',command_instance_id=A,admission_deadline=time.time()+2)
 c.release();a,b=[m for m,_ in c.sock.sent]
 assert a['admission_deadline_unix']>time.time()
 assert b['control_session_id']==a['control_session_id'] and b['transaction_id']=='x' and b['command_instance_id']==A

@pytest.mark.parametrize('ack',[None,SimpleNamespace(ok=True,raw={'cancelled':True,'latch_cancelled':True,'gui_cancelled':True}),SimpleNamespace(ok=False,raw={})])
def test_zero_throttle_or_cancel_ACK_never_certifies_physical_HOLD(ack):
 r=hold_outcome(reason='timeout',cancel_ack=ack)
 assert r['status']=='HOLD_UNVERIFIED' and not r['physical_stabilization_verified'] and not r['safe_to_continue'] and not r['command_replay_authorized']

@pytest.mark.parametrize('cancel_mode',['good','missing','error'])
def test_TDOC_failed_control_reports_hold_unknown_and_retains_cancel_error(cancel_mode):
 a=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter);a._client=SimpleNamespace();a._active_tx='x'
 ack=SimpleNamespace(ok=True,raw={'cancelled':True,'latch_cancelled':True,'gui_cancelled':True})
 def cancel(**kw):
  if cancel_mode=='error':raise TimeoutError('GUI lost')
  if cancel_mode=='missing':return SimpleNamespace(ok=True,raw={'cancelled':False})
  return ack
 a._bridge=lambda:SimpleNamespace(cancel=cancel)
 a.layer5=SimpleNamespace(online_control_readiness=lambda:{'ok':False,'blockers':['provider_missing']})
 r=a.invoke({'transaction_id':'x','layer5_output':{'gates':{'FINAL':True}}})
 assert not r['ok'] and r['status']=='FAILED' and r['route']=='HOLD'
 assert r['hold_outcome']['status']=='HOLD_UNVERIFIED' and not r['hold_outcome']['physical_stabilization_verified']
 if cancel_mode!='good':assert a._active_tx=='x' and r['hold_outcome']['cancel_error']
 else:assert r['hold_outcome']['cancel_confirmed'] and a._active_tx is None
