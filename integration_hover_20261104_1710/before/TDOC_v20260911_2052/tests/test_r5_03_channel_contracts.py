"""Independent SI, quaternion/frame and freshness acceptance for real wire fixtures."""
from copy import deepcopy
from pathlib import Path
import json
import pytest
from scipy.spatial.transform import Rotation
from test_r5_01_telemetry_receivers import wire_message
from layer6.telemetry import TelemetryReceiver
from layer6.telemetry_source import native_source_contract
from layer6.telemetry_normalization import ChannelContracts,normalize,MAX_AGE,G

ROOT=Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def source():
    result=native_source_contract();assert result['deployment_verified'];return result


def sample(name='SCALED_IMU',changes=None,source=None):
    r=TelemetryReceiver(1,1,source_contract=source)
    rec=r.ingest(wire_message(name,changes=changes));return r,rec


@pytest.mark.parametrize('name',['SCALED_IMU','SCALED_IMU2','SCALED_IMU3','RAW_IMU'])
def test_scaled_units_specific_force_no_gravity_subtraction(name,source):
    r,s=sample(name,{'xacc':1000,'yacc':-1000,'zacc':0,'xgyro':1000,'xmag':1000,'temperature':1000},source)
    n=normalize(s,source);f=n['fields'];assert n['available'] and n['frame']=='BODY_FRD'
    assert f['accel_x_m_s2']==G and f['accel_y_m_s2']==-G and f['accel_z_m_s2']==0
    assert f['gyro_x_rad_s']==1. and f['magnetic_x_t']==pytest.approx(.0001)
    assert f['temperature_k']==283.15 and not n['gravity_removed']
    assert s['fields']['xacc']==1000 and s['units']['xacc']=='mG'


def test_highres_units_and_update_mask_do_not_make_unupdated_fields_current(source):
    _,s=sample('HIGHRES_IMU',{'fields_updated':63|512|4096,'xacc':1.5,'xgyro':.25,'xmag':2.,
        'abs_pressure':1000.,'temperature':0.},source)
    n=normalize(s,source);f=n['fields'];assert n['available']
    assert f['accel_x_m_s2']==1.5 and f['gyro_x_rad_s']==.25 and f['magnetic_x_t'] is None
    assert f['absolute_pressure_pa']==100000. and f['temperature_k']==273.15
    assert f['differential_pressure_pa'] is None and f['pressure_altitude_m'] is None


def test_invalid_highres_update_mask_remains_unavailable_and_does_not_break_export(source):
    r=TelemetryReceiver(1,1,source_contract=source);message=wire_message('HIGHRES_IMU');del message.fields_updated
    record=r.ingest(message);assert not record['valid']
    page=r.history.page();assert not page['sample_contracts'][0]['normalized']['available']
    json.dumps(page,allow_nan=False)


@pytest.mark.parametrize('time_field,name,value,seconds',[('time_boot_ms','SCALED_IMU',1500,1.5),
    ('time_usec','RAW_IMU',1500000,1.5),('time_usec','SERVO_OUTPUT_RAW',1500000,1.5)])
def test_source_clock_seconds_are_exact_and_no_epoch_guess(time_field,name,value,seconds,source):
    _,s=sample(name,{time_field:value},source);n=normalize(s,source)
    assert n['source_time_s']==seconds and n['source_clock_domain']=='FCU_BOOT'
    if time_field=='time_usec':assert s['source_time']['clock_domain']=='message_native_origin_unqualified'


def test_unknown_usec_origin_remains_unqualified_without_sender_evidence():
    _,s=sample('RAW_IMU');n=normalize(s,{})
    assert n['source_clock_domain']=='message_native_origin_unqualified' and not n['available']


