"""Bounded raw telemetry history and read-only GUI IPC (R5.02).

Sequences describe arrival order, never a sorted or repaired source clock.
Missing source time remains None. No state estimation or readiness is implied.
"""
from collections import deque
from copy import deepcopy
import json
import uuid
import time

SCHEMA='Layer6-telemetry-history-v1'
MAX_CAPACITY=4096
MAX_LIMIT=64
MAX_RESPONSE_BYTES=48000  # Entire JSON datagram, below UDP's 65507-byte limit.
QUERY_KEYS={'after_seq','limit','channels','session_id','through_seq'}
CHANNELS=('SCALED_IMU','SCALED_IMU2','SCALED_IMU3','RAW_IMU','HIGHRES_IMU',
          'ATTITUDE_QUATERNION','SYS_STATUS','EKF_STATUS_REPORT','SERVO_OUTPUT_RAW','ACTUATOR_OUTPUT_STATUS')


def encode_response(response):
    return json.dumps(response,separators=(',',':'),allow_nan=False).encode('utf-8')


def validate_query(*,after_seq=0,limit=16,channels=None,session_id=None,through_seq=None):
    if type(after_seq) is not int or after_seq<0:raise ValueError('invalid_after_seq')
    if type(limit) is not int or not 1<=limit<=MAX_LIMIT:raise ValueError('invalid_limit')
    if through_seq is not None and (type(through_seq) is not int or through_seq<after_seq):
        raise ValueError('invalid_through_seq')
    if session_id is not None and (not isinstance(session_id,str) or len(session_id)!=32 or any(c not in '0123456789abcdef' for c in session_id)):
        raise ValueError('invalid_session_id')
    if (after_seq>0 or through_seq is not None) and session_id is None:raise ValueError('session_id_required')
    if channels is not None:
        if not isinstance(channels,(list,tuple)) or not 1<=len(channels)<=len(CHANNELS):raise ValueError('invalid_channels')
        if any(not isinstance(n,str) or n not in CHANNELS for n in channels) or len(set(channels))!=len(channels):
            raise ValueError('invalid_channels')
        channels=list(channels)
    return {'after_seq':after_seq,'limit':limit,'channels':channels,'session_id':session_id,'through_seq':through_seq}


def empty_response(reason,request_id=None):
    return {'schema':SCHEMA,'type':'telemetry_history','request_id':request_id,'ok':False,'reason':reason,
            'samples':[],'session_id':None,'has_more':False,'guidance_state_qualified':False}


class TelemetryHistory:
    def __init__(self,capacity=MAX_CAPACITY,*,projector=None):
        if type(capacity) is not int or not 1<=capacity<=MAX_CAPACITY:raise ValueError('invalid_history_capacity')
        self.capacity=capacity;self.session_id=uuid.uuid4().hex
        self.records=deque(maxlen=capacity);self.total=0;self.previous={}
        self.projector=projector

    def append(self,sample):
        record=deepcopy(sample);self.total+=1;name=record['type'];previous=self.previous.get(name)
        source=record['source_time'];prior=previous['source_time'] if previous is not None else None
        delta=None;relation='unavailable'
        if source is not None and isinstance(source.get('value'),(int,float)):
            relation='first'
            if prior is not None and all(prior.get(k)==source.get(k) for k in ('field','unit','clock_domain')) and isinstance(prior.get('value'),(int,float)):
                delta=source['value']-prior['value'];relation='forward' if delta>0 else 'duplicate' if delta==0 else 'reverse'
        record.update(history_sequence=self.total,session_id=self.session_id,
            previous_channel_sequence=previous['history_sequence'] if previous is not None else None,
            source_time_delta=delta,source_time_relation=relation,
            source_time_available=source is not None and source.get('value') is not None)
        self.records.append(record);self.previous[name]=record
        return deepcopy(record)

    def page(self,*,request_id=None,**query):
        try:q=validate_query(**query)
        except (ValueError,TypeError) as exc:
            return empty_response(str(exc) if isinstance(exc,ValueError) else 'invalid_query',request_id)
        if q['session_id'] is not None and q['session_id']!=self.session_id:
            r=empty_response('session_changed',request_id);r['session_id']=self.session_id;return r
        if q['after_seq']>self.total or (q['through_seq'] is not None and q['through_seq']>self.total):
            return empty_response('cursor_ahead_of_source',request_id)
        oldest=self.records[0]['history_sequence'] if self.records else None
        through=self.total if q['through_seq'] is None else q['through_seq']
        lost=max(0,min(through,(oldest or 1)-1)-q['after_seq'])
        matching=[r for r in self.records if q['after_seq']<r['history_sequence']<=through
                  and (q['channels'] is None or r['type'] in q['channels'])]
        response={'schema':SCHEMA,'type':'telemetry_history','request_id':request_id,'ok':True,'reason':'received',
            'session_id':self.session_id,'capacity':self.capacity,'retained_count':len(self.records),
            'total_received':self.total,'evicted_total':self.total-len(self.records),
            'oldest_sequence':oldest,'latest_sequence':self.total if self.total else None,
            'after_seq':q['after_seq'],'through_seq':through,'channels':q['channels'],
            'retention_gap':lost>0,'lost_unfiltered_samples':lost,
            'samples':[],'has_more':bool(matching),'next_after_seq':q['after_seq'],
            'limit':q['limit'],'max_response_bytes':MAX_RESPONSE_BYTES,'truncated_by_bytes':False,
            'guidance_state_qualified':False}
        if self.projector is not None:response['sample_contracts']=[]
        evaluated=time.monotonic()
        for record in matching[:q['limit']]:
            response['samples'].append(deepcopy(record))
            if self.projector is not None:response['sample_contracts'].append(self.projector(record,now=evaluated))
            if len(encode_response(response))>MAX_RESPONSE_BYTES-256:
                response['samples'].pop()
                if self.projector is not None:response['sample_contracts'].pop()
                response['truncated_by_bytes']=True;break
        if matching and not response['samples']:return empty_response('sample_exceeds_datagram_budget',request_id)
        response['has_more']=len(response['samples'])<len(matching)
        response['next_after_seq']=response['samples'][-1]['history_sequence'] if response['has_more'] else through
        assert len(encode_response(response))<=MAX_RESPONSE_BYTES
        return response


def history_response(link,request):
    """Used by the real GUI's read-only request handler; never sends pilot input."""
    request_id=request.get('request_id')
    if not isinstance(request_id,str) or len(request_id)!=32 or any(c not in '0123456789abcdef' for c in request_id):
        return empty_response('invalid_request_id')
    if set(request)-QUERY_KEYS-{'type','request_id','source','seq','sent_monotonic'}:
        return empty_response('invalid_query',request_id)
    query={k:request[k] for k in QUERY_KEYS if k in request}
    if link is None or link.master is None or link.telemetry_receiver is None:
        return empty_response('not_connected',request_id)
    return link.telemetry_receiver.history.page(request_id=request_id,**query)
