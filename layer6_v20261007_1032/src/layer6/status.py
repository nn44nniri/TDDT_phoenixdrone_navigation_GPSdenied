"""Versioned observation status for TDOC; request ACK is never execution evidence."""
from copy import deepcopy
import hashlib
import json
import math
import time
from .telemetry_normalization import finite

SCHEMA='Layer6-GUI-status-v1'
MAX_BYTES=48000
MAX_IMU_SAMPLES=64
CORE_CHANNELS=('HEARTBEAT','ATTITUDE','VFR_HUD','GLOBAL_POSITION_INT','SCALED_IMU','ATTITUDE_QUATERNION')
STATUS_REQUEST_HZ={'ATTITUDE':20.,'VFR_HUD':10.,'GLOBAL_POSITION_INT':20.,'EXTENDED_SYS_STATE':5.}
OBSERVATIONS={
    'HEARTBEAT':(('custom_mode','base_mode','type','autopilot','system_status'),None),
    'ATTITUDE':(('time_boot_ms','roll','pitch','yaw','rollspeed','pitchspeed','yawspeed'),'time_boot_ms'),
    'VFR_HUD':(('airspeed','groundspeed','heading','throttle','alt','climb'),None),
    'GLOBAL_POSITION_INT':(('time_boot_ms','lat','lon','alt','relative_alt','vx','vy','vz','hdg'),'time_boot_ms'),
    'EXTENDED_SYS_STATE':(('vtol_state','landed_state'),None),
}


def encode_status(value):return json.dumps(value,separators=(',',':'),allow_nan=False).encode('utf-8')


def remember_observation_context(link,environment,status_quality):
    """Retain actual GUI-validated metadata for the short bounded segment reply.

    Evidence timestamps remain untouched. Consumers must still check freshness;
    retaining this context never validates it again or grants control authority.
    """
    if link is not None:
        link._native_observation_context=deepcopy({'environment_contract':environment,'status_quality':status_quality})


def status_error(reason,request_id=None,**details):
    return {'type':'status','schema':SCHEMA,'schema_version':1,'ok':False,'reason':reason,
            'request_id':request_id,'mavlink_connected':False,'flight_ready':False,**details}


def validate_observation_status(status,*,required=CORE_CHANNELS,now=None):
    """Recheck current observations at consumption; never grants flight authority.

    Reported ages alone cannot keep an old snapshot fresh during transport/work.
    Historical replay is checked separately against its actual source intervals.
    """
    from .telemetry_normalization import MAX_AGE
    from .timebase import monotonic_domain
    reason='invalid_status_contract'
    try:
        now=time.monotonic() if now is None else now
        if (status.get('schema')!=SCHEMA or status.get('schema_version')!=1 or status.get('ok') is not True
            or status.get('mavlink_connected') is not True or not finite(now)
            or status.get('monotonic_domain_id')!=monotonic_domain()
            or type(status.get('schema_version')) is not int
            or any(type(status.get(k)) is not int or not 1<=status[k]<=255 for k in ('target_system','target_component'))
            or not isinstance(status.get('session_id'),str) or len(status['session_id'])!=32
            or any(c not in '0123456789abcdef' for c in status['session_id'])):raise ValueError(reason)
        for name in required:
            reason='invalid_or_stale_'+name
            meta=status['telemetry_metadata'][name];f=meta['freshness'];age=f['age_s'];evaluated=f['evaluated_monotonic'];received=meta['received_monotonic']
            limit=min(MAX_AGE[name],f['max_age_s'])
            if (meta['valid'] is not True or f['fresh'] is not True or f['reason']!='fresh_local_observation'
                or meta['source']!='MAVLink/'+name
                or (meta['source_system'],meta['source_component'])!=(status['target_system'],status['target_component'])
                or f['monotonic_domain_id']!=status['monotonic_domain_id']
                or not all(finite(v) for v in (age,evaluated,received,limit,f['receipt_age_s']))
                or not 0<=received<=evaluated<=now or limit<=0
                or abs(f['receipt_age_s']-(evaluated-received))>1e-6 or age+1e-6<f['receipt_age_s']
                or not 0<=age+now-evaluated<=limit):raise ValueError(reason)
        if 'SCALED_IMU' in required:
            reason='invalid_current_BODY_FRD_IMU';imu=status['imu'];meta=status['telemetry_metadata']['SCALED_IMU']
            if (imu['source']!='MAVLink/SCALED_IMU' or imu['frame']!='BODY_FRD'
                or not all(finite(imu[k]) for k in ('plant_time_s','accel_x_m_s2','accel_y_m_s2','accel_z_m_s2','gyro_x_rad_s','gyro_y_rad_s','gyro_z_rad_s'))
                or imu['plant_time_s']<0 or meta['source_time']['clock_domain']!='FCU_BOOT'
                or meta['source_time']['unit']!='ms' or meta['source_time']['field']!='time_boot_ms'
                or type(meta['source_time']['value']) is not int or not 0<=meta['source_time']['value']<2**32
                or imu['plant_time_s']!=meta['source_time']['value']*.001
                or imu['wire_sha256']!=meta['wire_sha256']):raise ValueError(reason)
        if 'ATTITUDE_QUATERNION' in required:
            reason='invalid_current_body_quaternion';att=status['attitude'];meta=status['telemetry_metadata']['ATTITUDE_QUATERNION'];q=att['quaternion_wxyz']
            if (att['source']!='MAVLink/ATTITUDE_QUATERNION' or att['frame']!='BODY_FRD_TO_NED'
                or len(q)!=4 or not all(finite(v) for v in q) or abs(sum(v*v for v in q)-1.)>.001
                or not all(finite(att[k]) for k in ('roll','pitch','yaw','roll_rate','pitch_rate','yaw_rate','plant_time_s'))
                or meta['source_time']['clock_domain']!='FCU_BOOT' or meta['source_time']['unit']!='ms'
                or meta['source_time']['field']!='time_boot_ms' or type(meta['source_time']['value']) is not int
                or not 0<=meta['source_time']['value']<2**32
                or att['plant_time_s']!=meta['source_time']['value']*.001):raise ValueError(reason)
        return {'ok':True,'reason':'fresh_local_observations_at_consumption','required_channels':list(required),
                'scope':'observation_validity_only','absolute_source_age_verified':False,'flight_ready':False}
    except (KeyError,ValueError,TypeError,AttributeError,OverflowError):
        return {'ok':False,'reason':reason,'required_channels':list(required),'flight_ready':False}


