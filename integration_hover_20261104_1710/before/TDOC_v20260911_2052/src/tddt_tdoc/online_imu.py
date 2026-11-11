"""Strict SI/body-FRD IMU-only projection of the PT_LIB public observation.

Coordinates, ground velocity, Euler attitude and truth wind are deliberately NOT
observations of MESA. A held RunIC uses only its explicit known initialization;
that support-force vector is labelled initialization, not measured sensor evidence.
"""
from __future__ import annotations
import math
import hashlib
import time
from dataclasses import dataclass, field
from typing import Mapping

CHANNELS={'imu_gx':'gyro_x_rad_s','imu_gy':'gyro_y_rad_s','imu_gz':'gyro_z_rad_s',
          'imu_ax':'accel_x_m_s2','imu_ay':'accel_y_m_s2','imu_az':'accel_z_m_s2'}
_HISTORY_SEAL=object()


@dataclass(frozen=True)
class NativeIMUHistory:
    """Process-local receipt of decoded original history, never a JSON permit."""
    session_id: str
    monotonic_domain_id: str
    target_system: int
    target_component: int
    after_seq: int
    through_seq: int
    samples: tuple
    page_sha256: tuple
    original_gui_page_sha256: tuple
    retention_origin: str
    samples_sha256: str
    _seal: object = field(repr=False,compare=False)


