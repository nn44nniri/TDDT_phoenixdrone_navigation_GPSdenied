"""R5.05: actual MANUAL_CONTROL writes, opaque response barrier and source witnesses.

No exact plant send timestamp is synthesized. Ordered-stream observations bound
the FCU clock at local send completion; neither barrier nor write is a motor ACK.
"""
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import math
import time
import uuid

SCHEMA='Layer6-command-correlation-v1'
REQUIRED_POST=('SCALED_IMU','ATTITUDE_QUATERNION','ATTITUDE','SERVO_OUTPUT_RAW')


def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def finite(v):return not isinstance(v,bool) and isinstance(v,(int,float)) and math.isfinite(v)
def valid_tx(v):return isinstance(v,str) and 1<=len(v)<=128 and all(ord(c)>=32 for c in v)
def identity(tx,values,mode=None,discrete=(),segment=None):
    obj={'transaction_id':tx,'state':values,'mode':mode,'discrete':list(discrete)}
    if segment is not None:obj['execution_segment']=segment
    return digest(obj)


def context_from_state(state,*,deadline=None,now=None):
    if deadline is not None:
        now=time.monotonic() if now is None else now
        if not finite(deadline) or not finite(now) or now>deadline:return None
    if not isinstance(state,dict) or not valid_tx(state.get('transaction_id')):return None
    values={k:state.get(k) for k in ('roll','pitch','throttle','yaw')}
    if any(not finite(v) for v in values.values()):return None
    mode=state.get('command_mode');discrete=state.get('command_discrete') or []
    if not isinstance(discrete,list) or len(discrete)>16 or any(not isinstance(v,str) for v in discrete):return None
    command=state.get('command_id');instance=state.get('command_instance_id');activation=state.get('_gui_activation_id')
    segment=state.get('execution_segment')
    if command!=identity(state['transaction_id'],values,mode,discrete,segment):return None
    if any(not isinstance(v,str) or len(v)!=32 or any(c not in '0123456789abcdef' for c in v) for v in (instance,activation)):return None
    result={'transaction_id':state['transaction_id'],'command_id':command,'instance_id':instance,
            'activation_id':activation,'requested_gui_state':values,'mode':mode,'discrete':discrete}
    if segment is not None:
        from .segments import contract
        try:
            if contract({'execution_segment':segment})!=segment:return None
        except (ValueError,TypeError,KeyError):return None
        if not finite(state.get('admission_deadline_unix')):return None
        result.update(execution_segment=segment,control_session_id=state.get('control_session_id'),admission_deadline_unix=state['admission_deadline_unix'])
    return result


def reference(rec):
    if rec is None:return None
    stamp=rec.get('source_time');seconds=None
    if stamp is not None and finite(stamp.get('value')):
        seconds=stamp['value']*(.001 if stamp['unit']=='ms' else .000001)
    return {'type':rec['type'],'source':rec['source'],'wire_sha256':rec['wire_sha256'],
        'received_monotonic':rec['timestamp_monotonic'],'source_time':deepcopy(stamp),'plant_time_s':seconds,
        'history_sequence':rec.get('history_sequence'),'source_system':rec['source_system'],'source_component':rec['source_component']}


def verified_mavlink_send(master,message):
    """Observe actual OS write counts: pymavlink TCP/UDP write() swallows errors."""
    original=master.port;calls=[]
    if original is None:raise ConnectionError('MAVLink transport has no port')
    class PortWitness:
        def __getattr__(self,name):return getattr(original,name)
        def _call(self,name,data,*a,**kw):
            raw=bytes(data)
            try:count=getattr(original,name)(data,*a,**kw)
            except Exception as exc:
                calls.append((raw,None,str(exc)));raise
            calls.append((raw,count,None));return count
        def send(self,data,*a,**kw):return self._call('send',data,*a,**kw)
        def sendto(self,data,*a,**kw):return self._call('sendto',data,*a,**kw)
        def write(self,data,*a,**kw):return self._call('write',data,*a,**kw)
    proxy=PortWitness();master.port=proxy
    try:master.mav.send(message)
    finally:
        if master.port is proxy:master.port=original
    wire=bytes(message.get_msgbuf())
    if not calls or any(error or type(n) is not int or n!=len(raw) or raw!=wire for raw,n,error in calls):
        raise OSError('MAVLink OS write failed, partial, or unverifiable')
    return wire


