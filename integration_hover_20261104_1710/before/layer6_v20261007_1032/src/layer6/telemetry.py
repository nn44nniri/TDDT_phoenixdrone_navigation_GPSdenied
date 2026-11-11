"""R5.01: retain actual MAVLink channel samples, without synthesizing state.

Raw values stay in message-native units. R5.03 projects separate SI/frame and
receipt/progression freshness contracts without overwriting those values.
"""
from copy import deepcopy
import hashlib
import math
import time
from pymavlink.dialects.v20 import ardupilotmega as dialect
from .telemetry_history import TelemetryHistory
from .telemetry_normalization import ChannelContracts

_AXES=('xacc','yacc','zacc','xgyro','ygyro','zgyro','xmag','ymag','zmag')
SPECS={
    'SCALED_IMU':(('time_boot_ms',)+_AXES,('temperature',),'time_boot_ms'),
    'SCALED_IMU2':(('time_boot_ms',)+_AXES,('temperature',),'time_boot_ms'),
    'SCALED_IMU3':(('time_boot_ms',)+_AXES,('temperature',),'time_boot_ms'),
    'RAW_IMU':(('time_usec',)+_AXES,('id','temperature'),'time_usec'),
    'HIGHRES_IMU':(('time_usec',)+_AXES+('abs_pressure','diff_pressure','pressure_alt','temperature','fields_updated'),('id',),'time_usec'),
    'ATTITUDE_QUATERNION':(('time_boot_ms','q1','q2','q3','q4','rollspeed','pitchspeed','yawspeed'),('repr_offset_q',),'time_boot_ms'),
    'SYS_STATUS':(('onboard_control_sensors_present','onboard_control_sensors_enabled','onboard_control_sensors_health',
        'load','voltage_battery','current_battery','battery_remaining','drop_rate_comm','errors_comm',
        'errors_count1','errors_count2','errors_count3','errors_count4'),
        ('onboard_control_sensors_present_extended','onboard_control_sensors_enabled_extended','onboard_control_sensors_health_extended'),None),
    'EKF_STATUS_REPORT':(('flags','velocity_variance','pos_horiz_variance','pos_vert_variance','compass_variance','terrain_alt_variance'),('airspeed_variance',),None),
    'SERVO_OUTPUT_RAW':(('time_usec','port')+tuple('servo%d_raw'%i for i in range(1,9)),tuple('servo%d_raw'%i for i in range(9,17)),'time_usec'),
    'ACTUATOR_OUTPUT_STATUS':(('time_usec','active','actuator'),(),'time_usec'),
}
REQUEST_HZ={'SCALED_IMU':50.,'ATTITUDE_QUATERNION':20.,'SYS_STATUS':2.,'EKF_STATUS_REPORT':5.,'SERVO_OUTPUT_RAW':20.}
_SIZE={'uint8_t':1,'int8_t':1,'uint16_t':2,'int16_t':2,'uint32_t':4,'int32_t':4,'uint64_t':8,'int64_t':8,'float':4,'double':8,'char':1}


def empty_channel_status(reason='not_connected'):
    return {name:{'received':False,'available':False,'reason':reason,'sample':None,'received_count':0} for name in SPECS}


def _finite(value):
    if isinstance(value,(list,tuple)):return bool(value) and all(_finite(v) for v in value)
    return not isinstance(value,bool) and isinstance(value,(int,float)) and math.isfinite(value)


def _extension_offset(cls,name):
    types=dict(zip(cls.fieldnames,cls.fieldtypes));arrays=dict(zip(cls.ordered_fieldnames,cls.array_lengths))
    offset=0
    for field in cls.ordered_fieldnames:
        if field==name:return offset
        offset+=_SIZE[types[field]]*max(1,arrays[field])
    return None