def prepare_native_imu_history(pages, *, expected_session_id, expected_monotonic_domain_id,
                               target_system, target_component, after_seq):
    """Decode authentic retained IMU records before requesting the final fresh state.

    Pages may come from the GUI or a process-local ring retaining its original
    primary records/contracts. Wrapper provenance is explicit; every wire, source
    identity, primary predecessor and clock relation is checked independently.
    Old receipts are historical integration observations, never relabelled fresh.
    """
    from layer6.telemetry_normalization import normalize
    from layer6.telemetry_source import native_source_contract
    from pymavlink.dialects.v20 import ardupilotmega as dialect
    from .utils import sha256_obj
    if (not isinstance(pages,(list,tuple)) or len(pages)>1024
        or type(after_seq) is not int or after_seq<0):
        raise ValueError('Native IMU history requires bounded actual pages/cursor')
    contract=native_source_contract()
    if contract.get('deployment_verified') is not True:
        raise ValueError('Native IMU history source frame is not verified')
    if not pages:
        return NativeIMUHistory(expected_session_id,expected_monotonic_domain_id,target_system,target_component,
            after_seq,after_seq,(),(),(),'empty-retained-history-window',sha256_obj([]),_HISTORY_SEAL)
    cursor=after_seq;through=None;previous_seq=after_seq;previous_stamp=None;previous_receipt=None
    samples=[];page_hashes=[];original_hashes=[];origin=None
    for index,page in enumerate(pages):
        if not isinstance(page,Mapping):raise ValueError('invalid Native IMU history page')
        boundary=page.get('through_seq')
        if through is None:through=boundary
        provenance=page.get('retention_origin','Layer6/GUI/telemetry_history')
        if origin is None:origin=provenance
        if (page.get('schema')!='Layer6-telemetry-history-v1' or page.get('ok') is not True
            or page.get('session_id')!=expected_session_id or page.get('channels')!=['SCALED_IMU']
            or page.get('after_seq')!=cursor or type(boundary) is not int or boundary<cursor
            or boundary!=through or page.get('retention_gap') is not False
            or page.get('lost_unfiltered_samples')!=0 or provenance!=origin):
            raise ValueError('Native IMU history page identity/cursor/retention mismatch')
        records=page.get('samples') or [];contracts=page.get('sample_contracts') or []
        if not isinstance(records,list) or len(records)!=len(contracts):
            raise ValueError('Native IMU history raw/contract pairing missing')
        for record,observed_contract in zip(records,contracts):
            if not isinstance(record,Mapping) or not isinstance(observed_contract,Mapping):
                raise ValueError('Native IMU history record/contract is not an object')
            seq=record.get('history_sequence');source=record.get('source_time') or {}
            raw=record.get('fields') or {};stamp=raw.get('time_boot_ms');received=record.get('timestamp_monotonic')
            if (record.get('type')!='SCALED_IMU' or record.get('source')!='MAVLink/SCALED_IMU'
                or record.get('valid') is not True or record.get('session_id')!=expected_session_id
                or record.get('monotonic_domain_id')!=expected_monotonic_domain_id
                or (record.get('source_system'),record.get('source_component'))!=(target_system,target_component)
                or type(seq) is not int or not previous_seq<seq<=through
                or (previous_seq>0 and record.get('previous_channel_sequence')!=previous_seq)
                or type(stamp) is not int or not 0<=stamp<2**32
                or source!={'field':'time_boot_ms','value':stamp,'unit':'ms','clock_domain':'FCU_BOOT'}
                or record.get('source_time_relation') not in ('first','forward')
                or (record.get('freshness_basis') or {}).get('clock_invalid') is not False
                or previous_stamp is not None and stamp<=previous_stamp
                or not isinstance(received,(int,float)) or isinstance(received,bool) or not math.isfinite(received)
                or not 0<=received<=time.monotonic() or previous_receipt is not None and received<previous_receipt):
                raise ValueError('Native IMU history raw source/sequence/clock mismatch')
            if previous_stamp is not None and record.get('source_time_delta')!=stamp-previous_stamp:
                raise ValueError('Native IMU history source interval mismatch')
            try:
                wire=bytes.fromhex(record['wire_hex'])
                decoded=dialect.MAVLink(None).parse_char(wire)
            except Exception as exc:raise ValueError('Native IMU history wire decode failed') from exc
            if (hashlib.sha256(wire).hexdigest()!=record.get('wire_sha256') or decoded is None
                or decoded.get_type()!='SCALED_IMU'
                or (decoded.get_srcSystem(),decoded.get_srcComponent())!=(target_system,target_component)
                or any(raw.get(k)!=getattr(decoded,k,None) for k in
                       ('time_boot_ms','xacc','yacc','zacc','xgyro','ygyro','zgyro'))):
                raise ValueError('Native IMU history wire/payload mismatch')
            normalized=normalize(record,contract)
            supplied=observed_contract.get('normalized') or {}
            freshness=observed_contract.get('freshness') or {}
            if (normalized.get('available') is not True or normalized.get('frame')!='BODY_FRD'
                or normalized.get('source_clock_domain')!='FCU_BOOT'
                or supplied.get('available') is not True or supplied.get('frame')!='BODY_FRD'
                or supplied.get('source_time_s')!=stamp*.001
                or freshness.get('reason') not in ('fresh_local_observation','stale')
                or any(supplied.get('fields',{}).get(k)!=normalized['fields'][k] for k in CHANNELS.values())):
                raise ValueError('Native IMU history SI/frame/clock contract mismatch')
            samples.append({**{k:normalized['fields'][k] for k in CHANNELS.values()},
                'source':'MAVLink/SCALED_IMU','frame':'BODY_FRD','plant_time_s':stamp*.001,
                'time_boot_ms':stamp,'history_sequence':seq,'previous_channel_sequence':record.get('previous_channel_sequence'),
                'timestamp_monotonic':received,'wire_sha256':record['wire_sha256'],'wire_hex':record['wire_hex'],
                'session_id':expected_session_id,'monotonic_domain_id':expected_monotonic_domain_id,
                'source_time_delta_ms':record.get('source_time_delta'),'source_time_relation':record.get('source_time_relation')})
            previous_seq=seq;previous_stamp=stamp;previous_receipt=received
            if len(samples)>8192:raise ValueError('Native IMU primary history exceeds bounded retention')
        next_cursor=page.get('next_after_seq')
        if type(next_cursor) is not int or not cursor<=next_cursor<=through:
            raise ValueError('Native IMU history continuation cursor invalid')
        if page.get('has_more') is True:
            if not records or next_cursor!=records[-1]['history_sequence'] or index==len(pages)-1:
                raise ValueError('Native IMU history pagination incomplete')
        elif page.get('has_more') is not False or next_cursor!=through or index!=len(pages)-1:
            raise ValueError('Native IMU history snapshot boundary incomplete')
        cursor=next_cursor;page_hashes.append(sha256_obj(page))
        original_hashes.extend(page.get('original_gui_page_sha256') or [page_hashes[-1]])
    return NativeIMUHistory(expected_session_id,expected_monotonic_domain_id,target_system,target_component,
                            after_seq,through,tuple(samples),tuple(page_hashes),tuple(dict.fromkeys(original_hashes)),
                            origin,sha256_obj(samples),_HISTORY_SEAL)