class CommandCorrelation:
    def __init__(self,session,domain,*,ordered_stream=False,source_verified=False):
        self.session=session;self.domain=domain;self.ordered=ordered_stream;self.source_verified=source_verified
        self.records=OrderedDict();self.current=None;self.last_manual=None

    def record_send(self,context,applied,wire,started,completed,*,write_verified,pre=None):
        self.last_manual=completed if write_verified else None
        if context is None or not write_verified:
            self.current=None;return None
        key=digest({'session':self.session,'context':context,'applied':applied})
        if key in self.records:
            event=self.records[key];event['send_count']+=1;event['last_sent_monotonic']=completed
            self.records.move_to_end(key);self.current=key;return None
        nonce=uuid.uuid4().int & ((1<<63)-1)
        event={'schema':SCHEMA,'generation_id':key,'session_id':self.session,'monotonic_domain_id':self.domain,
            'context':deepcopy(context),'applied_pilot_command':deepcopy(applied),'first_write_started_monotonic':started,
            'first_sent_monotonic':completed,'last_sent_monotonic':completed,'send_count':1,
            'wire_hex':wire.hex(),'wire_sha256':hashlib.sha256(wire).hexdigest(),
            'wire_sequence':wire[4] if wire[0]==253 else wire[2],
            'transport_write_verified':True,'pre_source_observation':reference(pre),
            'ordered_stream':self.ordered,'local_source_contract_verified':self.source_verified,
            'barrier_nonce':nonce,'barrier_request_sent':False,'barrier_reply':None,
            'plant_time_bounds':None,'post_samples':{},'scope':'continuous_MANUAL_CONTROL_transport_write',
            'FCU_manual_acknowledged':False,'mechanical_execution_verified':False}
        self.records[key]=event;self.current=key
        while len(self.records)>16:self.records.popitem(last=False)
        return nonce

    def failed_send(self):self.current=None

    def barrier_sent(self,nonce):
        e=self.records.get(self.current)
        if e is not None and e['barrier_nonce']==nonce:e['barrier_request_sent']=True

    def timesync_reply(self,message):
        # tc1 is UART-adjusted in this ArduPilot version. It is not used as an exact clock point.
        for e in self.records.values():
            if (e['barrier_request_sent'] and e['barrier_reply'] is None
                and getattr(message,'ts1',None)==e['barrier_nonce'] and getattr(message,'tc1',0)>0):
                raw=bytes(message.get_msgbuf())
                e['barrier_reply']={'received_monotonic':time.monotonic(),'wire_hex':raw.hex(),
                    'wire_sha256':hashlib.sha256(raw).hexdigest(),'echo_nonce':message.ts1,
                    'tc1_used_as_clock_point':False}

    def observe(self,rec,*,fresh,clock_valid):
        e=self.records.get(self.current)
        if e is None or not rec['valid'] or not fresh or not clock_valid:return
        ref=reference(rec)
        if ref['received_monotonic']<=e['first_sent_monotonic']:return
        barrier=e['barrier_reply'];pre=e['pre_source_observation']
        if (e['plant_time_bounds'] is None and self.ordered and self.source_verified and barrier is not None
            and rec['type']=='SCALED_IMU' and ref['received_monotonic']>barrier['received_monotonic']
            and pre is not None and pre['plant_time_s'] is not None and ref['plant_time_s']>=pre['plant_time_s']):
            e['plant_time_bounds']={'verified':True,'lower_s':pre['plant_time_s'],
                'upper_s':ref['plant_time_s']+.001,'clock_domain':'FCU_BOOT','upper_quantization_s':.001,
                'proof':'pre_send_source_then_write_then_echo_barrier_then_source_on_ordered_stream',
                'upper_source_observation':deepcopy(ref),'exact':False,'clock_rate_assumed':False,'rebase':False}
        bound=e['plant_time_bounds']
        if rec['type'] in REQUIRED_POST and bound is not None and ref['plant_time_s'] is not None and ref['plant_time_s']>=bound['upper_s']:
            e['post_samples'][rec['type']]=ref

    def snapshot(self,active_context=None):
        e=self.records.get(self.current)
        active=e is not None and active_context==e['context']
        if not active:
            return {'schema':SCHEMA,'available':False,'reason':'no_matching_active_sent_command',
                'manual_sent_monotonic':self.last_manual,'session_id':self.session,'record':None}
        row=deepcopy(e);complete=row['plant_time_bounds'] is not None and all(k in row['post_samples'] for k in REQUIRED_POST)
        return {'schema':SCHEMA,'available':complete,'reason':'bounded_post_send_observations' if complete else 'awaiting_verified_source_witnesses',
                'manual_sent_monotonic':self.last_manual,'session_id':self.session,'record':row,'record_hash':digest(row)}


