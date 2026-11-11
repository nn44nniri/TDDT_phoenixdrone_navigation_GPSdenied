"""Targeted cancellation primitives; native evidence separately uses real IPC/SITL."""
import ast,json,socket,time,os
from pathlib import Path
from types import SimpleNamespace,MethodType
import pytest
from layer6.cancellation import GuiControlGate,SCHEMA
from layer6.executor import DigitalPilotExecutor
from layer6.contracts import DecisionEnvelope,PilotCommand
from layer6.client import Layer6Client,Layer6ClientError,Layer6RemoteError,Layer6TimeoutError
from test_r5_02_public_history import FakeSocket

PEER=('127.0.0.1',12345);SESSION='1'*32;A='2'*32;B='3'*32

def state(tx='A',instance=A,seq=1):
 from layer6.command_correlation import identity
 values={'roll':.1,'pitch':-.2,'throttle':.3,'yaw':0.}
 return {'type':'state','transaction_id':tx,'command_instance_id':instance,'control_session_id':SESSION,
  'seq':seq,'ttl_s':1.,**values,'command_id':identity(tx,values),'command_mode':None,'command_discrete':[]}

def request(tx='A',instance=A,seq=2):
 return {'type':'cancel','transaction_id':tx,'command_instance_id':instance,'control_session_id':SESSION,
  'seq':seq,'request_id':'4'*32,'reason':'TDOC_HOLD'}


class Virtual:
 def __init__(self):self.events=[];self.executor=None;self.ack=True
 def state(self,*a,**kw):self.events.append(('state',kw))
 def action(self,*a,**kw):self.events.append(('action',kw))
 def release(self):self.events.append(('release',{}))
 def cancel(self,**kw):
  assert self.executor.active is None
  self.events.append(('cancel',kw))
  return {'ok':self.ack,'cancelled':self.ack,'replay_fence_installed':True,**kw}

def executor():
 v=Virtual();e=DigitalPilotExecutor(v);v.executor=e
 e.accept(DecisionEnvelope('A',PilotCommand(.1,-.2,.3,0.),tau_deadline=time.time()+10));return e,v


def test_service_latch_is_stopped_before_waiting_for_GUI_and_no_more_ticks():
 e,v=executor();e.tick();old=e.active_instance_id;r=e.cancel(transaction_id='A')
 assert r['ok'] and r['cancelled'] and r['command_instance_id']==old
 assert e.active is None and not e.tick() and [x[0] for x in v.events]==['state','cancel']
 assert not r['physical_stabilization_verified']


def test_wrong_target_or_instance_does_not_clear_the_active_latch():
 e,v=executor();old=e.active
 assert e.cancel(transaction_id='B')['cancelled'] is False
 assert e.cancel(transaction_id='A',command_instance_id=B)['cancelled'] is False
 assert e.active is old and e.tick() and len(v.events)==1


def test_GUI_ACK_missing_cannot_be_reported_as_success_even_though_latch_is_stopped():
 e,v=executor();v.ack=False;r=e.cancel(transaction_id='A')
 assert not r['ok'] and not r['cancelled'] and r['latch_cancelled'] and e.active is None and not e.tick()


def test_none_snapshots_current_service_target_and_empty_latch_is_explicit():
 e,v=executor();r=e.cancel(transaction_id=None);assert r['cancelled'] and r['transaction_id']=='A'
 fresh=DigitalPilotExecutor(v);r=fresh.cancel();assert r['ok'] and not r['cancelled'] and r['reason']=='no_matching_latch'


def test_GUI_fence_rejects_queued_and_later_refreshes_of_cancelled_instance():
 gate=GuiControlGate();s=state();assert gate.admit(s,PEER)
 r=gate.fence(request(),PEER,s);assert r['ok'] and r['matched_active'] and r['inhibit_output']
 assert not gate.admit(state(seq=1),PEER) and not gate.admit(state(seq=100),PEER)
 assert gate.admit(state('B',B,101),PEER)


def test_GUI_cancellation_does_not_clear_a_different_current_generation():
 gate=GuiControlGate();b=state('B',B,3);assert gate.admit(b,PEER)
 r=gate.fence(request(seq=2),PEER,b)
 assert r['ok'] and not r['matched_active'] and not r['inhibit_output']
 assert gate.admit(state('B',B,4),PEER)


def test_cancel_before_delayed_state_installs_a_fence_even_if_target_absent():
 gate=GuiControlGate();r=gate.fence(request(),PEER,None)
 assert r['ok'] and r['replay_fence_installed'] and not gate.admit(state(seq=1),PEER)
 assert not gate.admit(state(seq=3),PEER)


def test_control_session_is_bound_to_its_original_peer():
 gate=GuiControlGate();assert gate.admit(state(),PEER)
 assert not gate.admit(state(seq=2),('127.0.0.1',54321))
 assert not gate.fence(request(),('127.0.0.1',54321),state())['ok']


def test_revocations_are_bounded_without_silent_eviction_or_reauthorization():
 gate=GuiControlGate();gate.admit(state(),PEER)
 for i in range(260):
  q=request(instance=f'{i:032x}',seq=i+2);assert gate.fence(q,PEER,None)['ok']
 row=gate.sessions[SESSION];assert row['closed'] and len(row['revoked'])==256
 assert not gate.admit(state('new',B,1000),PEER)


class GuiSocket:
 def __init__(self):self.queue=[];self.sent=[]
 def recvfrom(self,n):
  if not self.queue:raise BlockingIOError
  return json.dumps(self.queue.pop(0)).encode(),PEER
 def sendto(self,data,peer):self.sent.append((json.loads(data),peer))


