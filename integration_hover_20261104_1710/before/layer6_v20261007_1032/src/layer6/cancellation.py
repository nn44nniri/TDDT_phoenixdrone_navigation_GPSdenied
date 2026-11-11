"""Targeted cancellation of service/GUI command generations, not FCU stabilization."""
import time,uuid
from .command_correlation import valid_tx

SCHEMA='Layer6-targeted-cancel-v1'

def token(value):return isinstance(value,str) and len(value)==32 and all(c in '0123456789abcdef' for c in value)
def reason_text(value):return isinstance(value,str) and 1<=len(value)<=256 and all(ord(c)>=32 for c in value)
def key(state):
    if not isinstance(state,dict):return None
    return (state.get('control_session_id'),state.get('transaction_id'),state.get('command_instance_id'))


class GuiControlGate:
    """Bounded, non-evicting sender fences. Full capacity fails closed.

    Sequence fences reject queued old packets; revoked instances reject even
    later refreshes of a cancelled latch. Identity is local IPC, not signed peer
    attestation. A GUI restart establishes a new cancellation session.
    """
    def __init__(self):
        self.gui_session_id=uuid.uuid4().hex;self.sessions={};self.last_state_key=None

    def session(self,msg,peer):
        session=msg.get('control_session_id');seq=msg.get('seq')
        if not token(session) or type(seq) is not int or seq<=0:raise ValueError('invalid_control_identity')
        row=self.sessions.get(session)
        if row is None:
            if len(self.sessions)>=32:raise ValueError('control_session_capacity')
            row={'peer':peer,'sequence':0,'revoked':set(),'closed':False};self.sessions[session]=row
        if row['peer']!=peer:raise ValueError('control_peer_mismatch')
        return row

    def admit(self,msg,peer):
        try:
            row=self.session(msg,peer);target=(msg.get('transaction_id'),msg.get('command_instance_id'))
            if not valid_tx(target[0]) or not token(target[1]) or row['closed'] or target in row['revoked'] or msg['seq']<=row['sequence']:return False
            row['sequence']=msg['seq']
            if msg['type']=='state':self.last_state_key=key(msg)
            return True
        except ValueError:return False

    def fence(self,msg,peer,current):
        response={'type':'cancel_ack','schema':SCHEMA,'request_id':msg.get('request_id'),
            'transaction_id':msg.get('transaction_id'),'command_instance_id':msg.get('command_instance_id'),
            'control_session_id':msg.get('control_session_id'),'gui_session_id':self.gui_session_id,
            'scope':'GUI_COMMAND_LATCH_AND_REPLAY_FENCE_ONLY','physical_stabilization_verified':False}
        try:
            if not token(msg.get('request_id')) or not valid_tx(msg.get('transaction_id')) or not token(msg.get('command_instance_id')) or not reason_text(msg.get('reason')):
                raise ValueError('invalid_cancel_request')
            row=self.session(msg,peer);target=(msg['transaction_id'],msg['command_instance_id'])
            # Never evict a revocation and thereby authorize an old refresh.
            if target not in row['revoked']:
                if len(row['revoked'])>=256:row['closed']=True
                else:row['revoked'].add(target)
            row['sequence']=max(row['sequence'],msg['seq'])
            identity=key(msg);matched=key(current)==identity
            inhibit=matched or current is None and self.last_state_key==identity
            return {**response,'ok':True,'matched_active':matched,'inhibit_output':inhibit,
                'replay_fence_installed':True,'received_monotonic':time.monotonic()}
        except ValueError as exc:return {**response,'ok':False,'cancelled':False,'error':str(exc)}


OP_SCHEMA='Layer6-cancel-operation-v2'


class CancelJournal:
    """Process-local idempotence, with one immutable target per operation.

    Completed proof can be replayed as historical completion, never as a new
    claim about the currently active command. Evicted operations are rejected
    using non-evicting per-client sequence watermarks; they cannot be retargeted.
    """
    def __init__(self,capacity=128):
        from collections import OrderedDict
        if type(capacity) is not int or capacity<1:raise ValueError('invalid_cancel_journal_capacity')
        self.capacity=capacity;self.operations=OrderedDict();self.clients={};self.service_id=uuid.uuid4().hex

    def handle(self,msg,peer,executor):
        import json
        from copy import deepcopy
        allowed={'type','transaction_id','command_instance_id','reason','intake_request_id',
                 'operation_schema','cancel_session_id','cancel_sequence'}
        session=msg.get('cancel_session_id');seq=msg.get('cancel_sequence');nonce=msg.get('intake_request_id')
        if (set(msg)-allowed or msg.get('operation_schema')!=OP_SCHEMA or not token(session) or not token(nonce)
            or type(seq) is not int or seq<1 or not reason_text(msg.get('reason'))
            or msg.get('transaction_id') is not None and not valid_tx(msg['transaction_id'])
            or msg.get('command_instance_id') is not None and not token(msg['command_instance_id'])):
            raise ValueError('invalid_cancel_operation')
        client=self.clients.get(session)
        if client is None:
            if len(self.clients)>=64:raise ValueError('cancel_client_capacity')
            client={'peer':peer,'sequence':0};self.clients[session]=client
        if client['peer']!=peer:raise ValueError('cancel_operation_peer_mismatch')
        fingerprint=json.dumps(msg,sort_keys=True,separators=(',',':'),allow_nan=False)
        key=(session,seq);entry=self.operations.get(key)
        if entry is not None:
            if entry['fingerprint']!=fingerprint:raise ValueError('cancel_operation_payload_conflict')
        else:
            if seq<=client['sequence']:raise ValueError('stale_or_evicted_cancel_operation')
            client['sequence']=seq
            target=executor.resolve_cancel_target(transaction_id=msg.get('transaction_id'),command_instance_id=msg.get('command_instance_id'))
            entry={'fingerprint':fingerprint,'target':target,'response':None,'attempts':0};self.operations[key]=entry
            while len(self.operations)>self.capacity:self.operations.popitem(last=False)
        old=entry['response']
        replay=old is not None and (old.get('cancelled') is True or entry['target'] is None)
        if replay:result=deepcopy(old)
        elif entry['target'] is None:
            result={'schema':SCHEMA,'ok':True,'cancelled':False,'latch_cancelled':False,
                    'transaction_id':msg.get('transaction_id'),'reason':'no_matching_operation_target'}
        else:
            entry['attempts']+=1
            result=executor.cancel_bound(entry['target'],reason=msg['reason'])
        result.update(type='cancel_ack',requested_transaction_id=msg.get('transaction_id'),intake_request_id=nonce,
            operation_schema=OP_SCHEMA,cancel_session_id=session,cancel_sequence=seq,
            service_session_id=self.service_id,idempotent_replay=replay,operation_attempts=entry['attempts'],
            bound_transaction_id=entry['target'][0] if entry['target'] else None,
            bound_command_instance_id=entry['target'][1] if entry['target'] else None,
            completion_semantics=('original_operation_completed' if result.get('cancelled') is True else 'original_operation_no_matching_target') if replay else 'current_operation_observation',
            physical_stabilization_verified=False)
        entry['response']=deepcopy(result)
        return result
