from __future__ import annotations
import time,uuid
from .contracts import DecisionEnvelope
from .validation import validate
from .virtual_pilot import VirtualPilotClient

MODE_TO_ACTION={'QHOVER':'qhover','FBWA':'fbwa','QSTABILIZE':'qstabilize','QLOITER':'qloiter'}
DISCRETE_TO_ACTION={'ARM':'arm','DISARM':'disarm','THROTTLE_ZERO':'throttle_zero','NEUTRAL_ATTITUDE':'neutral_attitude'}

class DigitalPilotExecutor:
    def __init__(self, client=None, rate_hz=30.0):
        self.client=client or VirtualPilotClient(); self.rate_hz=float(rate_hz); self.active=None; self._last_mode=None
        self.active_instance_id=None;self._cancel_target=None;self._cancel_confirmed=False
    def accept(self, data):
        env=data if isinstance(data,DecisionEnvelope) else DecisionEnvelope.from_dict(data)
        vr=validate(env)
        if not vr.ok: raise ValueError('Layer6 validation failed: '+','.join(vr.reasons))
        mission=env.provenance.get('execution_scope')=='SITL_CLOSED_LOOP'
        mode=(env.command.mode or '').upper()
        discrete={str(d).upper() for d in env.command.discrete}
        if mission and (mode and mode not in MODE_TO_ACTION or {'ARM','DISARM'}<=discrete):
            raise ValueError('invalid mission lifecycle mode/actions')
        self.active=env
        self.active_instance_id=uuid.uuid4().hex
        self._mission_sequence=mission
        self._lifecycle_complete=not self._mission_sequence
        self._last_lifecycle_action=0.0
        if mode and mode != self._last_mode and mode in MODE_TO_ACTION:
            self.client.action(MODE_TO_ACTION[mode],env.transaction_id,command_instance_id=self.active_instance_id); self._last_mode=mode
        for d in (() if self._mission_sequence else env.command.discrete):
            a=DISCRETE_TO_ACTION.get(str(d).upper())
            if a: self.client.action(a,env.transaction_id,command_instance_id=self.active_instance_id)
        return env

    def _mission_ready(self):
        """Confirm FCU MODE/ARM before the first nonzero pilot write/segment."""
        if self._lifecycle_complete:return True
        from .status import validate_observation_status
        remaining=(.12 if self.active.tau_deadline is None else self.active.tau_deadline-time.time())
        if remaining<=0:return False
        st=self.client.status(timeout=min(.12,remaining))
        # A blocking status query may consume the remaining admission deadline.
        # Never issue a lifecycle action after the packet has expired.
        if not validate(self.active).ok:return False
        if not st or not validate_observation_status(st,required=('HEARTBEAT',))['ok']:
            return False
        command=self.active.command;now=time.monotonic()
        mode=(command.mode or '').upper();discrete={str(d).upper() for d in command.discrete}
        if mode and st.get('mode')!=mode:
            if now-self._last_lifecycle_action>=.5:
                self.client.action(MODE_TO_ACTION[mode],self.active.transaction_id,
                    command_instance_id=self.active_instance_id)
                self._last_lifecycle_action=now
            return False
        desired=(False if 'DISARM' in discrete else True if 'ARM' in discrete else st.get('armed'))
        if type(desired) is not bool:return False
        if st.get('armed') is not desired:
            action='disarm' if not desired else ('arm' if 'ARM' in discrete else None)
            if action is None:return False
            if now-self._last_lifecycle_action>=.5:
                self.client.action(action,self.active.transaction_id,command_instance_id=self.active_instance_id)
                self._last_lifecycle_action=now
            return False
        # Preserving a disarmed touchdown state never authorizes motor demand.
        if command.throttle>0 and st.get('armed') is not True:return False
        self._lifecycle_complete=True
        return True
    def cancel(self, *, transaction_id=None, reason='TDOC_HOLD', command_instance_id=None):
        from .cancellation import token,reason_text,SCHEMA
        from .command_correlation import valid_tx
        if (transaction_id is not None and not valid_tx(transaction_id)) or not reason_text(reason) or (command_instance_id is not None and not token(command_instance_id)):
            raise ValueError('invalid cancellation target/reason')
        base={'schema':SCHEMA,'cancelled':False,'scope':'SERVICE_AND_GUI_COMMAND_LATCH_ONLY','physical_stabilization_verified':False}
        target=self.resolve_cancel_target(transaction_id=transaction_id,command_instance_id=command_instance_id)
        if target is None:return {**base,'ok':True,'transaction_id':transaction_id,'reason':'no_matching_latch','latch_cancelled':False}
        return self.cancel_bound(target,reason=reason)
    def resolve_cancel_target(self, *, transaction_id=None, command_instance_id=None):
        if self.active is not None:target=(self.active.transaction_id,self.active_instance_id)
        else:
            target=self._cancel_target
            if transaction_id is None and self._cancel_confirmed:return None
        if target is None or transaction_id is not None and transaction_id!=target[0] or command_instance_id is not None and command_instance_id!=target[1]:return None
        return target
    def cancel_bound(self,target,*,reason):
        from .cancellation import SCHEMA
        base={'schema':SCHEMA,'cancelled':False,'scope':'SERVICE_AND_GUI_COMMAND_LATCH_ONLY','physical_stabilization_verified':False}
        if self.active is not None:
            if (self.active.transaction_id,self.active_instance_id)!=target:
                return {**base,'ok':True,'transaction_id':target[0],'command_instance_id':target[1],
                    'reason':'newer_command_active_original_cancel_unconfirmed','latch_cancelled':False,'gui_cancelled':False}
            # Stop the bound generation before waiting; never stop a newer one.
            self.active=None;self.active_instance_id=None;self._last_mode=None
        self._cancel_target=target;self._cancel_confirmed=False
        try:gui=self.client.cancel(transaction_id=target[0],command_instance_id=target[1],reason=reason)
        except Exception as exc:gui={'ok':False,'cancelled':False,'error':str(exc)}
        good=(isinstance(gui,dict) and gui.get('ok') is True and gui.get('cancelled') is True
              and gui.get('transaction_id')==target[0] and gui.get('command_instance_id')==target[1]
              and gui.get('replay_fence_installed') is True)
        self._cancel_confirmed=good
        return {**base,'ok':good,'cancelled':good,'transaction_id':target[0],'command_instance_id':target[1],
            'latch_cancelled':True,'gui_cancelled':good,'gui_ack':gui,
            'reason':'targeted_cancel_confirmed' if good else 'gui_cancel_not_confirmed'}
    def tick(self):
        poll=getattr(self.client,'segment_notices',None)
        if callable(poll):
            for notice in poll():
                if self.active is not None and (notice.get('transaction_id'),notice.get('command_instance_id'))==(self.active.transaction_id,self.active_instance_id):
                    self._cancel_target=(self.active.transaction_id,self.active_instance_id);self._cancel_confirmed=False
                    self.active=None;self.active_instance_id=None;self._last_mode=None
        if not self.active: return False
        vr=validate(self.active)
        if not vr.ok:
            target=(self.active.transaction_id,self.active_instance_id)
            self.last_timeout_stop=self.cancel_bound(target,reason='packet_invalid_or_deadline_expired')
            return False
        if getattr(self,'_mission_sequence',False) and not self._mission_ready():
            if not validate(self.active).ok:
                target=(self.active.transaction_id,self.active_instance_id)
                self.last_timeout_stop=self.cancel_bound(target,reason='packet_invalid_or_deadline_expired')
                return False
            return True
        if not validate(self.active).ok:
            target=(self.active.transaction_id,self.active_instance_id)
            self.last_timeout_stop=self.cancel_bound(target,reason='packet_invalid_or_deadline_expired')
            return False
        extra={'execution_segment':self.active.execution_segment,'admission_deadline':self.active.tau_deadline} if self.active.execution_segment is not None else {}
        if self.active.tau_deadline is not None:extra['admission_deadline']=self.active.tau_deadline
        self.client.state(self.active.command,ttl_s=max(0.15,3/self.rate_hz),transaction_id=self.active.transaction_id,
                          command_instance_id=self.active_instance_id,**extra); return True
    def hold_for(self, seconds):
        end=time.monotonic()+float(seconds); period=1/self.rate_hz
        while time.monotonic()<end and self.tick(): time.sleep(period)
