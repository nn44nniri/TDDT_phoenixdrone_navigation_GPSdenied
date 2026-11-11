"""Actual encoded/decoded observations and public status/consumer boundaries."""
import inspect
import json
from types import SimpleNamespace
import pytest
from test_r5_01_telemetry_receivers import wire_message
from test_r5_02_public_history import virtual
from layer6.client import Layer6Client
from layer6.mavlink_link import MavlinkLink
from layer6.telemetry import TelemetryReceiver
from layer6.telemetry_source import native_source_contract
from layer6.status import project_status,status_response,encode_status,SCHEMA,MAX_BYTES,MAX_IMU_SAMPLES
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter


@pytest.fixture(scope='module')
def sender():
    s=native_source_contract();assert s['deployment_verified'];return s


def make_link(sender,messages=None):
    link=MavlinkLink({'mavlink':{'connection':'unit-only'}})
    link.target_system=link.target_component=1;link.telemetry_receiver=TelemetryReceiver(1,1,source_contract=sender)
    if messages is None:
        messages=[wire_message('HEARTBEAT',changes={'autopilot':3,'type':1,'custom_mode':18}),
            wire_message('ATTITUDE',changes={'time_boot_ms':1000,'pitch':0.}),
            wire_message('SCALED_IMU',changes={'time_boot_ms':1000,'xacc':1000,'xgyro':1000}),
            wire_message('ATTITUDE_QUATERNION',changes={'time_boot_ms':1000,'q1':.70710678,'q2':0.,'q3':.70710678,'q4':0.}),
            wire_message('VFR_HUD',changes={'airspeed':18.,'groundspeed':12.,'climb':2.,'heading':45}),
            wire_message('GLOBAL_POSITION_INT',changes={'time_boot_ms':1000,'lat':400100000,'lon':-1053000000,
                'alt':1798657,'relative_alt':141,'vx':123,'vy':-45,'vz':67}),
            wire_message('SYS_STATUS'),wire_message('EKF_STATUS_REPORT'),wire_message('SERVO_OUTPUT_RAW'),
            wire_message('EXTENDED_SYS_STATE',changes={'landed_state':1})]
    queue=list(messages)
    link.master=SimpleNamespace(recv_match=lambda **kw:queue.pop(0) if queue else None,mode_mapping=lambda:{'QHOVER':18})
    link.drain();return link


def test_public_signature_matches_tdoc_and_delegates_the_flag():
    assert 'include_imu_samples' in inspect.signature(Layer6Client.gui_status).parameters
    calls=[];client=Layer6Client.__new__(Layer6Client)
    client._gui=SimpleNamespace(status=lambda **kw:calls.append(kw) or {'schema':SCHEMA})
    assert client.gui_status(include_imu_samples=True)['schema']==SCHEMA
    assert calls==[{'timeout':.8,'include_imu_samples':True}]


def test_real_wire_fields_project_to_body_imu_ned_velocity_health_and_clock(sender):
    link=make_link(sender);p=project_status(link,include_imu_samples=True)
    assert p['attitude']['source']=='MAVLink/ATTITUDE_QUATERNION' and p['attitude']['frame']=='BODY_FRD_TO_NED'
    assert p['attitude']['pitch']==pytest.approx(1.5707963,abs=1e-5) and p['display_attitude']['pitch']==0.
    assert p['imu']['accel_x_m_s2']==9.80665 and p['imu']['gyro_x_rad_s']==1.
    assert p['imu']['source']=='MAVLink/SCALED_IMU' and p['imu']['frame']=='BODY_FRD'
    assert p['plant_time_s']==1. and p['plant_time_source']=='MAVLink/SCALED_IMU.time_boot_ms'
    assert [p['velocity_'+axis+'_m_s'] for axis in 'ned']==pytest.approx([1.23,-.45,.67])
    assert p['latitude_deg']==pytest.approx(40.01) and p['longitude_deg']==pytest.approx(-105.3)
    assert p['altitude_msl_m']==pytest.approx(1798.657) and p['relative_altitude_m']==pytest.approx(.141,abs=1e-12)
    assert p['fcu_health']['onboard_control_sensors_health']==7 and p['ekf_status']['flags']==3
    assert p['armed'] is False and p['mode']=='QHOVER' and p['landed_state']=='ON_GROUND'
    assert set(('HEARTBEAT','ATTITUDE','VFR_HUD','GLOBAL_POSITION_INT','SCALED_IMU')).issubset(p['ages'])


def test_missing_imu_or_quaternion_is_not_filled_from_euler_or_prior(sender):
    link=make_link(sender,[wire_message('ATTITUDE',changes={'pitch':.4})]);p=project_status(link,include_imu_samples=True)
    assert p['imu'] is None and p['attitude'] is None and p['imu_samples']==[]
    assert p['display_attitude']['pitch']==pytest.approx(.4)
    assert 'SCALED_IMU' not in p['ages']


def test_history_flag_is_real_and_window_is_bounded_in_arrival_order(sender):
    messages=[wire_message('SCALED_IMU',changes={'time_boot_ms':1000+20*i}) for i in range(150)]
    link=make_link(sender,messages);plain=project_status(link)
    assert plain['imu_samples']==[] and plain['imu_history']['requested'] is False
    buffered=project_status(link,include_imu_samples=True);samples=buffered['imu_samples']
    assert len(samples)==MAX_IMU_SAMPLES==64 and buffered['imu_history']['truncated_by_limit']
    assert [s['time_boot_ms'] for s in samples]==[1000+20*i for i in range(86,150)]
    assert samples[-1]['plant_time_s']==buffered['imu']['plant_time_s']
    assert all(s['wire_sha256'] and s['source_time_delta_ms']==20 for s in samples)


