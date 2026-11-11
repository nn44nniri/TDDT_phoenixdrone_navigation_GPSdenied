"""Cancellation operation identity and failure acceptance; no flight/knowledge writes."""
from copy import deepcopy
import json,socket,time
from types import SimpleNamespace
import pytest
from layer6.cancellation import CancelJournal,OP_SCHEMA,SCHEMA
from layer6.client import Layer6Client,Layer6ClientError,Layer6TimeoutError,Layer6RemoteError
from layer6.contracts import DecisionEnvelope,PilotCommand
from test_r6_01_targeted_cancel import executor,A,B,gui,state,request,PEER

PUBLIC=('127.0.0.1',31337)

def operation(seq=1,tx=None,instance=None):
 r={'type':'cancel','operation_schema':OP_SCHEMA,'cancel_session_id':'a'*32,'cancel_sequence':seq,
  'intake_request_id':f'{seq:032x}','transaction_id':tx,'reason':'same_intent'}
 if instance:r['command_instance_id']=instance
 return r

def accept(e,tx='B'):
 e.accept(DecisionEnvelope(tx,PilotCommand(0.,0.,0.,0.),tau_deadline=time.time()+10));return e.active_instance_id


def test_duplicate_completed_none_operation_keeps_original_target_and_newer_command():
 e,v=executor();j=CancelJournal();r=operation();first=j.handle(r,PUBLIC,e);old=first['command_instance_id']
 newer=accept(e);count=len(v.events);again=j.handle(deepcopy(r),PUBLIC,e)
 assert again['cancelled'] and again['idempotent_replay'] and again['completion_semantics']=='original_operation_completed'
 assert again['bound_transaction_id']=='A' and again['bound_command_instance_id']==old
 assert e.active.transaction_id=='B' and e.active_instance_id==newer and len(v.events)==count and e.tick()
 assert again['gui_ack']==first['gui_ack']  # Actual earlier completion, never a manufactured GUI ACK.


def test_lost_GUI_ack_retry_uses_same_bound_generation_and_real_new_confirmation():
 e,v=executor();j=CancelJournal();r=operation();v.ack=False;first=j.handle(r,PUBLIC,e)
 assert not first['ok'] and not first['cancelled'] and first['latch_cancelled'] and e.active is None
 v.ack=True;second=j.handle(r,PUBLIC,e)
 assert second['cancelled'] and not second['idempotent_replay'] and second['operation_attempts']==2
 assert second['bound_command_instance_id']==first['bound_command_instance_id'] and len(v.events)==2


def test_pending_old_none_retry_does_not_wait_for_GUI_or_cancel_newer_command():
 e,v=executor();j=CancelJournal();r=operation();v.ack=False;j.handle(r,PUBLIC,e)
 newer=accept(e);count=len(v.events);second=j.handle(r,PUBLIC,e)
 assert second['ok'] and not second['cancelled'] and not second['gui_cancelled']
 assert second['reason']=='newer_command_active_original_cancel_unconfirmed'
 assert e.active_instance_id==newer and e.active.transaction_id=='B' and len(v.events)==count and e.tick()


def test_completed_old_cancel_cannot_delete_a_newer_instance_with_the_same_transaction():
 e,v=executor();j=CancelJournal();r=operation(tx='A',instance=e.active_instance_id);old=j.handle(r,PUBLIC,e)
 newer=accept(e,'A');again=j.handle(r,PUBLIC,e)
 assert again['cancelled'] and again['command_instance_id']==old['command_instance_id']!=newer
 assert e.active_instance_id==newer and e.tick()


def test_new_logical_none_request_may_cancel_the_new_current_target():
 e,v=executor();j=CancelJournal();j.handle(operation(),PUBLIC,e);newer=accept(e)
 second=j.handle(operation(2),PUBLIC,e)
 assert second['cancelled'] and second['bound_transaction_id']=='B' and second['bound_command_instance_id']==newer


def test_initial_no_matching_target_is_not_retargeted_on_duplicate():
 e,v=executor();j=CancelJournal();r=operation(tx='unknown');first=j.handle(r,PUBLIC,e)
 assert not first['cancelled'];newer=accept(e,'unknown');again=j.handle(r,PUBLIC,e)
 assert not again['cancelled'] and again['idempotent_replay'] and e.active_instance_id==newer


def test_conflicting_payload_under_same_operation_identity_is_rejected():
 e,v=executor();j=CancelJournal();r=operation();j.handle(r,PUBLIC,e);newer=accept(e)
 bad={**r,'transaction_id':'B'}
 with pytest.raises(ValueError,match='payload_conflict'):j.handle(bad,PUBLIC,e)
 assert e.active_instance_id==newer


def test_operation_is_bound_to_the_original_client_peer():
 e,v=executor();j=CancelJournal();r=operation();j.handle(r,PUBLIC,e);newer=accept(e)
 with pytest.raises(ValueError,match='peer_mismatch'):j.handle(r,('127.0.0.1',22),e)
 assert e.active_instance_id==newer


def test_evicted_operation_fails_closed_and_is_never_resolved_again():
 e,v=executor();j=CancelJournal(capacity=2);r=operation();j.handle(r,PUBLIC,e)
 for seq in (2,3):accept(e);j.handle(operation(seq),PUBLIC,e)
 newer=accept(e);assert len(j.operations)==2
 with pytest.raises(ValueError,match='stale_or_evicted'):j.handle(r,PUBLIC,e)
 assert e.active_instance_id==newer and e.tick()