def test_quaternion_body_rotation_is_independent_of_display_offset(source):
    rot=Rotation.from_euler('y',90,degrees=True);x,y,z,w=rot.as_quat()
    _,s=sample('ATTITUDE_QUATERNION',{'q1':w,'q2':x,'q3':y,'q4':z,'repr_offset_q':[.7071068,0.,-.7071068,0.]},source)
    n=normalize(s,source);q=n['fields']['quaternion_wxyz'];assert n['available'] and n['frame']=='BODY_FRD_TO_NED'
    measured=Rotation.from_quat([q[1],q[2],q[3],q[0]]).apply([1.,0.,0.])
    assert measured==pytest.approx([0.,0.,-1.],abs=1e-6)
    assert not n['display_offset_applied_to_body'] and q!=n['fields']['display_offset_wxyz']


@pytest.mark.parametrize('q',[{'q1':2.,'q2':0.,'q3':0.,'q4':0.},
    {'q1':0.,'q2':0.,'q3':0.,'q4':0.},{'q1':float('nan')}])
def test_invalid_quaternion_is_not_repaired_to_identity(q,source):
    _,s=sample('ATTITUDE_QUATERNION',q,source);n=normalize(s,source)
    assert not n['available'] and n['fields']['quaternion_wxyz'] is None
    json.dumps(n,allow_nan=False)


def test_only_small_quaternion_roundoff_is_normalized_and_raw_value_survives(source):
    _,s=sample('ATTITUDE_QUATERNION',{'q1':1.00005,'q2':0.,'q3':0.,'q4':0.},source)
    n=normalize(s,source);assert n['available'] and n['fields']['quaternion_wxyz']==[1.,0.,0.,0.]
    assert s['fields']['q1']>1 and n['fields']['raw_quaternion_norm']>1


def test_changed_or_unverified_deployment_cannot_claim_body_frame(tmp_path,source):
    bad=native_source_contract(tmp_path);assert not bad['deployment_verified']
    for name in ('SCALED_IMU','ATTITUDE_QUATERNION'):
        _,s=sample(name);n=normalize(s,bad);assert not n['available'] and n['frame']=='UNQUALIFIED'
    changed={**source,'imu_frame':'VTOL_VIEW'}
    assert not normalize(sample()[1],changed)['available']


def test_health_units_and_unknown_battery_sentinels(source):
    _,s=sample('SYS_STATUS',{'load':500,'voltage_battery':65535,'current_battery':-1,'battery_remaining':-1,'drop_rate_comm':250},source)
    n=normalize(s,source);f=n['fields'];assert n['available'] and n['frame']=='NOT_SPATIAL'
    assert f['load_fraction']==.5 and f['communication_drop_fraction']==.025
    assert all(f[k] is None for k in ('battery_voltage_v','battery_current_a','battery_fraction'))
    assert n['source_time_s'] is None and s['source_time'] is None


def test_ekf_test_ratios_are_dimensionless_not_state_covariance(source):
    _,s=sample('EKF_STATUS_REPORT',{'velocity_variance':.5},source);n=normalize(s,source)
    assert n['available'] and n['units']['velocity_variance']=='1' and n['fields']['velocity_variance']==.5


def test_servo_seconds_preserve_unknown_extensions_and_do_not_invent_angles(source):
    _,s=sample('SERVO_OUTPUT_RAW',{'servo1_raw':1500,'servo2_raw':0,'servo3_raw':65535},source)
    n=normalize(s,source);assert n['fields']['servo1_pulse_s']==.0015
    assert n['fields']['servo2_pulse_s'] is None and n['fields']['servo3_pulse_s'] is None
    assert not n['mechanical_actuator_measurement'] and n['frame']=='ACTUATOR_CHANNELS'


def test_actuator_active_mask_is_not_a_default_for_inactive_outputs(source):
    _,s=sample('ACTUATOR_OUTPUT_STATUS',{'active':5,'actuator':[.5]*32},source);n=normalize(s,source)
    assert n['fields']['outputs'][:4]==[.5,None,.5,None]
    assert n['units']['outputs']=='1' and not n['mechanical_actuator_measurement']


def timed(ctx,*,source_ms=1000,received=100.,name='SCALED_IMU'):
    _,s=sample(name,{'time_boot_ms':source_ms});s['timestamp_monotonic']=received
    s['monotonic_domain_id']=ctx.domain;s['freshness_basis']=ctx.observe(s);return s


