"""Plant-clock bounded continuous first-move latch; no clock-rate or exact-time guess."""
from collections import OrderedDict
from copy import deepcopy
import hashlib,json,time,uuid
from .command_correlation import finite,digest,reference,verified_send_bounds

SCHEMA='Layer6-first-move-segment-v1'
STATUS_SCHEMA='Layer6-segment-status-v1'
NOTICE_SCHEMA='Layer6-segment-latch-stop-v1'
DEFAULT_CLOSE_GUARD_S=.04
POST=('SCALED_IMU','ATTITUDE_QUATERNION','ATTITUDE','SERVO_OUTPUT_RAW')


def contract(packet):
    inner=packet.get('layer6_package',packet)
    explicit=packet.get('execution_segment') or inner.get('execution_segment')
    duration=packet.get('execution_segment_s',inner.get('execution_segment_s'))
    if explicit is None and duration is None:return None
    if explicit is not None:
        if not isinstance(explicit,dict) or set(explicit)!={'schema','max_duration_s','close_guard_s'} or explicit['schema']!=SCHEMA:raise ValueError('invalid execution segment contract')
        if duration is not None and duration!=explicit['max_duration_s']:raise ValueError('conflicting execution segment duration')
        duration=explicit['max_duration_s'];guard=explicit['close_guard_s']
    else:guard=DEFAULT_CLOSE_GUARD_S
    if not finite(duration) or not finite(guard) or not 0<guard<duration:raise ValueError('invalid segment duration/close guard')
    result={'schema':SCHEMA,'max_duration_s':float(duration),'close_guard_s':float(guard)}
    for obj in (packet,inner):
        if obj.get('execution_segment') is not None and obj['execution_segment']!=result:raise ValueError('conflicting segment aliases')
        if obj.get('execution_segment_s') is not None and obj['execution_segment_s']!=duration:raise ValueError('conflicting segment aliases')
    return result