def observe_status(link,message):
    name=message.get_type()
    if name not in OBSERVATIONS:return None
    required,clock=OBSERVATIONS[name];fields={k:getattr(message,k,None) for k in required}
    issues={k:'missing_or_nonfinite' for k,v in fields.items() if not finite(v)}
    for k in issues:fields[k]=None
    wire=bytes(message.get_msgbuf());received=time.monotonic()
    rec={'type':name,'source':'MAVLink/'+name,'fields':fields,'valid':not issues,'issues':issues,
         'source_system':message.get_srcSystem(),'source_component':message.get_srcComponent(),
         'source_time':None if clock is None else {'field':clock,'value':fields[clock],'unit':'ms','clock_domain':'FCU_BOOT'},
         'timestamp_monotonic':received,'monotonic_domain_id':link.telemetry_receiver.contracts.domain,
         'wire_sha256':hashlib.sha256(wire).hexdigest(),'wire_hex':wire.hex()}
    rec['freshness_basis']=link.telemetry_receiver.contracts.observe(rec)
    link.status_observations[name]=rec
    return rec


def project_status(link,*,include_imu_samples=False,active_command_context=None):
    if type(include_imu_samples) is not bool:raise ValueError('invalid_include_imu_samples')
    now=time.monotonic();connected=link is not None and link.master is not None and link.telemetry_receiver is not None
    receiver=link.telemetry_receiver if connected else None
    raw=receiver.latest if receiver is not None else {}
    legacy=link.status_observations if connected else {}
    records={**legacy,**raw};contracts={};ages={};metadata={}
    for name,rec in records.items():
        contract=receiver.contracts.evaluate(rec,now=now);contracts[name]=contract
        f=contract['freshness']
        age=f['age_s'] if f['reason'] in ('fresh_local_observation','stale') else None
        ages[name]=age
        metadata[name]={'source':rec['source'],'source_system':rec['source_system'],'source_component':rec['source_component'],
            'source_time':deepcopy(rec['source_time']),'received_monotonic':rec['timestamp_monotonic'],
            'wire_sha256':rec['wire_sha256'],'history_sequence':rec.get('history_sequence'),
            'valid':rec['valid'],'issues':deepcopy(rec['issues']),'freshness':deepcopy(f)}
    def data(name):
        rec=records.get(name)
        return rec['fields'] if rec is not None and rec['valid'] else {}
    def normalized(name):
        c=contracts.get(name,{}).get('normalized',{})
        return c if c.get('available') else None
    def imu_sample(rec):
        c=receiver.contracts.evaluate(rec,now=now);n=c['normalized']
        if not n['available'] or c['freshness']['reason'] not in ('fresh_local_observation','stale') or n['source_clock_domain']!='FCU_BOOT':return None
        return {**{k:n['fields'][k] for k in ('accel_x_m_s2','accel_y_m_s2','accel_z_m_s2','gyro_x_rad_s','gyro_y_rad_s','gyro_z_rad_s')},
            'source':'MAVLink/SCALED_IMU','frame':'BODY_FRD','plant_time_s':n['source_time_s'],
            'time_boot_ms':rec['fields']['time_boot_ms'],'history_sequence':rec['history_sequence'],
            'timestamp_monotonic':rec['timestamp_monotonic'],'wire_sha256':rec['wire_sha256'],
            'session_id':rec['session_id'],'monotonic_domain_id':rec['monotonic_domain_id'],
            'source_time_delta_ms':rec['source_time_delta'],'source_time_relation':rec['source_time_relation']}
    imu=imu_sample(raw['SCALED_IMU']) if 'SCALED_IMU' in raw else None
    attitude=None;n=normalized('ATTITUDE_QUATERNION')
    if n is not None:
        w,x,y,z=n['fields']['quaternion_wxyz']
        attitude={'qw':w,'qx':x,'qy':y,'qz':z,'quaternion_wxyz':[w,x,y,z],
            'roll':math.atan2(2*(w*x+y*z),1-2*(x*x+y*y)),
            'pitch':math.asin(max(-1.,min(1.,2*(w*y-z*x)))),
            'yaw':math.atan2(2*(w*z+x*y),1-2*(y*y+z*z)),
            'roll_rate':n['fields']['gyro_x_rad_s'],'pitch_rate':n['fields']['gyro_y_rad_s'],'yaw_rate':n['fields']['gyro_z_rad_s'],
            'source':'MAVLink/ATTITUDE_QUATERNION','frame':'BODY_FRD_TO_NED','unit_angle':'rad','unit_rate':'rad/s',
            'plant_time_s':n['source_time_s'],'display_offset_applied_to_body':False}
    primary=[rec for rec in receiver.history.records if rec['type']=='SCALED_IMU'] if receiver else []
    selected=[];invalid=0
    if include_imu_samples:
        for rec in primary[-MAX_IMU_SAMPLES:]:
            item=imu_sample(rec)
            if item is None:invalid+=1
            else:selected.append(item)
    history={'requested':include_imu_samples,'max_samples':MAX_IMU_SAMPLES,
        'capacity':receiver.history.capacity if receiver else None,'retained_primary_count':len(primary),
        'total_received_primary':receiver.counts.get('SCALED_IMU',0) if receiver else 0,
        'session_id':receiver.history.session_id if receiver else None,
        'oldest_available_time_boot_ms':primary[0]['fields']['time_boot_ms'] if primary else None,
        'newest_available_time_boot_ms':primary[-1]['fields']['time_boot_ms'] if primary else None,
        'truncated_by_limit':include_imu_samples and len(primary)>MAX_IMU_SAMPLES,'truncated_by_bytes':False,
        'excluded_invalid_samples':invalid,'returned_count':len(selected)}
    position=data('GLOBAL_POSITION_INT');hud=data('VFR_HUD');heart=data('HEARTBEAT');ext=data('EXTENDED_SYS_STATE')
    scaled=lambda values,key,factor:values[key]*factor if finite(values.get(key)) else None
    plant=imu['plant_time_s'] if imu is not None else scaled(position,'time_boot_ms',.001)
    plant_source='MAVLink/SCALED_IMU.time_boot_ms' if imu is not None else 'MAVLink/GLOBAL_POSITION_INT.time_boot_ms' if plant is not None else None
    landed={0:'UNDEFINED',1:'ON_GROUND',2:'IN_AIR',3:'TAKEOFF',4:'LANDING'}.get(ext.get('landed_state'))
    summaries={}
    from .telemetry_history import CHANNELS
    for name in CHANNELS:
        rec=raw.get(name);c=contracts.get(name)
        summaries[name]={'received':rec is not None,'available':bool(c and c['normalized']['available']),
            'reason':c['normalized']['reason'] if c else 'not_received','received_count':receiver.counts.get(name,0) if receiver else 0,
            'sample':None if rec is None else {'source':rec['source'],'source_time':rec['source_time'],
                'history_sequence':rec['history_sequence'],'wire_sha256':rec['wire_sha256']},
            'fresh':c['freshness']['fresh'] if c else False}
    from .command_correlation import SCHEMA as CORRELATION_SCHEMA
    tracker=getattr(link,'command_correlation',None) if connected else None
    correlation=tracker.snapshot(active_command_context) if tracker is not None else {
        'schema':CORRELATION_SCHEMA,'available':False,'reason':'no_connected_command_tracker','record':None,'manual_sent_monotonic':None}
    sent=correlation['record']
    return {**deepcopy(getattr(link,'_native_observation_context',{})),
        'connected':connected,'now':now,'domain':receiver.contracts.domain if receiver else None,
        'session_id':receiver.history.session_id if receiver else None,'ages':ages,'metadata':metadata,'channels':summaries,
        'attitude':attitude,'display_attitude':deepcopy(data('ATTITUDE')),'imu':imu,'imu_samples':selected,'imu_history':history,
        'command_correlation':correlation,'applied_transaction_id':sent['context']['transaction_id'] if sent else None,
        'command_first_sent_monotonic':sent['first_sent_monotonic'] if sent else None,
        'manual_sent_monotonic':correlation['manual_sent_monotonic'],
        'command_first_sent_plant_time_bounds':sent['plant_time_bounds'] if sent else None,
        'applied_pilot_command':deepcopy(sent['applied_pilot_command']) if sent else dict(link.last_manual_pilot) if connected else {},
        'plant_time_s':plant,'plant_time_source':plant_source,'landed_state':landed,
        'groundspeed_m_s':hud.get('groundspeed'),'vertical_speed_m_s':hud.get('climb'),
        'airspeed_m_s':hud.get('airspeed'),'heading_deg':hud.get('heading'),
        'latitude_deg':scaled(position,'lat',1e-7),'longitude_deg':scaled(position,'lon',1e-7),
        'altitude_msl_m':scaled(position,'alt',.001),'relative_altitude_m':scaled(position,'relative_alt',.001),
        'velocity_n_m_s':scaled(position,'vx',.01),'velocity_e_m_s':scaled(position,'vy',.01),'velocity_d_m_s':scaled(position,'vz',.01),
        'fcu_health':deepcopy(data('SYS_STATUS')) or None,'ekf_status':deepcopy(data('EKF_STATUS_REPORT')) or None,
        'battery':{'voltage_v':normalized('SYS_STATUS')['fields']['battery_voltage_v'] if normalized('SYS_STATUS') else None,
            'current_a':normalized('SYS_STATUS')['fields']['battery_current_a'] if normalized('SYS_STATUS') else None,
            'remaining_percent':data('SYS_STATUS').get('battery_remaining') if finite(data('SYS_STATUS').get('battery_remaining')) and 0<=data('SYS_STATUS')['battery_remaining']<=100 else None},
        'servo_output_raw':{k:v for k,v in data('SERVO_OUTPUT_RAW').items() if k.startswith('servo')},
        'mode':link.mode if heart else None,'armed':bool(heart['base_mode'] & 128) if heart else None}


