"""Public history: real wire samples, retention, pagination and IPC boundaries."""
import json
import socket
from types import SimpleNamespace
import pytest
from test_r5_01_telemetry_receivers import wire_message
from layer6.telemetry import TelemetryReceiver
from layer6.telemetry_history import TelemetryHistory,history_response,encode_response,MAX_RESPONSE_BYTES,SCHEMA
from layer6.client import Layer6Client
from layer6.virtual_pilot import VirtualPilotClient


def receiver(times,capacity=32):
    r=TelemetryReceiver(1,1,history_capacity=capacity)
    for t in times:r.ingest(wire_message('SCALED_IMU',changes={'time_boot_ms':t}))
    return r


def continuation(page):
    return {'session_id':page['session_id'],'after_seq':page['next_after_seq'],'through_seq':page['through_seq']}


def test_arrival_order_does_not_rewrite_gaps_duplicates_or_reverse_source_time():
    r=receiver([100,120,500,500,480]);p=r.history.page()
    assert [s['source_time']['value'] for s in p['samples']]==[100,120,500,500,480]
    assert [s['history_sequence'] for s in p['samples']]==[1,2,3,4,5]
    assert [s['source_time_delta'] for s in p['samples']]==[None,20,380,0,-20]
    assert [s['source_time_relation'] for s in p['samples']]==['first','forward','forward','duplicate','reverse']
    assert all(s['wire_sequence']==0 and s['source_time_available'] for s in p['samples'])


def test_overflow_reports_exact_unfiltered_loss_and_retained_bounds():
    r=receiver(range(8),capacity=3);p=r.history.page(limit=2)
    assert p['capacity']==3 and p['retained_count']==3 and p['evicted_total']==5
    assert p['oldest_sequence']==6 and p['latest_sequence']==8
    assert p['retention_gap'] and p['lost_unfiltered_samples']==5
    assert [s['history_sequence'] for s in p['samples']]==[6,7] and p['has_more']
    second=r.history.page(**continuation(p))
    assert [s['history_sequence'] for s in second['samples']]==[8] and not second['has_more']
    assert second['lost_unfiltered_samples']==0
    assert p['samples'][0]['previous_channel_sequence']==5


def test_frozen_cutoff_excludes_new_arrivals_and_polling_does_not_duplicate_samples():
    r=receiver(range(5));p=r.history.page(limit=2);original=p['through_seq']
    r.ingest(wire_message('SCALED_IMU',changes={'time_boot_ms':99}))
    rest=r.history.page(**continuation(p));assert rest['through_seq']==original
    assert [s['history_sequence'] for s in rest['samples']]==[3,4,5]
    empty=r.history.page(**continuation(rest));assert empty['samples']==[] and not empty['has_more']
    new=r.history.page(after_seq=5,session_id=p['session_id']);assert [s['history_sequence'] for s in new['samples']]==[6]


def test_retention_overtaking_a_snapshot_is_explicit_not_silent_success():
    r=receiver(range(5),capacity=5);p=r.history.page(limit=1)
    for t in range(10,20):r.ingest(wire_message('SCALED_IMU',changes={'time_boot_ms':t}))
    lost=r.history.page(**continuation(p));assert lost['samples']==[]
    assert lost['retention_gap'] and lost['lost_unfiltered_samples']==4
    assert lost['next_after_seq']==p['through_seq'] and not lost['has_more']


def test_filtering_does_not_invent_missing_channels_or_treat_other_channels_as_packet_loss():
    r=receiver(range(3));r.ingest(wire_message('ATTITUDE_QUATERNION'))
    p=r.history.page(channels=['ATTITUDE_QUATERNION']);assert len(p['samples'])==1
    assert p['samples'][0]['history_sequence']==4 and not p['retention_gap']
    missing=r.history.page(channels=['HIGHRES_IMU']);assert missing['ok'] and missing['samples']==[]


def test_no_source_timestamp_is_explicit_and_not_borrowed_from_imu():
    r=receiver([42]);r.ingest(wire_message('SYS_STATUS'));r.ingest(wire_message('EKF_STATUS_REPORT'))
    for s in r.history.page()['samples'][1:]:
        assert s['source_time'] is None and not s['source_time_available']
        assert s['source_time_delta'] is None and s['source_time_relation']=='unavailable'


def test_history_retains_invalid_sample_without_making_it_qualified():
    r=receiver([]);r.ingest(wire_message('ATTITUDE_QUATERNION',changes={'q1':0.,'q2':0.,'q3':0.,'q4':0.}))
    s=r.history.page()['samples'][0];assert not s['valid'] and not s['guidance_state_qualified']
    assert not r.snapshot()['ATTITUDE_QUATERNION']['available']


def test_page_mutation_and_ingest_return_mutation_cannot_change_retained_wire_or_state():
    r=receiver([]);s=r.ingest(wire_message('ATTITUDE_QUATERNION'));s['fields']['q1']=99
    p=r.history.page();p['samples'][0]['fields']['q1']=88
    assert r.history.page()['samples'][0]['fields']['q1']==pytest.approx(.8)
    assert r.snapshot()['ATTITUDE_QUATERNION']['sample']['fields']['q1']==pytest.approx(.8)