class SegmentExecution:
    def __init__(self,link):self.link=link;self.records=OrderedDict();self.current=None
    def current_record(self):return self.records.get(self.current)
    def running(self):
        r=self.current_record();return r is not None and r['state'] in ('ACTIVE','STOP_REQUESTED','CLOSING')
    def _clock(self):
        receiver=self.link.telemetry_receiver
        if receiver is None:return None
        r=receiver.latest.get('SCALED_IMU')
        if r is None:return None
        c=receiver.contracts.evaluate(r)
        return r if c['freshness']['fresh'] and c['normalized']['available'] and c['normalized']['source_clock_domain']=='FCU_BOOT' else None
    def request_stop(self,r,reason):
        if r['state']=='ACTIVE':
            r.update(state='STOP_REQUESTED',stop_reason=reason,write_blocked_monotonic=time.monotonic())
    def before_write(self,context,pre):
        active=self.current_record();policy=context.get('execution_segment') if context else None
        if active is not None and active['state']=='ACTIVE' and (context is None or context.get('instance_id')!=active['context']['instance_id']):
            self.request_stop(active,'context_lost_or_superseded')
        if policy is None:
            # A missing context cannot turn inherited throttle into a manual write.
            if context is None and active is not None:return False
            return True
        instance=context['instance_id'];r=self.records.get(instance)
        if r is None:
            p=contract({'execution_segment':policy});source=reference(pre)
            r={'schema':SCHEMA,'state':'ACTIVE','context':deepcopy(context),'policy':p,
               'session_id':self.link.telemetry_receiver.history.session_id,'monotonic_domain_id':self.link.telemetry_receiver.contracts.domain,
               'start_pre_source':source,'first_event':None,'first_generation':None,'close':None,'GUI_fence':None,
               'snapshot':None,'qualified':False,'physical_stabilization_verified':False,'exact_time_assumed':False,'clock_rate_assumed':False}
            self.records[instance]=r;self.current=instance
            while len(self.records)>32:self.records.popitem(last=False)
            if pre is None or not self.link.command_correlation.ordered or not self.link.command_correlation.source_verified:
                self.request_stop(r,'unqualified_start_clock_or_transport');return False
            r['ceiling_s']=source['plant_time_s']+p['max_duration_s']
            r['trigger_s']=r['ceiling_s']-p['close_guard_s'];r['last_write_source_s']=None
        if r['context']!=context or r['policy']!=policy:
            self.request_stop(r,'changed_segment_context');return False
        if r['state']!='ACTIVE':return False
        if not finite(context.get('admission_deadline_unix')) or time.time()>=context['admission_deadline_unix']:
            self.request_stop(r,'admission_deadline_expired');return False
        if pre is None:self.request_stop(r,'source_clock_unavailable');return False
        stamp=reference(pre)['plant_time_s']
        if stamp+1e-9>=r['trigger_s']:self.request_stop(r,'plant_close_guard_reached');return False
        if r['last_write_source_s'] is not None and stamp<=r['last_write_source_s']:return False
        r['last_write_source_s']=stamp;return True
    def sent(self,context):
        if not context or not context.get('execution_segment'):return
        r=self.records.get(context['instance_id']);tracker=self.link.command_correlation
        if r is not None and r['first_generation'] is None:r['first_generation']=tracker.current
    def observe(self,rec):
        for r in list(self.records.values()):
            if r['state']=='ACTIVE' and rec['type']=='SCALED_IMU':
                clock=self._clock()
                if clock is None:self.request_stop(r,'invalid_or_reversed_source_clock')
                elif reference(clock)['plant_time_s']+1e-9>=r['trigger_s']:self.request_stop(r,'plant_close_guard_reached')
            close=r.get('close')
            if r['state']=='CLOSING' and rec['type']=='SCALED_IMU' and close and close.get('reply'):
                ref=reference(rec)
                if self._clock() is None:r.update(state='FAILED',failure='invalid_close_clock');continue
                if close.get('bounds') is None and ref['received_monotonic']>close['reply']['received_monotonic']:
                    close['upper_source']=ref
                    close['bounds']={'lower_s':close['pre_source']['plant_time_s'],'upper_s':ref['plant_time_s']+.001}
            self.finalize(r)
    def check(self):
        r=self.current_record()
        if r and r['state']=='ACTIVE' and self._clock() is None:self.request_stop(r,'missing_stale_or_reversed_clock')
        for row in self.records.values():
            if row['state']=='CLOSING':
                if self._clock() is None:row.update(state='FAILED',failure='close_clock_unavailable')
                else:self.finalize(row)
    def GUI_closed(self,r,fence):
        if r['state']!='STOP_REQUESTED':return
        r['GUI_fence']=deepcopy(fence);pre=self._clock()
        if pre is None or r.get('first_generation') is None:r.update(state='FAILED',failure=r['stop_reason']);return
        r['close']={'monotonic':time.monotonic(),'pre_source':reference(pre),'nonce':uuid.uuid4().int&((1<<63)-1),
                    'request_sent_verified':False,'reply':None,'bounds':None}
        r['state']='CLOSING'
        try:
            from .command_correlation import verified_mavlink_send
            c=r['close'];wire=verified_mavlink_send(self.link.master,self.link.master.mav.timesync_encode(0,c['nonce']))
            c.update(request_sent_verified=True,request_wire_hex=wire.hex(),request_wire_sha256=hashlib.sha256(wire).hexdigest())
        except Exception as exc:r.update(state='FAILED',failure='stop_barrier_write_failed: '+str(exc))
    def timesync(self,message):
        for r in self.records.values():
            c=r.get('close')
            if c and c['request_sent_verified'] and c['reply'] is None and message.ts1==c['nonce'] and message.tc1>0:
                wire=bytes(message.get_msgbuf());c['reply']={'received_monotonic':time.monotonic(),'wire_hex':wire.hex(),'wire_sha256':hashlib.sha256(wire).hexdigest(),'tc1_used_as_clock_point':False}
    def finalize(self,r):
        if r['state']!='CLOSING' or not r['close'].get('bounds'):return
        if r['stop_reason']!='plant_close_guard_reached':
            r.update(state='FAILED',failure=r['stop_reason']);return
        e=self.link.command_correlation.records.get(r['first_generation']);b=e.get('plant_time_bounds') if e else None
        if b is None:r.update(state='FAILED',failure='start_bounds_unavailable');return
        cb=r['close']['bounds'];duration={'lower_s':max(0.,cb['lower_s']-b['upper_s']),'upper_s':cb['upper_s']-b['lower_s']}
        if duration['upper_s']>r['policy']['max_duration_s']+1e-9 or duration['lower_s']<=0:
            r.update(state='FAILED',failure='segment_overrun_or_unproven_positive_exposure',duration_bounds_s=duration);return
        from .status import project_status,validate_observation_status,SCHEMA as GUI_SCHEMA
        p=project_status(self.link,active_command_context=r['context'])
        # Use the public GUI-status names once. Keeping project_status's private
        # aliases as well serializes the complete telemetry metadata twice and
        # can make a valid Native closure exceed the segment RPC datagram budget.
        snap={k:v for k,v in p.items() if k not in ('connected','now','domain','metadata','ages','channels')}
        snap.update({'schema':GUI_SCHEMA,'schema_version':1,'ok':True,'mavlink_connected':p['connected'],
            'timestamp_monotonic':p['now'],'monotonic_domain_id':p['domain'],'telemetry_metadata':p['metadata'],
            'telemetry_age_s':p['ages'],'telemetry_channels':p['channels'],
            'telemetry_received_monotonic':{k:v['received_monotonic'] for k,v in p['metadata'].items()},
            'target_system':self.link.target_system,'target_component':self.link.target_component,
            'command_first_sent_plant_time_s':None,'scope':'bounded_segment_post_observation','flight_ready':False})
        if 'status_quality' in snap:
            snap['status_quality'].update(scope='bounded_segment_post_observation',
                execution_correlation_available=p['command_correlation']['available'])
            if r.get('GUI_fence') is not None:
                snap['status_quality']['manual_output_inhibited']=r['GUI_fence'].get('inhibit_output') is True
        if not validate_observation_status(snap)['ok']:return
        for name in POST:
            meta=snap['telemetry_metadata'][name];s=meta['source_time']
            stamp=s['value']*(.001 if s['unit']=='ms' else .000001)
            if stamp<cb['upper_s'] or not meta['freshness']['fresh']:return
        if verified_send_bounds(snap,r['context']['transaction_id']) is None:return
        r.update(state='CLOSED',qualified=True,duration_bounds_s=duration,first_event=deepcopy(e),snapshot=snap)
        r['result_hash']=digest({k:v for k,v in r.items() if k!='result_hash'})
    def response(self,tx,instance):
        r=self.records.get(instance)
        if r is None or r['context']['transaction_id']!=tx:return {'available':False,'reason':'segment_not_found'}
        return {'available':True,'segment':deepcopy(r)}