def status_response(factory,request=None):
    request_id=None;include=False
    if request is not None:
        request_id=request.get('request_id')
        if not isinstance(request_id,str) or len(request_id)!=32 or any(c not in '0123456789abcdef' for c in request_id):return status_error('invalid_request_id')
        if set(request)-{'type','request_id','include_imu_samples','status_schema','source','seq','sent_monotonic'}:return status_error('invalid_query',request_id)
        if request.get('status_schema')!=SCHEMA:return status_error('status_schema_mismatch',request_id,expected_schema=SCHEMA,actual_schema=str(request.get('status_schema'))[:80])
        include=request.get('include_imu_samples',False)
        if type(include) is not bool:return status_error('invalid_include_imu_samples',request_id)
    result=factory(include_imu_samples=include);result['request_id']=request_id
    def bounds():
        result['imu_history']['returned_count']=len(result['imu_samples'])
        result['imu_history']['oldest_returned_time_boot_ms']=result['imu_samples'][0]['time_boot_ms'] if result['imu_samples'] else None
        result['imu_history']['newest_returned_time_boot_ms']=result['imu_samples'][-1]['time_boot_ms'] if result['imu_samples'] else None
    bounds()
    while len(encode_status(result))>MAX_BYTES and result['imu_samples']:
        result['imu_samples'].pop(0);result['imu_history']['truncated_by_bytes']=True
        bounds()
    if len(encode_status(result))>MAX_BYTES:return status_error('status_exceeds_datagram_budget',request_id)
    return result