def native_imu_history_samples(history):
    """Export verified historical samples as plain JSON data, preserving clocks."""
    from .utils import sha256_obj
    if (not isinstance(history,NativeIMUHistory) or history._seal is not _HISTORY_SEAL
        or sha256_obj(history.samples)!=history.samples_sha256):
        raise ValueError('Native IMU history must originate from the local read-only provider')
    return [dict(sample) for sample in history.samples]


def extend_native_imu_history(previous, pages):
    """Verify a new retained suffix without decoding the verified prefix again.

    A slow initial transfer may outlive the GUI's short current-state buffer.
    The provider can then retain the intervening original records and verify
    only those new pages before requesting another fresh snapshot. This does
    not repair an unavailable source interval or change any source timestamp.
    """
    from .utils import sha256_obj
    if (not isinstance(previous,NativeIMUHistory) or previous._seal is not _HISTORY_SEAL
        or len(previous.samples)>8192 or len(previous.page_sha256)>1024
        or len(previous.original_gui_page_sha256)>8192
        or type(previous.after_seq) is not int or type(previous.through_seq) is not int
        or not 0<=previous.after_seq<=previous.through_seq
        or sha256_obj(previous.samples)!=previous.samples_sha256):
        raise ValueError('Native IMU history extension requires the unchanged bounded local receipt')
    suffix=prepare_native_imu_history(pages,expected_session_id=previous.session_id,
        expected_monotonic_domain_id=previous.monotonic_domain_id,
        target_system=previous.target_system,target_component=previous.target_component,
        after_seq=previous.through_seq)
    if (len(previous.samples)+len(suffix.samples)>8192
        or len(previous.page_sha256)+len(suffix.page_sha256)>1024):
        raise ValueError('Native IMU history extension exceeds bounded retention')
    if not suffix.samples:
        if suffix.through_seq!=previous.through_seq:
            raise ValueError('Native IMU history extension advanced without primary observations')
        return previous
    if previous.samples:
        last=previous.samples[-1];first=suffix.samples[0]
        if (last['history_sequence']!=previous.through_seq
            or first['previous_channel_sequence']!=last['history_sequence']
            or first['time_boot_ms']<=last['time_boot_ms']
            or first['source_time_delta_ms']!=first['time_boot_ms']-last['time_boot_ms']
            or first['timestamp_monotonic']<last['timestamp_monotonic']
            or previous.retention_origin!=suffix.retention_origin):
            raise ValueError('Native IMU history extension source boundary mismatch')
    elif previous.after_seq!=previous.through_seq:
        raise ValueError('Native IMU history extension prefix has no primary boundary')
    originals=tuple(dict.fromkeys((*previous.original_gui_page_sha256,*suffix.original_gui_page_sha256)))
    if len(originals)>8192:
        raise ValueError('Native IMU history extension source lineage exceeds bounded retention')
    samples=previous.samples+suffix.samples
    return NativeIMUHistory(previous.session_id,previous.monotonic_domain_id,
        previous.target_system,previous.target_component,previous.after_seq,suffix.through_seq,
        samples,previous.page_sha256+suffix.page_sha256,originals,suffix.retention_origin,
        sha256_obj(samples),_HISTORY_SEAL)