def test_disconnect_cannot_reuse_old_observation_fields(sender):
    link=make_link(sender);link.master=None;p=project_status(link,include_imu_samples=True)
    assert not p['connected'] and p['ages']=={} and p['imu'] is None and p['attitude'] is None
    assert p['fcu_health'] is None and p['velocity_n_m_s'] is None


def test_invalid_position_does_not_keep_a_previous_value_or_age(sender):
    good=wire_message('GLOBAL_POSITION_INT');bad=wire_message('GLOBAL_POSITION_INT');del bad.vx
    link=make_link(sender,[good,bad]);p=project_status(link)
    assert p['velocity_n_m_s'] is None and p['latitude_deg'] is None and p['ages']['GLOBAL_POSITION_INT'] is None


def test_imu_clock_reverse_is_explicit_and_not_exported_as_valid_replay(sender):
    link=make_link(sender,[wire_message('SCALED_IMU',changes={'time_boot_ms':1000}),
        wire_message('SCALED_IMU',changes={'time_boot_ms':900})]);p=project_status(link,include_imu_samples=True)
    assert p['imu'] is None and p['imu_samples']==[] and p['ages']['SCALED_IMU'] is None
    assert p['metadata']['SCALED_IMU']['freshness']['reason']=='invalid_source_clock'


def test_tdoc_fresh_and_state_consume_body_projection_without_a_signature_fallback(sender):
    p=project_status(make_link(sender));status={'mavlink_connected':p['connected'],'telemetry_age_s':p['ages'],
        'attitude':p['attitude'],'imu':p['imu'],'plant_time_s':p['plant_time_s'],
        **{k:v for k,v in p.items() if k.startswith(('latitude_','longitude_','altitude_','velocity_','airspeed_'))}}
    adapter=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter)
    adapter._option=lambda k,d=None:{'require_body_attitude_quaternion':True,'telemetry_max_age_s':.2}.get(k,d)
    assert adapter._fresh(status)
    state=adapter._state(status);assert state['imu_time_s']==1. and state['imu_frame']=='BODY_FRD'
    assert state['pitch_rad']==pytest.approx(1.5707963,abs=1e-5)
    status['attitude']['frame']='VTOL_VIEW';assert not adapter._fresh(status)


def request(**kw):return {'type':'status_request','request_id':'a'*32,'status_schema':SCHEMA,**kw}


def factory(*,include_imu_samples=False):
    return {'type':'status','schema':SCHEMA,'schema_version':1,'ok':True,
        'imu_samples':[{'time_boot_ms':i,'data':'x'*3000} for i in range(64)] if include_imu_samples else [],'imu_history':{}}


def test_status_budget_trims_oldest_only_and_records_actual_bounds():
    p=status_response(factory,request(include_imu_samples=True));assert len(encode_status(p))<=MAX_BYTES
    assert 0<len(p['imu_samples'])<64 and p['imu_history']['truncated_by_bytes']
    assert p['imu_history']['returned_count']==len(p['imu_samples'])
    assert p['imu_history']['oldest_returned_time_boot_ms']==p['imu_samples'][0]['time_boot_ms']
    assert p['imu_history']['newest_returned_time_boot_ms']==63


@pytest.mark.parametrize('override',[{'include_imu_samples':1},{'unknown':True},{'status_schema':'old'}, {'request_id':'x'*1000}])
def test_bad_status_query_does_not_create_a_successful_observation(override):
    p=status_response(factory,request(**override));assert not p['ok'] and not p['mavlink_connected']


def test_too_large_base_status_is_an_error_without_partial_mandatory_fields():
    def large(**kw):return {**factory(**kw),'huge':'x'*50000}
    p=status_response(large,request());assert not p['ok'] and p['reason']=='status_exceeds_datagram_budget'


def test_status_rpc_matches_peer_request_schema_and_restores_timeout(monkeypatch):
    good={'type':'status','schema':SCHEMA,'schema_version':1,'request_id':'a'*32,'ok':True}
    b=lambda d:json.dumps(d).encode()
    c=virtual([(b(good),('127.0.0.1',22)),(b({**good,'request_id':'b'*32}),('127.0.0.1',14660)),
        (b'bad json',('127.0.0.1',14660)),(b(good),('127.0.0.1',14660))],monkeypatch)
    assert c.status(include_imu_samples=True)==good and c.sock.timeout==7.
    assert c.sock.sent[0][0]['include_imu_samples'] is True and c.sock.sent[0][0]['type']=='status_request'


@pytest.mark.parametrize('override',[{'schema':'old'},{'schema_version':2}])
def test_wrong_response_version_is_explicit_and_cannot_claim_connection(monkeypatch,override):
    bad={'type':'status','schema':SCHEMA,'schema_version':1,'request_id':'a'*32,'mavlink_connected':True,**override}
    c=virtual([(json.dumps(bad).encode(),('127.0.0.1',14660))],monkeypatch)
    p=c.status();assert p['reason']=='status_schema_mismatch' and not p['mavlink_connected']


def test_status_timeout_is_not_a_valid_empty_state(monkeypatch):
    c=virtual([],monkeypatch);assert c.status(timeout=.01) is None and c.sock.timeout==7.


@pytest.mark.parametrize('flag',[0,1,None,'true'])
def test_non_boolean_include_flag_is_rejected_before_send(monkeypatch,flag):
    c=virtual([],monkeypatch)
    with pytest.raises(ValueError):c.status(include_imu_samples=flag)
    assert c.sock.sent==[]
