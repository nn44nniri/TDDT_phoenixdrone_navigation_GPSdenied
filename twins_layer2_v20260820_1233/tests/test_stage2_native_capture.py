"""Synthetic schema tests only; no SITE/FCU validity assertions."""
import base64
import copy
import hashlib
import importlib.util
import json
import socket
from pathlib import Path
import threading
import time
import pytest
from twins_fmi.native_actuator_binding import NativeReplayRejected
from twins_fmi.stage2_native_event_assembler import assemble,sha256
from test_stage2_native_actuator_binding import record

ROOT=Path(__file__).resolve().parents[1]
sp=importlib.util.spec_from_file_location('capture',ROOT/'scripts'/'capture_stage2_mavlink_udp.py')
capture_mod=importlib.util.module_from_spec(sp);sp.loader.exec_module(capture_mod)


def native_events(tmp_path,n=3):
    p=tmp_path/'TEST_FIXTURE_NOT_NATIVE.log';p.write_text('SIMULATED TEST SOURCE ONLY')
    dg=sha256(p)
    ssignals={k:{'native_signal':'native/'+k,'unit':u} for k,u in {
        'left_motor_cmd':'1','right_motor_cmd':'1','left_elevon_rad':'rad',
        'right_elevon_rad':'rad','omega_left':'rad/s','omega_right':'rad/s'}.items()}
    osignals={k:{'native_signal':'native/'+k,'unit':u} for k,u in {
        'vn':'m/s','ve':'m/s','vd':'m/s','omega_left':'rad/s','omega_right':'rad/s'}.items()}
    sev={'source_file':str(p),'source_sha256':dg,'producer':'MERGED_NATIVE_FCU_JSBSIM_LOG',
         'signal_map':ssignals,'time_domain':'JSBSIM_PLANT_SECONDS'}
    oev={'source_file':str(p),'source_sha256':dg,'producer':'NATIVE_JSBSIM_PROPERTY_LOG',
         'signal_map':osignals,'time_domain':'JSBSIM_PLANT_SECONDS','velocity_frame':'NED'}
    events=[]
    for i in range(n):
        a=record();a.update(transaction_id='fixture'+str(i),snapshot_id='fixture-s'+str(i),
                            plant_time_s=2+i*.2,actuator_timestamp_s=1.99+i*.2,
                            motor_state_timestamp_s=1.99+i*.2,
                            actuator_source='jsbsim_actuator_property')
        obs=dict(transaction_id=a['transaction_id'],snapshot_id=a['snapshot_id'],
                 controller_identity=a['controller_identity'],model_identity=a['model_identity'],
                 plant_time_s=a['plant_time_s']+.01,motor_state_source='jsbsim_property',
                 vn=0,ve=0,vd=0,omega_left=100,omega_right=100)
        events.extend((dict(kind='sample',sample=a,evidence=copy.deepcopy(sev)),
                       dict(kind='observation',observation=obs,evidence=copy.deepcopy(oev))))
    return events


def test_asymmetric_native_events_pair_without_inventing(tmp_path):
    events=native_events(tmp_path)
    paired=assemble(events)
    assert len(paired)==3
    assert paired[0]['source_evidence']['pair_provenance']=='INDEPENDENT_NATIVE_EXPORT_NOT_CONTROLLER_CERTIFICATION'
    assert paired[0]['observation']['omega_left']==100


def test_absent_motor_native_signal_rejected(tmp_path):
    events=native_events(tmp_path)
    del events[1]['evidence']['signal_map']['omega_left']
    with pytest.raises(NativeReplayRejected,match='omega_left'):
        assemble(events)


def test_invalid_native_hash_rejected(tmp_path):
    events=native_events(tmp_path)
    events[0]['evidence']['source_sha256']='0'*64
    with pytest.raises(NativeReplayRejected,match='sha256'):
        assemble(events)


def test_pilot_mapping_not_native(tmp_path):
    events=native_events(tmp_path)
    events[0]['evidence']['signal_map']['omega_left']['native_signal']='pwm_1200'
    with pytest.raises(NativeReplayRejected,match='proxy'):
        assemble(events)


def test_unmatched_sample_rejected(tmp_path):
    events=native_events(tmp_path)
    with pytest.raises(NativeReplayRejected,match='unmatched'):
        assemble(events[:-1])


def test_model_generated_source_rejected(tmp_path):
    events=native_events(tmp_path)
    events[0]['evidence']['producer']='WINGMPET_PREDICTION'
    with pytest.raises(NativeReplayRejected,match='native'):
        assemble(events)


def test_duplicate_transaction_rejected(tmp_path):
    events=native_events(tmp_path)
    events.append(copy.deepcopy(events[0]))
    with pytest.raises(NativeReplayRejected,match='duplicate'):
        assemble(events)


def test_cross_time_rejected(tmp_path):
    events=native_events(tmp_path)
    events[1]['observation']['plant_time_s']+=.04
    with pytest.raises(NativeReplayRejected,match='time'):
        assemble(events)


def test_mavlink_raw_packet_capture_without_command(tmp_path):
    receiver=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    receiver.bind(('127.0.0.1',0))
    port=receiver.getsockname()[1]
    sender=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    def send():
        time.sleep(.03)
        sender.sendto(b'NOT_A_REAL_MAVLINK_TEST_PACKET',('127.0.0.1',port))
    t=threading.Thread(target=send)
    try:
        t.start()
        path=tmp_path/'raw.jsonl'
        n=capture_mod.capture(('127.0.0.1',port),.3,path,sock=receiver)
        t.join()
        assert n==1
        data=json.loads(path.read_text().strip())
        assert base64.b64decode(data['payload_base64'])==b'NOT_A_REAL_MAVLINK_TEST_PACKET'
        assert data['kind']=='raw_mavlink_udp'
    finally:
        sender.close();receiver.close()


def test_raw_udp_not_native_pairs(tmp_path):
    with pytest.raises(NativeReplayRejected):
        assemble([{'kind':'raw_mavlink_udp','payload_base64':'AAA='}])
