"""Timeout closes a command generation; it does not certify an airborne HOLD."""
from copy import deepcopy
import json,time,uuid
from .cancellation import key
from .command_correlation import finite
from .segments import NOTICE_SCHEMA

SCHEMA='Layer6-timeout-stop-v1'

def close_gui_latch(gui,reason,*,target=None,peer=None):
    """Install the same generation fence used by explicit cancellation, locally."""
    current=gui.virtual_state;target=current if target is None else target
    if not isinstance(target,dict):return None
    gate=getattr(gui,'_control_gate',None);session=target.get('control_session_id')
    row=gate.sessions.get(session) if gate else None
    event={'schema':SCHEMA,'reason':reason,'transaction_id':target.get('transaction_id'),
        'command_instance_id':target.get('command_instance_id'),'control_session_id':session,
        'received_monotonic':time.monotonic(),'physical_stabilization_verified':False,
        'FCU_input_release_verified':False,'hold_status':'HOLD_UNVERIFIED'}
    if row is None or peer is not None and row['peer']!=peer:
        return {**event,'ok':False,'error':'unbound_stop_target'}
    request={'control_session_id':session,'transaction_id':target.get('transaction_id'),
        'command_instance_id':target.get('command_instance_id'),'seq':max(1,row['sequence']),
        'request_id':uuid.uuid4().hex,'reason':reason}
    fence=gate.fence(request,row['peer'],current);event['GUI_fence']=deepcopy(fence)
    if fence.get('ok'):
        if fence['matched_active']:gui.virtual_state=None;gui.virtual_deadline=0
        if fence['inhibit_output']:
            gui._cancel_output_inhibited=True
            gui.lbl_digital.setText('Digital Pilot: '+reason+'; HOLD unverified; output inhibited')
        event.update(ok=True,target_absent=key(gui.virtual_state)!=key(target),
            manual_output_inhibited=bool(getattr(gui,'_cancel_output_inhibited',False)))
        manager=getattr(getattr(gui,'link',None),'segments',None)
        active=manager.current_record() if manager else None
        if active and active['context']['instance_id']==target.get('command_instance_id'):
            manager.request_stop(active,reason)
        notice={'type':'segment_closed_notice','schema':NOTICE_SCHEMA,**{k:event[k] for k in ('transaction_id','command_instance_id','control_session_id')},
            'latch_closed':True,'success_claimed':False,'stop_reason':reason}
        try:gui.virtual_sock.sendto(json.dumps(notice,separators=(',',':')).encode(),row['peer'])
        except OSError as exc:event['notice_error']=str(exc)
    else:event.update(ok=False,error='local_fence_failed')
    gui._last_timeout_stop=event
    return event

def expire_gui_state(gui):
    state=getattr(gui,'virtual_state',None)
    if not isinstance(state,dict):return None
    now=time.monotonic();deadline=getattr(gui,'virtual_deadline',None)
    if not finite(deadline) or now>=deadline:return close_gui_latch(gui,'GUI_TTL_expired')
    admission=state.get('admission_deadline_unix')
    if admission is not None and (not finite(admission) or time.time()>=admission):return close_gui_latch(gui,'packet_deadline_expired')
    return None

def hold_outcome(*,reason,cancel_ack=None,cancel_error=None):
    """A transport cancel can succeed without a mode-specific physical HOLD."""
    raw=dict(getattr(cancel_ack,'raw',None) or {})
    confirmed=(getattr(cancel_ack,'ok',False) is True and raw.get('cancelled') is True
        and raw.get('latch_cancelled') is True and raw.get('gui_cancelled') is True)
    return {'schema':'Layer6-HOLD-outcome-v1','status':'HOLD_UNVERIFIED','reason':reason,
        'cancel_confirmed':confirmed,'cancel_ack':raw,'cancel_error':cancel_error,
        'physical_stabilization_verified':False,'FCU_input_release_verified':False,
        'safe_to_continue':False,'flight_ready':False,'command_replay_authorized':False,
        'scope':'transport inhibition is distinct from plant/mode safety'}