def test_duplicate_source_packets_cannot_refresh_freshness(source):
    ctx=ChannelContracts(source);timed(ctx);duplicate=timed(ctx,received=100.1)
    f=ctx.evaluate(duplicate,now=100.16)['freshness']
    assert not f['fresh'] and f['reason']=='stale' and f['age_s']==pytest.approx(.16)
    assert f['receipt_age_s']==pytest.approx(.06) and f['source_age_s'] is None
    fresh=timed(ctx,source_ms=1020,received=100.2)
    assert ctx.evaluate(fresh,now=100.205)['freshness']['fresh']


def test_source_reverse_or_wrap_latches_until_new_receiver_session(source):
    ctx=ChannelContracts(source);earlier=timed(ctx,source_ms=2**32-1)
    wrapped=timed(ctx,source_ms=1,received=100.01)
    assert ctx.evaluate(wrapped,now=100.02)['freshness']['reason']=='invalid_source_clock'
    assert not ctx.evaluate(earlier,now=100.02)['freshness']['fresh']
    later=timed(ctx,source_ms=2**32-1,received=100.03)
    assert not ctx.evaluate(later,now=100.04)['freshness']['fresh']
    fresh_ctx=ChannelContracts(source);new=timed(fresh_ctx,source_ms=1)
    assert fresh_ctx.evaluate(new,now=100.01)['freshness']['fresh']


@pytest.mark.parametrize('now',[-1.,99.99,float('nan'),float('inf')])
def test_bad_or_future_monotonic_time_is_not_fresh(now,source):
    ctx=ChannelContracts(source);s=timed(ctx);f=ctx.evaluate(s,now=now)['freshness']
    assert not f['fresh'] and f['age_s'] is None
    json.dumps(f,allow_nan=False)


def test_monotonic_domain_mismatch_and_receive_clock_reverse_are_rejected(source):
    ctx=ChannelContracts(source);s=timed(ctx)
    assert not ctx.evaluate(s,now=100.01,now_domain='another-host-boot')['freshness']['fresh']
    reversed_receive=timed(ctx,source_ms=1020,received=99.99)
    assert not ctx.evaluate(reversed_receive,now=100.01)['freshness']['fresh']


@pytest.mark.parametrize('name',['SYS_STATUS','EKF_STATUS_REPORT'])
def test_health_freshness_is_receipt_only_with_unknown_source_latency(name,source):
    ctx=ChannelContracts(source);s=timed(ctx,name=name)
    f=ctx.evaluate(s,now=100.1)['freshness'];assert f['fresh']
    assert f['source_progress']=='source_time_not_supplied' and not f['source_latency_verified']
    assert not ctx.evaluate(s,now=100.51)['freshness']['fresh']


def test_snapshot_and_public_history_export_separate_contracts_without_mutating_raw(source):
    r,s=sample(source=source);saved=deepcopy(s)
    p=r.history.page();c=p['sample_contracts'][0];assert p['samples'][0]==saved
    assert c['wire_sha256']==saved['wire_sha256'] and c['history_sequence']==saved['history_sequence']
    assert c['freshness']['age_s']==pytest.approx(c['freshness']['evaluated_monotonic']-saved['timestamp_monotonic'])
    assert r.snapshot()['SCALED_IMU']['contract']['normalized']['frame']=='BODY_FRD'
    assert r.history.records[0]==saved and not c['guidance_state_qualified']


def test_age_limits_match_current_tdoc_instead_of_relaxing_gates():
    import yaml
    config=yaml.safe_load((ROOT/'TDOC_v20260911_2052/configs/tdoc.yaml').read_text())
    limits=config['layers']['layer6']['online'] if 'layers' in config else config['layer6']['online']
    assert MAX_AGE['SCALED_IMU']==limits['imu_max_age_s']==.15
    assert MAX_AGE['ATTITUDE_QUATERNION']==limits['telemetry_max_age_s']==.2
    assert MAX_AGE['SYS_STATUS']==MAX_AGE['EKF_STATUS_REPORT']==.5