def test_unseen_reordered_operation_below_watermark_is_rejected():
 e,v=executor();j=CancelJournal();j.handle(operation(2,tx='no-match'),PUBLIC,e)
 with pytest.raises(ValueError,match='stale_or_evicted'):j.handle(operation(1),PUBLIC,e)
 assert e.active is not None


@pytest.mark.parametrize('change',[{'cancel_sequence':True},{'cancel_sequence':0},{'cancel_session_id':'bad'},
 {'intake_request_id':'bad'},{'operation_schema':'old'},{'reason':''},{'unexpected':1},{'command_instance_id':'bad'}])
def test_invalid_operation_is_rejected_before_latch_mutation(change):
 e,v=executor();j=CancelJournal();before=e.active
 with pytest.raises(ValueError):j.handle({**operation(),**change},PUBLIC,e)
 assert e.active is before and v.events==[]


def test_delayed_old_GUI_cancel_cannot_clear_or_inhibit_newer_state():
 g,sent,_=gui();g.virtual_sock.queue=[state(),state('B',B,3)];g._poll_virtual_pilot();g._send_pilot_command(0,0,0,0)
 g.virtual_sock.queue=[request(seq=2)];g._poll_virtual_pilot();ack=g.virtual_sock.sent[-1][0]
 assert ack['cancelled'] and not ack['matched_active'] and not ack['manual_output_inhibited']
 assert g.virtual_state['transaction_id']=='B';g._send_pilot_command(0,0,0,0);assert len(sent)==2


class ReplySocket:
 def __init__(self):self.timeout=7.;self.requests=[];self.respond=None
 def gettimeout(self):return self.timeout
 def settimeout(self,v):self.timeout=v
 def sendto(self,data,peer):self.requests.append(json.loads(data))
 def recvfrom(self,n):
  if self.respond is None:raise socket.timeout()
  value=self.respond(self.requests[-1]);return json.dumps(value).encode(),PUBLIC


def client():
 c=Layer6Client.__new__(Layer6Client);c.endpoint=PUBLIC;c.timeout=.01;c._sock=ReplySocket();return c


def response(req,*,tx='A',instance=A,replay=False,**changes):
 return {**req,'type':'cancel_ack','requested_transaction_id':req['transaction_id'],'transaction_id':tx,
  'schema':SCHEMA,'ok':True,'cancelled':True,'latch_cancelled':True,'gui_cancelled':True,
  'service_session_id':'d'*32,'command_instance_id':instance,'bound_transaction_id':tx,'bound_command_instance_id':instance,
  'idempotent_replay':replay,'completion_semantics':'original_operation_completed' if replay else 'current_operation_observation',
  'gui_ack':{'ok':True,'schema':SCHEMA,'gui_session_id':'e'*32,'transaction_id':tx,'command_instance_id':instance,
   'cancelled':True,'replay_fence_installed':True},**changes}


def test_public_timeout_retry_keeps_original_operation_even_for_None():
 c=client()
 with pytest.raises(Layer6TimeoutError):c.cancel(reason='retry')
 first=deepcopy(c._sock.requests[-1]);c._sock.respond=lambda req:response(req,replay=True)
 ack=c.cancel(reason='retry')
 assert ack.raw['idempotent_replay'] and c._sock.requests[-1]==first and c._sock.timeout==7.
 assert c._cancel_pending=={}


def test_GUI_unconfirmed_service_reply_does_not_forget_pending_target():
 c=client();c._sock.respond=lambda req:response(req,ok=False,cancelled=False,reason='gui_cancel_not_confirmed')
 with pytest.raises(Layer6RemoteError):c.cancel(transaction_id='A')
 first=deepcopy(c._sock.requests[-1]);c._sock.respond=lambda req:response(req)
 assert c.cancel(transaction_id='A').raw['cancelled'] and c._sock.requests[-1]==first


def test_successful_method_call_then_new_call_is_a_new_operation():
 c=client();c._sock.respond=lambda req:response(req)
 c.cancel();first=c._sock.requests[-1];c._sock.respond=lambda req:response(req,tx='B',instance=B)
 c.cancel();second=c._sock.requests[-1]
 assert second['cancel_sequence']==first['cancel_sequence']+1 and second['intake_request_id']!=first['intake_request_id']


def test_delayed_old_public_ack_cannot_satisfy_new_operation():
 c=client();c._sock.respond=lambda req:response(req);c.cancel(transaction_id='A');old=response(c._sock.requests[-1])
 calls=[]
 def old_then_timeout(req):
  if not calls:calls.append(1);return old
  raise socket.timeout()
 c._sock.respond=old_then_timeout
 with pytest.raises(Layer6TimeoutError):c.cancel(transaction_id='B')
 assert len(c._cancel_pending)==1


@pytest.mark.parametrize('changes',[{'gui_ack':{}},{'idempotent_replay':True,'completion_semantics':'current_operation_observation'},
 {'bound_command_instance_id':B},{'service_session_id':'bad'}])
def test_forged_completion_without_consistent_original_proof_is_rejected(changes):
 c=client();c._sock.respond=lambda req:response(req,**changes)
 with pytest.raises(Layer6ClientError):c.cancel(transaction_id='A')
 assert c._cancel_pending


def test_changing_reason_during_retry_cannot_select_a_new_None_target():
 c=client()
 with pytest.raises(Layer6TimeoutError):c.cancel(reason='first reason')
 original=deepcopy(c._sock.requests[-1]);c._sock.respond=lambda req:response(req,replay=True)
 ack=c.cancel(reason='retry reason changed')
 assert ack.raw['cancelled'] and c._sock.requests[-1]==original
 assert c._sock.requests[-1]['reason']=='first reason'