def attach_native_imu_history(ingress, history, *, after_seq, last_time, max_dt):
    """Merge exact retained observations and the final fresh 64-sample tail."""
    from .utils import sha256_obj
    retained=native_imu_history_samples(history)
    gui=ingress.get('gui_state') or {};current=gui.get('imu') or {}
    if (history.after_seq!=after_seq or history.session_id!=gui.get('session_id')
        or history.monotonic_domain_id!=gui.get('monotonic_domain_id')
        or (history.target_system,history.target_component)!=(gui.get('target_system'),gui.get('target_component'))):
        raise ValueError('Native IMU history/final observation session mismatch')
    end=_finite(current.get('plant_time_s'),'final Native IMU time')
    previous_seq=after_seq;previous_stamp=last_time;merged=[];by_sequence={}
    compare=('plant_time_s','wire_sha256','session_id','monotonic_domain_id',*CHANNELS.values())
    for group in (retained,ingress.get('imu_samples') or []):
        group_previous=None
        for original in group:
            sample=dict(original);seq=sample.get('history_sequence');stamp=_finite(sample.get('plant_time_s'),'history source time')
            if type(seq) is not int or group_previous is not None and seq<=group_previous:
                raise ValueError('Native IMU history/tail source order changed')
            group_previous=seq
            if seq<=after_seq:
                if stamp>last_time:raise ValueError('Native IMU cursor/time mismatch')
                continue
            if seq in by_sequence:
                if any(sample.get(k)!=by_sequence[seq].get(k) for k in compare):
                    raise ValueError('Native IMU history/tail overlapping wire values differ')
                continue
            if seq<=previous_seq or stamp>end or stamp<=previous_stamp:
                raise ValueError('Native IMU history/tail clock or cursor gap')
            dt=stamp-previous_stamp
            if not 1e-5<=dt<=max_dt+1e-8:
                raise ValueError('Native buffered IMU interval exceeds operation-5 grant')
            if sample.get('source_time_delta_ms') is not None and abs(float(sample['source_time_delta_ms'])*.001-dt)>1e-8:
                raise ValueError('Native IMU history has an omitted primary observation')
            merged.append(sample);by_sequence[seq]=sample;previous_seq=seq;previous_stamp=stamp
    if previous_stamp!=end or previous_seq!=current.get('history_sequence'):
        raise ValueError('Native IMU retained history does not reach the final fresh sample')
    result=dict(ingress);result.pop('native_imu_history',None);result.pop('imu_history_pages',None)
    result['imu_samples']=merged
    result['native_imu_history_provenance']={'schema':'TDOC-Native-measured-IMU-history-v1',
        'session_id':history.session_id,'after_seq':history.after_seq,'through_seq':history.through_seq,
        'final_sequence':previous_seq,'sample_count':len(merged),'retention_origin':history.retention_origin,
        'retained_wrapper_sha256':list(history.page_sha256),'measured_samples_sha256':sha256_obj(merged),
        'original_gui_page_sha256':list(history.original_gui_page_sha256),
        'interpolation':False,'resampling':False,'clock_rebased':False,'max_step_s':max_dt}
    return result


def _finite(value, name: str) -> float:
    if isinstance(value,bool) or not isinstance(value,(int,float)):
        raise ValueError(f'invalid Online IMU field: {name}')
    result=float(value)
    if not math.isfinite(result):
        raise ValueError(f'non-finite Online IMU field: {name}')
    return result


def canonical_imu(ingress: Mapping, initialization: Mapping | None=None) -> dict:
    if ingress.get('ok') is not True:
        raise ValueError('Online IMU requires a valid PT_LIB ingress')
    state=ingress.get('state') or {}
    native=ingress.get('source')=='Layer6/GUI/MAVLink'
    gui=ingress.get('gui_state') or {}
    if native and ('schema' in gui or 'telemetry_metadata' in gui):
        from layer6.status import validate_observation_status
        check=validate_observation_status(gui)
        if not check['ok']:raise ValueError('Native Online IMU observations rejected: '+check['reason'])
        current=gui.get('imu') or {}
        if (state.get('imu_time_s')!=current.get('plant_time_s')
            or any(state.get(k)!=current.get(k) for k in CHANNELS.values())):
            raise ValueError('Native Online IMU state differs from current observation')
    plant_time=_finite(state.get('imu_time_s') if native else state.get('sim_time_s'),'IMU plant time')
    if plant_time<0:raise ValueError('negative Online IMU plant time')
    if native and (state.get('imu_frame')!='BODY_FRD' or state.get('imu_source')!='MAVLink/SCALED_IMU'):
        raise ValueError('Native Online IMU requires measured BODY_FRD SCALED_IMU')
    if state.get('runtime_ready') != 1.0:
        raise ValueError('Online IMU runtime is not ready')
    if ingress.get('released'):
        values={target:_finite(state.get(source),source) for target,source in CHANNELS.items()}
        source='Layer6/GUI/MAVLink/SCALED_IMU/body_FRD' if native else 'PT_LIB/JSBSim/body_FRD_IMU'
        observed=True
    else:
        if not initialization:
            raise ValueError('Held plant requires explicit known navigation initialization')
        w,x,y,z=(_finite(initialization[k],k) for k in ('qw','qx','qy','qz'))
        norm=math.sqrt(w*w+x*x+y*y+z*z)
        if norm<1e-9: raise ValueError('invalid initialization quaternion')
        w,x,y,z=(v/norm for v in (w,x,y,z))
        # R_body_to_NED^T * [0,0,-g], stationary support reaction.
        g=9.80665
        values={'imu_gx':0.,'imu_gy':0.,'imu_gz':0.,
                'imu_ax':-g*2*(x*z-w*y),'imu_ay':-g*2*(y*z+w*x),
                'imu_az':-g*(1-2*(x*x+y*y))}
        source='HELD_RunIC_known_stationary_initialization_only'
        observed=False
    return {'schema':'TDOC-Online-MESA-IMU-v2','values':values,'plant_time_s':plant_time,
            'source':source,'measured_simulated_imu':observed,'initialization_only':not observed,
            'unit_acceleration':'m/s2','unit_gyro':'rad/s','frame':'BODY_FRD',
            'navigation_authority':'MESA_STD','truth_position_used':False,
            'truth_velocity_used':False,'gnss_used':False,'not_field_evidence':True}