def gui():
 path=Path(__file__).resolve().parents[2]/'layer6_v20261007_1032/src/gui/phoenix_f310_gui.py'
 tree=ast.parse(path.read_text());cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='PhoenixGUI')
 names={'_poll_virtual_pilot','_send_pilot_command'};nodes=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in names]
 namespace={'json':json,'time':time,'os':os};exec(compile(ast.Module(body=nodes,type_ignores=[]),'<production-GUI-cancel>','exec'),namespace)
 sent=[];actions=[];sock=GuiSocket();g=SimpleNamespace(virtual_sock=sock,virtual_state=None,virtual_deadline=0.,
  lbl_digital=SimpleNamespace(setText=lambda text:None),link=SimpleNamespace(send_manual=lambda *a,**kw:sent.append((a,kw))),
  _dispatch_action=lambda *a:actions.append(a),_send_virtual_status=lambda *a:None)
 for n in names:setattr(g,n,MethodType(namespace[n],g))
 return g,sent,actions


def test_actual_GUI_handler_clears_target_and_inhibits_inherited_throttle_writes():
 g,sent,_=gui();g.virtual_sock.queue=[state()];g._poll_virtual_pilot();g._send_pilot_command(-200,100,300,0);assert len(sent)==1
 g.virtual_sock.queue=[request()];g._poll_virtual_pilot()
 ack=g.virtual_sock.sent[-1][0];assert ack['cancelled'] and ack['manual_output_inhibited'] and ack['replay_fence_installed']
 assert g.virtual_state is None and g.virtual_deadline==0
 assert g._send_pilot_command(-200,100,300,0) is False and len(sent)==1
 g.virtual_sock.queue=[state(seq=3)];g._poll_virtual_pilot();assert g.virtual_state is None
 g.virtual_sock.queue=[state('B',B,4)];g._poll_virtual_pilot();g._send_pilot_command(-200,100,300,0)
 assert g.virtual_state['transaction_id']=='B' and len(sent)==2


def test_cancelled_discrete_action_is_not_dispatched_from_the_queue():
 g,_,actions=gui();g.virtual_sock.queue=[state(),request(),{**state(seq=3),'type':'action','action':'qhover'}]
 g._poll_virtual_pilot();assert actions==[]


def test_cancel_after_GUI_TTL_expiry_still_inhibits_the_last_command_output():
 g,sent,_=gui();g.virtual_sock.queue=[state()];g._poll_virtual_pilot();g.virtual_state=None;g.virtual_deadline=0
 g.virtual_sock.queue=[request()];g._poll_virtual_pilot()
 assert g._send_pilot_command(0,0,300,0) is False and sent==[]


def public_client(messages,monkeypatch):
 import layer6.client as m
 monkeypatch.setattr(m.uuid,'uuid4',lambda:SimpleNamespace(hex='a'*32))
 c=Layer6Client.__new__(Layer6Client);c._sock=FakeSocket(messages);c.endpoint=('127.0.0.1',31337);c.timeout=.01;return c


def public_reply(**kw):
 return {'type':'cancel_ack','schema':SCHEMA,'operation_schema':'Layer6-cancel-operation-v2','cancel_session_id':'a'*32,'cancel_sequence':1,'ok':True,'cancelled':True,'transaction_id':'A','requested_transaction_id':'A',
  'command_instance_id':A,'service_session_id':'c'*32,'bound_transaction_id':'A','bound_command_instance_id':A,'idempotent_replay':False,'intake_request_id':'a'*32,'latch_cancelled':True,'gui_cancelled':True,
  'gui_ack':{'ok':True,'schema':SCHEMA,'gui_session_id':'b'*32,'transaction_id':'A','command_instance_id':A,'cancelled':True,'replay_fence_installed':True},**kw}


def test_public_cancel_matches_peer_nonce_and_target_and_checks_GUI_evidence(monkeypatch):
 b=lambda d:json.dumps(d).encode();right=('127.0.0.1',31337)
 c=public_client([(b(public_reply()),('127.0.0.1',22)),(b(public_reply(intake_request_id='wrong')),right),(b(public_reply()),right)],monkeypatch)
 c._intake_instances={'A':A};ack=c.cancel(transaction_id='A');assert ack.ok and ack.raw['cancelled']
 assert c._sock.sent[0][0]['command_instance_id']==A and c._sock.timeout==7.


def test_missing_GUI_confirmation_is_not_a_successful_public_cancel(monkeypatch):
 c=public_client([(json.dumps(public_reply(gui_ack={})).encode(),('127.0.0.1',31337))],monkeypatch)
 with pytest.raises(Layer6ClientError,match='evidence'):c.cancel(transaction_id='A')


def test_cancel_timeout_and_service_failure_are_explicit(monkeypatch):
 c=public_client([],monkeypatch)
 with pytest.raises(Layer6TimeoutError):c.cancel(transaction_id='A')
 c=public_client([(json.dumps(public_reply(ok=False,cancelled=False,reason='gui_cancel_not_confirmed')).encode(),('127.0.0.1',31337))],monkeypatch)
 with pytest.raises(Layer6RemoteError,match='gui_cancel_not_confirmed'):c.cancel(transaction_id='A')


@pytest.mark.parametrize('tx,reason',[('', 'hold'),(True,'hold'),('A',''),('A','bad\nreason')])
def test_invalid_cancel_request_is_rejected_before_transmission(monkeypatch,tx,reason):
 c=public_client([],monkeypatch)
 with pytest.raises(ValueError):c.cancel(transaction_id=tx,reason=reason)
 assert c._sock.sent==[]