def test_byte_budget_paginates_instead_of_sending_oversized_udp_datagrams():
    r=receiver([],capacity=64)
    for i in range(64):r.ingest(wire_message('ACTUATOR_OUTPUT_STATUS',changes={'time_usec':i}))
    p=r.history.page(limit=64,request_id='a'*32)
    assert p['has_more'] and p['truncated_by_bytes'] and 0<len(p['samples'])<64
    assert len(encode_response(p))<=MAX_RESPONSE_BYTES
    seen=list(p['samples'])
    while p['has_more']:
        p=r.history.page(limit=64,request_id='a'*32,**continuation(p));seen.extend(p['samples'])
        assert len(encode_response(p))<=MAX_RESPONSE_BYTES
    assert [s['history_sequence'] for s in seen]==list(range(1,65))


@pytest.mark.parametrize('capacity',[True,0,-1,4097,1.5,'32'])
def test_invalid_capacity_rejected(capacity):
    with pytest.raises(ValueError):TelemetryHistory(capacity)


@pytest.mark.parametrize('query',[{'after_seq':True},{'after_seq':-1},{'after_seq':1},
    {'limit':65},{'limit':0},{'channels':[]},{'channels':['SIMSTATE']},{'channels':['RAW_IMU','RAW_IMU']},
    {'through_seq':1},{'session_id':'bad'},{'unexpected':True}])
def test_invalid_queries_are_explicit_errors_with_no_fake_samples(query):
    p=receiver([1]).history.page(**query);assert not p['ok'] and p['samples']==[]


def test_session_change_and_future_cursor_cannot_silently_reuse_old_history():
    a=receiver([1]);b=receiver([1]);p=a.history.page()
    changed=b.history.page(**continuation(p));assert not changed['ok'] and changed['reason']=='session_changed'
    ahead=a.history.page(session_id=p['session_id'],after_seq=99);assert not ahead['ok']


def test_disconnected_rpc_and_unknown_query_are_explicit_without_readiness():
    req={'type':'telemetry_history_request','request_id':'a'*32}
    assert history_response(None,req)['reason']=='not_connected'
    assert history_response(None,{**req,'unknown':1})['reason']=='invalid_query'
    assert history_response(None,{**req,'request_id':'x'*10000})['request_id'] is None


def test_public_client_delegates_read_only_without_intake_or_lifecycle():
    calls=[];client=Layer6Client.__new__(Layer6Client)
    client._gui=SimpleNamespace(telemetry_history=lambda **q:calls.append(q) or {'ok':True})
    result=client.telemetry_history(limit=3,channels=['RAW_IMU']);assert result['ok']
    assert calls[0]['limit']==3 and calls[0]['channels']==['RAW_IMU']


class FakeSocket:
    def __init__(self,packets):self.packets=list(packets);self.timeout=7.;self.sent=[]
    def gettimeout(self):return self.timeout
    def settimeout(self,v):self.timeout=v
    def sendto(self,data,addr):self.sent.append((json.loads(data),addr))
    def recvfrom(self,size):
        if not self.packets:raise socket.timeout()
        return self.packets.pop(0)


def virtual(packets,monkeypatch):
    import layer6.virtual_pilot as module
    monkeypatch.setattr(module.uuid,'uuid4',lambda:SimpleNamespace(hex='a'*32))
    v=VirtualPilotClient.__new__(VirtualPilotClient);v.addr=('127.0.0.1',14660);v.seq=0;v.sock=FakeSocket(packets)
    return v


def test_history_rpc_rejects_wrong_peer_request_id_type_and_bad_json(monkeypatch):
    good={'type':'telemetry_history','schema':SCHEMA,'request_id':'a'*32,'ok':True,'samples':[]}
    b=lambda d:json.dumps(d).encode()
    v=virtual([(b(good),('127.0.0.1',1234)),(b({**good,'request_id':'b'*32}),('127.0.0.1',14660)),
        (b({'type':'status'}),('127.0.0.1',14660)),(b'broken',('127.0.0.1',14660)),
        (b(good),('127.0.0.1',14660))],monkeypatch)
    assert v.telemetry_history()==good and v.sock.timeout==7.
    assert len(v.sock.sent)==1 and v.sock.sent[0][0]['type']=='telemetry_history_request'


def test_timeout_is_not_an_empty_valid_history_and_restores_socket(monkeypatch):
    v=virtual([],monkeypatch);assert v.telemetry_history(timeout=.01) is None
    assert v.sock.timeout==7.


@pytest.mark.parametrize('timeout',[True,0,-1,float('inf'),float('nan')])
def test_invalid_timeout_never_sends_a_request(monkeypatch,timeout):
    v=virtual([],monkeypatch)
    with pytest.raises(ValueError):v.telemetry_history(timeout=timeout)
    assert v.sock.sent==[]