class TelemetryReceiver:
    def __init__(self,system,component,*,history_capacity=4096,source_contract=None):
        if any(isinstance(v,bool) or not isinstance(v,int) or not 1<=v<=255 for v in (system,component)):
            raise ValueError('observed FCU identity required for telemetry')
        self.system=system;self.component=component;self.latest={};self.counts={};self.foreign_packets=0
        self.contracts=ChannelContracts(source_contract)
        self.history=TelemetryHistory(history_capacity,projector=self.contracts.evaluate)

    def accepts(self,msg):
        try:correct=(msg.get_srcSystem(),msg.get_srcComponent())==(self.system,self.component)
        except Exception:correct=False
        if not correct:self.foreign_packets+=1
        return correct

    def ingest(self,msg):
        name=msg.get_type()
        if name not in SPECS:return None
        if not self.accepts(msg):return None
        required,optional,clock=SPECS[name];cls=getattr(dialect,'MAVLink_'+name.lower()+'_message')
        issues={}
        try:wire=bytes(msg.get_msgbuf())
        except Exception:wire=b''
        version=2 if wire and wire[0]==253 else 1 if wire and wire[0]==254 else None
        length=wire[1] if len(wire)>1 else None
        if version is None or len(wire)<(12 if version==2 else 8)+(length or 0):issues['wire']='missing_or_incomplete_frame'
        else:
            source=(wire[5],wire[6]) if version==2 else (wire[3],wire[4])
            msgid=int.from_bytes(wire[7:10],'little') if version==2 else wire[5]
            if source!=(self.system,self.component) or msgid!=cls.id:issues['wire']='header_identity_or_message_mismatch'
        payload={};unavailable={}
        for field in required+optional:
            if field in optional:
                offset=_extension_offset(cls,field)
                if version!=2 or offset is None or length is None or length<=offset:
                    payload[field]=None;unavailable[field]='extension_not_observed_on_wire';continue
            value=getattr(msg,field,None)
            if value is None or not _finite(value):
                payload[field]=None
                if field in required:issues[field]='missing_or_nonfinite'
                else:unavailable[field]='missing_or_nonfinite'
            else:payload[field]=list(value) if isinstance(value,(list,tuple)) else value
        if clock and (isinstance(payload[clock],bool) or not isinstance(payload[clock],int) or payload[clock]<0):
            issues[clock]='invalid_source_timestamp'
        if name=='ATTITUDE_QUATERNION' and all(payload[k] is not None for k in ('q1','q2','q3','q4')):
            if sum(payload[k]**2 for k in ('q1','q2','q3','q4'))<=1e-12:issues['quaternion']='zero_norm'
        source_time=None if clock is None else {'field':clock,'value':payload.get(clock),
            'unit':'ms' if clock=='time_boot_ms' else 'us',
            'clock_domain':'FCU_BOOT' if clock=='time_boot_ms' else 'message_native_origin_unqualified'}
        self.counts[name]=self.counts.get(name,0)+1
        rec={'schema':'Layer6-received-channel-r5.01-v1','type':name,'source':'MAVLink/'+name,
            'source_system':self.system,'source_component':self.component,'timestamp_monotonic':time.monotonic(),
            'source_time':source_time,'fields':payload,'units':{k:cls.fieldunits_by_name.get(k,'message_native') for k in payload},
            'valid':not issues,'issues':issues,'unavailable_fields':unavailable,'wire_version':version,
            'wire_payload_length':length,'wire_sha256':hashlib.sha256(wire).hexdigest(),'wire_hex':wire.hex(),
            'received_count':self.counts[name],'guidance_state_qualified':False,'navigation_truth_injection':False}
        rec['wire_sequence']=wire[4] if version==2 and len(wire)>4 else wire[2] if version==1 and len(wire)>2 else None
        rec['monotonic_domain_id']=self.contracts.domain
        rec['freshness_basis']=self.contracts.observe(rec)
        rec=self.history.append(rec)
        self.latest[name]=deepcopy(rec)
        return rec

    def snapshot(self):
        result=empty_channel_status('not_received');now=time.monotonic()
        for name,rec in self.latest.items():
            result[name]={'received':True,'available':rec['valid'],'reason':'received' if rec['valid'] else 'invalid_sample',
                'sample':deepcopy(rec),'received_count':self.counts[name]}
            result[name]['contract']=self.contracts.evaluate(rec,now=now)
        return result