def initial_ground_attitude(ingress, known_initialization):
    """Align roll/pitch once from actual ground IMU, retaining surveyed yaw/position.

    This consumes no FCU attitude/position/velocity. Ground contact only qualifies
    lifecycle; it never re-anchors a running estimator or adjusts measured samples.
    """
    from .native_control_binding import ground_contact
    packet=canonical_imu(ingress,known_initialization)
    gui=ingress.get('gui_state') or {}
    if (ingress.get('source')!='Layer6/GUI/MAVLink' or not packet['measured_simulated_imu']
        or gui.get('armed') is not False or gui.get('throttle')!=0. or not ground_contact(gui)):
        raise ValueError('initial gravity alignment requires disarmed zero-throttle measured ground state')
    values=packet['values'];ax,ay,az=(values[k] for k in ('imu_ax','imu_ay','imu_az'))
    g=math.sqrt(ax*ax+ay*ay+az*az)
    rate=math.sqrt(sum(values[k]**2 for k in ('imu_gx','imu_gy','imu_gz')))
    if abs(g-9.80665)>.05*9.80665 or rate>.02:
        raise ValueError('initial gravity alignment requires low-motion support-force IMU')
    w,x,y,z=(_finite(known_initialization[k],k) for k in ('qw','qx','qy','qz'))
    norm=math.sqrt(w*w+x*x+y*y+z*z)
    if norm<1e-9:raise ValueError('invalid known initial attitude')
    w,x,y,z=(q/norm for q in (w,x,y,z))
    yaw=math.atan2(2*(w*z+x*y),1-2*(y*y+z*z))
    roll=math.atan2(-ay,-az);pitch=math.atan2(ax,math.hypot(ay,az))
    cr,sr=math.cos(roll/2),math.sin(roll/2);cp,sp=math.cos(pitch/2),math.sin(pitch/2)
    cy,sy=math.cos(yaw/2),math.sin(yaw/2)
    quaternion=(cr*cp*cy+sr*sp*sy,sr*cp*cy-cr*sp*sy,cr*sp*cy+sr*cp*sy,cr*cp*sy-sr*sp*cy)
    initial={**known_initialization,**dict(zip(('qw','qx','qy','qz'),quaternion))}
    evidence={'schema':'TDOC-initial-ground-IMU-attitude-v1','initialization_only':True,
        'roll_rad':roll,'pitch_rad':pitch,'yaw_rad':yaw,'yaw_source':'mission_known_initialization',
        'source':packet['source'],'source_time_s':packet['plant_time_s'],
        'wire_sha256':(gui.get('imu') or {}).get('wire_sha256'),
        'support_force_m_s2':[ax,ay,az],'angular_rate_norm_rad_s':rate,
        'original_quaternion_wxyz':[known_initialization[k] for k in ('qw','qx','qy','qz')],
        'aligned_quaternion_wxyz':list(quaternion),'gnss_used':False,'FCU_attitude_used':False,
        'raw_samples_changed':False,'continuous_reanchoring':False}
    return initial,evidence