def sync_gui_segment(gui):
    link=getattr(gui,'link',None);manager=getattr(link,'segments',None)
    if manager is None:return
    r=manager.current_record()
    if r is None or r['state']!='STOP_REQUESTED':return
    from .cancellation import key
    ctx=r['context'];gate=getattr(gui,'_control_gate',None);session=ctx.get('control_session_id')
    row=gate.sessions.get(session) if gate else None
    if row is None:r.update(state='FAILED',failure='GUI_control_fence_unavailable');return
    msg={'control_session_id':session,'transaction_id':ctx['transaction_id'],'command_instance_id':ctx['instance_id'],
         'seq':max(1,row['sequence']),'request_id':uuid.uuid4().hex,'reason':'local_execution_segment_stop'}
    fence=gate.fence(msg,row['peer'],gui.virtual_state)
    if not fence['ok']:r.update(state='FAILED',failure='GUI_segment_fence_rejected');return
    if fence['matched_active']:gui.virtual_state=None;gui.virtual_deadline=0
    if fence['inhibit_output']:gui._cancel_output_inhibited=True;gui.lbl_digital.setText('Digital Pilot: segment closed; pilot output inhibited')
    fence['target_absent']=key(gui.virtual_state)!=key(msg)
    manager.GUI_closed(r,fence)
    notice={'type':'segment_closed_notice','schema':NOTICE_SCHEMA,'transaction_id':ctx['transaction_id'],
            'command_instance_id':ctx['instance_id'],'control_session_id':session,'latch_closed':True,'success_claimed':False}
    gui.virtual_sock.sendto(json.dumps(notice,separators=(',',':')).encode(),row['peer'])


