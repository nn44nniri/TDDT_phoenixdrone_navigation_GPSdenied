"""SI/body contracts and local receipt/progression freshness, without clock rebase."""
from copy import deepcopy
import math
import time
from .timebase import monotonic_domain

SCHEMA='Layer6-channel-contract-r5.03-v1'
MAX_AGE={name:.15 for name in ('SCALED_IMU','SCALED_IMU2','SCALED_IMU3','RAW_IMU','HIGHRES_IMU')}
MAX_AGE.update(ATTITUDE_QUATERNION=.2,SYS_STATUS=.5,EKF_STATUS_REPORT=.5,SERVO_OUTPUT_RAW=.2,ACTUATOR_OUTPUT_STATUS=.2)
MAX_AGE.update(HEARTBEAT=2.,ATTITUDE=.2,VFR_HUD=.2,GLOBAL_POSITION_INT=.2,EXTENDED_SYS_STATE=.5)
G=9.80665


def finite(x):return not isinstance(x,bool) and isinstance(x,(int,float)) and math.isfinite(x)


def normalize(sample,source_contract):
    name=sample['type'];raw=sample['fields'];fields={};units={};unavailable={}
    verified=(source_contract.get('deployment_verified') is True and source_contract.get('imu_frame')=='BODY_FRD'
              and source_contract.get('attitude_frame')=='BODY_FRD_TO_NED' and source_contract.get('quaternion_order')=='wxyz')
    frame='NOT_SPATIAL';reason=None;source=sample.get('source_time');source_s=None;source_domain='unavailable'
    if source is not None and finite(source.get('value')):
        source_s=source['value']*(.001 if source['unit']=='ms' else .000001)
        source_domain=source['clock_domain']
        if verified and name in ('RAW_IMU','HIGHRES_IMU','SERVO_OUTPUT_RAW'):
            source_domain='FCU_BOOT'  # Pinned AP_HAL::micros64/micros sender, not a magnitude guess.
    def put(target,original,factor=1.,offset=0.,unit='1',valid=None):
        value=raw.get(original)
        if not finite(value) or (valid is not None and not valid(value)):
            fields[target]=None;unavailable[target]='missing_or_protocol_sentinel'
        else:fields[target]=value*factor+offset
        units[target]=unit
    imu=name in ('SCALED_IMU','SCALED_IMU2','SCALED_IMU3','RAW_IMU','HIGHRES_IMU')
    if imu:
        frame='BODY_FRD' if verified else 'UNQUALIFIED';high=name=='HIGHRES_IMU'
        updated=raw.get('fields_updated',0) if high else None
        if high and type(updated) is not int:updated=0
        for group,prefix,factor,unit in [('acc','accel',1. if high else G/1000.,'m/s2'),
            ('gyro','gyro',1. if high else .001,'rad/s'),('mag','magnetic',.0001 if high else .0000001,'T')]:
            for axis in 'xyz':
                target=prefix+'_'+axis+{'acc':'_m_s2','gyro':'_rad_s','mag':'_t'}[group];original=axis+group
                put(target,original,factor,unit=unit)
                bit=('acc','gyro','mag').index(group)*3+'xyz'.index(axis)
                if high and (not isinstance(updated,int) or not updated & (1<<bit)):
                    fields[target]=None;unavailable[target]='field_not_updated'
        if high:
            for original,target,bit in [('abs_pressure','absolute_pressure_pa',9),('diff_pressure','differential_pressure_pa',10)]:
                put(target,original,100.,unit='Pa')
                if not updated & (1<<bit):fields[target]=None;unavailable[target]='field_not_updated'
            put('temperature_k','temperature',offset=273.15,unit='K')
            if not updated & (1<<12):fields['temperature_k']=None;unavailable['temperature_k']='field_not_updated'
            fields['pressure_altitude_m']=None;units['pressure_altitude_m']='m';unavailable['pressure_altitude_m']='unit_not_declared_in_wire_schema'
        else:put('temperature_k','temperature',.01,273.15,'K',valid=lambda v:v!=0)
        if not verified:reason='source_frame_unverified'
        elif any(fields[k] is None for k in ('accel_x_m_s2','accel_y_m_s2','accel_z_m_s2','gyro_x_rad_s','gyro_y_rad_s','gyro_z_rad_s')):reason='imu_axes_unavailable'
    elif name=='ATTITUDE_QUATERNION':
        frame='BODY_FRD_TO_NED' if verified else 'UNQUALIFIED'
        q=[raw.get(k) for k in ('q1','q2','q3','q4')]
        norm=math.sqrt(sum(v*v for v in q)) if all(finite(v) for v in q) else None
        good=norm is not None and abs(norm-1.)<=.001
        fields['quaternion_wxyz']=[v/norm for v in q] if good else None;units['quaternion_wxyz']='1'
        fields['raw_quaternion_norm']=norm;units['raw_quaternion_norm']='1'
        for axis,original in zip('xyz',('rollspeed','pitchspeed','yawspeed')):put('gyro_'+axis+'_rad_s',original,unit='rad/s')
        fields['display_offset_wxyz']=deepcopy(raw.get('repr_offset_q'));units['display_offset_wxyz']='1'
        if not good:reason='quaternion_norm_invalid';unavailable['quaternion_wxyz']=reason
        elif not verified:reason='source_frame_unverified'
    elif name=='SYS_STATUS':
        for k in ('onboard_control_sensors_present','onboard_control_sensors_enabled','onboard_control_sensors_health',
                  'errors_comm','errors_count1','errors_count2','errors_count3','errors_count4'):
            fields[k]=raw.get(k);units[k]='bitmask' if 'sensors' in k else 'count'
        put('load_fraction','load',.001,valid=lambda v:0<=v<=1000)
        put('battery_voltage_v','voltage_battery',.001,unit='V',valid=lambda v:0<=v<65535)
        put('battery_current_a','current_battery',.01,unit='A',valid=lambda v:v>=0)
        put('battery_fraction','battery_remaining',.01,valid=lambda v:0<=v<=100)
        put('communication_drop_fraction','drop_rate_comm',.0001,valid=lambda v:0<=v<=10000)
    elif name=='EKF_STATUS_REPORT':
        fields['flags']=raw.get('flags');units['flags']='bitmask'
        # These AP fields are innovation test ratios, not SI state covariance.
        for k in ('velocity_variance','pos_horiz_variance','pos_vert_variance','compass_variance','terrain_alt_variance','airspeed_variance'):
            put(k,k,valid=lambda v:v>=0)
        if not verified:reason='estimator_field_semantics_unverified'
    elif name=='SERVO_OUTPUT_RAW':
        frame='ACTUATOR_CHANNELS';fields['port']=raw.get('port');units['port']='index'
        for i in range(1,17):put('servo%d_pulse_s'%i,'servo%d_raw'%i,.000001,unit='s',valid=lambda v:0<v<65535)
        if not any(fields['servo%d_pulse_s'%i] is not None for i in range(1,17)):reason='actuator_outputs_unavailable'
    elif name=='ACTUATOR_OUTPUT_STATUS':
        frame='ACTUATOR_CHANNELS';active=raw.get('active');values=raw.get('actuator')
        fields['active']=active;units['active']='bitmask';units['outputs']='1'
        fields['outputs']=[v if finite(v) and -1<=v<=1 and isinstance(active,int) and active&(1<<i) else None for i,v in enumerate(values or [])]
        if not any(v is not None for v in fields['outputs']):reason='actuator_outputs_unavailable'
    else:reason='unsupported_channel'
    if not sample['valid']:reason='invalid_raw_sample'
    return {'schema':SCHEMA,'available':reason is None,'reason':reason or 'converted',
        'frame':frame,'units':units,'fields':fields,'unavailable_fields':unavailable,
        'source_time_s':source_s,'source_clock_domain':source_domain,
        'quaternion_convention':'Hamilton_wxyz; v_NED=R(q)*v_BODY_FRD' if name=='ATTITUDE_QUATERNION' else None,
        'display_offset_applied_to_body':False,'gravity_removed':False,
        'mechanical_actuator_measurement':False,'source_contract_id':source_contract.get('contract_id'),
        'local_deployment_verified':verified,'peer_attested':False,'navigation_observation_authorized':False,
        'role':'FCU_reported_inertial_sensor' if imu else 'FCU_reference_only',
        'sensor_presence_verified':False,'source_time_is_sensor_capture_verified':False,'guidance_state_qualified':False}