def verified_send_bounds(status,tx):
    """Validate a self-contained temporal witness; no signed peer attestation is implied."""
    try:
        c=status['command_correlation'];e=c['record'];b=e['plant_time_bounds']
        if (c['schema']!=SCHEMA or c['available'] is not True or c['record_hash']!=digest(e)
            or e['context']['transaction_id']!=tx or status['applied_transaction_id']!=tx
            or e['session_id']!=status['session_id'] or e['monotonic_domain_id']!=status['monotonic_domain_id']
            or e['transport_write_verified'] is not True or e['first_sent_monotonic']!=status['command_first_sent_monotonic']
            or e['ordered_stream'] is not True or e['local_source_contract_verified'] is not True
            or b['verified'] is not True or b['exact'] is not False or b['rebase'] is not False or b['clock_rate_assumed'] is not False
            or not all(finite(b[k]) for k in ('lower_s','upper_s')) or not 0<=b['lower_s']<=b['upper_s']):return None
        pre=e['pre_source_observation'];upper=b['upper_source_observation'];barrier=e['barrier_reply']
        if (b['proof']!='pre_send_source_then_write_then_echo_barrier_then_source_on_ordered_stream'
            or b['lower_s']!=pre['plant_time_s'] or abs(b['upper_s']-upper['plant_time_s']-.001)>1e-12
            or b['upper_quantization_s']!=.001 or b['clock_domain']!='FCU_BOOT'
            or not pre['received_monotonic']<=e['first_write_started_monotonic']<=e['first_sent_monotonic']<=barrier['received_monotonic']<upper['received_monotonic']):return None
        from pymavlink.dialects.v20 import ardupilotmega as dialect
        packet=dialect.MAVLink(None).parse_char(bytes.fromhex(e['wire_hex']))
        reply=dialect.MAVLink(None).parse_char(bytes.fromhex(barrier['wire_hex']))
        if (hashlib.sha256(bytes.fromhex(e['wire_hex'])).hexdigest()!=e['wire_sha256']
            or hashlib.sha256(bytes.fromhex(barrier['wire_hex'])).hexdigest()!=barrier['wire_sha256']
            or packet is None or packet.get_type()!='MANUAL_CONTROL' or packet.target!=status['target_system']
            or reply is None or reply.get_type()!='TIMESYNC' or reply.ts1!=e['barrier_nonce'] or reply.tc1<=0
            or (reply.get_srcSystem(),reply.get_srcComponent())!=(status['target_system'],status['target_component'])):return None
        actual={'aileron':packet.y/1000.,'elevator':packet.x/1000.,'throttle':packet.z/1000.,'rudder':packet.r/1000.}
        if actual!=e['applied_pilot_command'] or actual!=status['applied_pilot_command']:return None
        for name in REQUIRED_POST:
            post=e['post_samples'][name];current=status['telemetry_metadata'][name]
            if (current['valid'] is not True or current['freshness']['fresh'] is not True
                or post['wire_sha256']!=current['wire_sha256'] or post['received_monotonic']<e['first_sent_monotonic']
                or post['plant_time_s']<b['upper_s']):return None
        return b['lower_s'],b['upper_s']
    except Exception:return None  # Malformed/undecodable evidence fails closed.