def measured_intervals(ingress, last_time, packet, max_dt):
    """Replay buffered native IMU samples through the existing operation 5.

    This changes transport cadence only. Every integration step uses an actual
    timestamped six-channel sample; gaps retain the existing fail-closed limit.
    """
    if ingress.get('source') != 'Layer6/GUI/MAVLink' or last_time is None:
        return None
    end=_finite(packet['plant_time_s'],'current IMU time');last_time=_finite(last_time,'previous IMU time');max_dt=_finite(max_dt,'IMU grant')
    if end<0 or last_time<0 or end<last_time or max_dt<1e-5:
        raise ValueError('Invalid/reversed native IMU time or grant')
    samples=[];previous_source=None
    gui=ingress.get('gui_state') or {};qualified='schema' in gui or 'telemetry_metadata' in gui
    for sample in ingress.get('imu_samples') or []:
        stamp=_finite(sample.get('plant_time_s'),'buffered IMU time')
        if stamp<0 or stamp>end or previous_source is not None and stamp<=previous_source:
            raise ValueError('Unordered/duplicate/future native IMU history; source time is not repaired')
        previous_source=stamp
        if sample.get('frame') != 'BODY_FRD' or sample.get('source') != 'MAVLink/SCALED_IMU':
            raise ValueError('Invalid buffered native IMU source/frame')
        if qualified and (sample.get('session_id')!=gui.get('session_id')
            or sample.get('monotonic_domain_id')!=gui.get('monotonic_domain_id')
            or type(sample.get('time_boot_ms')) is not int or sample['time_boot_ms']*.001!=stamp):
            raise ValueError('Native IMU history source/session/domain mismatch')
        values={target:_finite(sample.get(source),source) for target,source in CHANNELS.items()}
        if last_time<stamp<=end:samples.append((stamp,values))
    current={k:_finite(packet['values'].get(k),k) for k in CHANNELS}
    if samples and samples[-1][0]==end:
        if samples[-1][1]!=current:raise ValueError('Current IMU differs from buffered sample at same source time')
    elif end>last_time:samples.append((end,current))
    if end-last_time<=max_dt+1e-8:return None
    steps=[];previous=last_time
    for stamp, values in samples:
        dt=stamp-previous
        if not 1e-5<=dt<=max_dt+1e-8:
            raise ValueError('Native buffered IMU interval exceeds operation-5 grant')
        steps.append((dt,values));previous=stamp
    return steps


def compact_replay_context(context):
    """Omit duplicate transport buffers from intermediate evidence only.

    Per-sample values/timestamps remain recorded and the final packet retains
    the original buffer. All operating context, TEP and validity checks remain.
    """
    result=dict(context)
    result['online_state_ingress']=dict(context.get('online_state_ingress') or {})
    ingress=dict(result['online_state_ingress'].get('ptlib_ingress') or {})
    ingress.pop('imu_samples',None)
    gui=dict(ingress.get('gui_state') or {})
    gui.pop('imu_samples',None)
    ingress['gui_state']=gui
    result['online_state_ingress']['ptlib_ingress']=ingress
    return result


def wing_inputs_from_navigation(values, mesa_state, climate_state):
    """Same-cycle WingMPET op4 inputs from MESA navigation and MicroMet wind.

    The native FCU has no PT_LIB truth-air-velocity channel. Example cruise inputs
    must not survive that absence, and GPS velocity must not replace navigation.
    """
    wind_e=_finite(climate_state['wind_u_3dw'],'wind E')
    wind_n=_finite(climate_state['wind_v_3dw'],'wind N')
    wind_up=_finite(climate_state['diagnostic_vertical_wind'],'wind Up')
    state={k:_finite(mesa_state[k],k) for k in ('position_n','position_e','position_d',
                                              'velocity_n','velocity_e','velocity_d')}
    values.update(air_velocity_x=state['velocity_e']-wind_e,
                  air_velocity_y=state['velocity_n']-wind_n,
                  air_velocity_h=-state['velocity_d']-wind_up,
                  state_x=state['position_e'],state_y=state['position_n'],state_h=-state['position_d'])
    return {'applied':True,'schema':'TDOC-WingMPET-native-navigation-input-v1',
            'operation_code':4,'source':'same-cycle MESA navigation + MicroMet wind',
            'gnss_used':False,'truth_velocity_used':False}