class ChannelContracts:
    def __init__(self,source_contract=None):
        self.source_contract=deepcopy(source_contract or {});self.domain=monotonic_domain();self.clocks={}
        self.last_received=None;self.receipt_invalid=False

    def observe(self,sample):
        name=sample['type'];stamp=sample.get('source_time');received=sample['timestamp_monotonic']
        self.receipt_invalid=self.receipt_invalid or not finite(received) or received<0 or (self.last_received is not None and received<self.last_received)
        self.last_received=received
        previous=self.clocks.get(name);value=stamp.get('value') if stamp else None
        if value is None:return {'anchor_monotonic':received,'source_progress':'source_time_not_supplied','clock_invalid':self.receipt_invalid}
        bad=self.receipt_invalid or not finite(value) or value<0 or (stamp['field']=='time_boot_ms' and value>=2**32)
        invalid=bad or (previous is not None and (previous['clock_invalid'] or value<previous['value']))
        duplicate=previous is not None and value==previous['value']
        anchor=previous['anchor_monotonic'] if duplicate or invalid and previous is not None else received
        result={'value':value,'anchor_monotonic':anchor,'clock_invalid':invalid,
                'source_progress':'reversed_or_invalid_latched' if invalid else 'duplicate' if duplicate else 'advanced' if previous else 'first'}
        if previous is not None and invalid:result['value']=max(value,previous['value'])
        self.clocks[name]=result;return deepcopy(result)

    def evaluate(self,sample,*,now=None,now_domain=None):
        now=time.monotonic() if now is None else now;domain=self.domain if now_domain is None else now_domain
        basis=sample['freshness_basis'];receipt=sample['timestamp_monotonic'];anchor=basis['anchor_monotonic']
        valid_clock=(finite(now) and finite(receipt) and finite(anchor) and 0<=anchor<=receipt<=now
                     and sample.get('monotonic_domain_id')==domain)
        receipt_age=now-receipt if valid_clock else None;age=now-anchor if valid_clock else None
        limit=MAX_AGE[sample['type']]
        invalid_source=basis['clock_invalid'] or self.receipt_invalid or self.clocks.get(sample['type'],{}).get('clock_invalid',False)
        reason='invalid_monotonic_clock_or_domain' if not valid_clock else 'invalid_source_clock' if invalid_source else 'invalid_raw_sample' if not sample['valid'] else 'stale' if age>limit else 'fresh_local_observation'
        return {'history_sequence':sample.get('history_sequence'),'wire_sha256':sample['wire_sha256'],
            'normalized':normalize(sample,self.source_contract),
            'freshness':{'fresh':reason=='fresh_local_observation','reason':reason,'age_s':age,'receipt_age_s':receipt_age,
                'max_age_s':limit,'source_progress':basis['source_progress'],'source_age_s':None,
                'source_latency_verified':False,'clock_rate_assumed':False,'source_time_rebased':False,
                'basis':'local_monotonic_receipt_and_source_progress','monotonic_domain_id':self.domain,
                'evaluated_monotonic':now if finite(now) else None},'guidance_state_qualified':False}