def validate_result(r,tx,instance,policy,*,before_session=None):
    try:
        if r['schema']!=SCHEMA or r['state']!='CLOSED' or r['qualified'] is not True or r['result_hash']!=digest({k:v for k,v in r.items() if k!='result_hash'}):return False
        if r['context']['transaction_id']!=tx or r['context']['instance_id']!=instance or r['policy']!=policy:return False
        if before_session is not None and r['session_id']!=before_session:return False
        s=r['snapshot'];start=verified_send_bounds(s,tx);c=r['close'];b=c['bounds'];f=r['GUI_fence']
        if start is None or s['session_id']!=r['session_id'] or s['monotonic_domain_id']!=r['monotonic_domain_id']:return False
        e=s['command_correlation']['record']
        if r['first_event']!=e or r['first_generation']!=e['generation_id'] or r['context']!=e['context'] or r['context']['execution_segment']!=policy:return False
        if r['stop_reason']!='plant_close_guard_reached' or r['physical_stabilization_verified'] is not False:return False
        if (f['transaction_id'],f['command_instance_id'],f['control_session_id'])!=(tx,instance,r['context']['control_session_id']):return False
        if f['replay_fence_installed'] is not True or f['target_absent'] is not True or c['request_sent_verified'] is not True:return False
        if not r['write_blocked_monotonic']<=c['monotonic']<=c['reply']['received_monotonic']<c['upper_source']['received_monotonic']:return False
        if not c['pre_source']['received_monotonic']<=c['monotonic'] or b['lower_s']!=c['pre_source']['plant_time_s'] or abs(b['upper_s']-c['upper_source']['plant_time_s']-.001)>1e-12:return False
        for source in (c['pre_source'],c['upper_source']):
            if source['type']!='SCALED_IMU' or source['source_time']['clock_domain']!='FCU_BOOT' or (source['source_system'],source['source_component'])!=(s['target_system'],s['target_component']):return False
        from pymavlink.dialects.v20 import ardupilotmega as dialect
        for obj,name in ((c,'request'),(c['reply'],None)):
            prefix=name+'_' if name else ''
            wire=bytes.fromhex(obj[prefix+'wire_hex'])
            if hashlib.sha256(wire).hexdigest()!=obj[prefix+'wire_sha256']:return False
            msg=dialect.MAVLink(None).parse_char(wire)
            if msg is None or msg.get_type()!='TIMESYNC' or msg.ts1!=c['nonce']:return False
            if name and msg.tc1!=0:return False
            if not name and (msg.tc1<=0 or (msg.get_srcSystem(),msg.get_srcComponent())!=(s['target_system'],s['target_component'])):return False
        duration={'lower_s':max(0.,b['lower_s']-start[1]),'upper_s':b['upper_s']-start[0]}
        if r['duration_bounds_s']!=duration or not 0<duration['lower_s']<=duration['upper_s']<=policy['max_duration_s']+1e-9:return False
        if r['exact_time_assumed'] is not False or r['clock_rate_assumed'] is not False:return False
        return True
    except (KeyError,TypeError,ValueError,AttributeError):return False
