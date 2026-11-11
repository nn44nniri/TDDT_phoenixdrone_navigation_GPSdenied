"""Actual wire fixtures and OS write outcomes; no flight authorization inferred."""
from copy import deepcopy
import json
import time
from types import SimpleNamespace
import pytest
from pymavlink.dialects.v20 import ardupilotmega as dialect
from test_r5_01_telemetry_receivers import wire_message
from test_r5_02_public_history import FakeSocket
from test_r5_04_gui_status import make_link
from layer6.command_correlation import CommandCorrelation,context_from_state,identity,verified_mavlink_send,verified_send_bounds,digest
from layer6.telemetry_source import native_source_contract
from layer6.status import project_status
from layer6.client import Layer6Client,Layer6ClientError,Layer6RemoteError,Layer6TimeoutError
from layer6.virtual_pilot import VirtualPilotClient
from layer6.contracts import PilotCommand
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter


@pytest.fixture(scope='module')
def sender():
    s=native_source_contract();assert s['deployment_verified'];return s


def context(tx='tx',instance='1'*32,activation='2'*32):
    state={'roll':0.,'pitch':0.,'throttle':0.,'yaw':0.,'transaction_id':tx,
        'command_instance_id':instance,'_gui_activation_id':activation,'command_mode':None,'command_discrete':[]}
    state['command_id']=identity(tx,{k:state[k] for k in ('roll','pitch','throttle','yaw')})
    return context_from_state(state)


class Port:
    def __init__(self,outcome='full'):self.outcome=outcome;self.wires=[]
    def send(self,data):
        self.wires.append(bytes(data))
        if self.outcome=='error':raise OSError('socket failed')
        return len(data) if self.outcome=='full' else len(data)-1 if self.outcome=='partial' else None


class Master:
    def __init__(self,outcome='full'):
        self.port=Port(outcome);self.queue=[];self.mav=dialect.MAVLink(self,srcSystem=255,srcComponent=191)
    def write(self,data):
        try:self.port.send(data)
        except OSError:pass  # Same swallowed-error behavior as installed mavtcp.write.
    def recv_match(self,**kw):return self.queue.pop(0) if self.queue else None
    def mode_mapping(self):return {'QHOVER':18}


def driver(sender,*,ordered=True):
    messages=[wire_message('HEARTBEAT',changes={'autopilot':3,'type':1,'custom_mode':18}),
        wire_message('SCALED_IMU',changes={'time_boot_ms':1000}),wire_message('ATTITUDE_QUATERNION',changes={'time_boot_ms':1000}),
        wire_message('ATTITUDE',changes={'time_boot_ms':1000}),wire_message('SERVO_OUTPUT_RAW',changes={'time_usec':1000000}),
        wire_message('GLOBAL_POSITION_INT',changes={'time_boot_ms':1000}),wire_message('VFR_HUD')]
    link=make_link(sender,messages);link.master=Master()
    link.command_correlation=CommandCorrelation(link.telemetry_receiver.history.session_id,link.telemetry_receiver.contracts.domain,
        ordered_stream=ordered,source_verified=True)
    return link


def status(link,ctx):
    p=project_status(link,active_command_context=ctx)
    return {**p,'schema':'Layer6-GUI-status-v1','schema_version':1,'ok':True,'mavlink_connected':p['connected'],'telemetry_age_s':p['ages'],
        'telemetry_metadata':p['metadata'],'monotonic_domain_id':p['domain'],'target_system':1,'target_component':1,
        'telemetry_received_monotonic':{k:v['received_monotonic'] for k,v in p['metadata'].items()},
        'command_first_sent_plant_time_s':None}


def complete(link,ctx):
    e=link.command_correlation.records[link.command_correlation.current]
    link.master.queue=[wire_message('TIMESYNC',changes={'tc1':800000000,'ts1':e['barrier_nonce']}),
        wire_message('SCALED_IMU',changes={'time_boot_ms':1100})]
    link.drain()
    link.master.queue=[wire_message('SCALED_IMU',changes={'time_boot_ms':1200}),
        wire_message('ATTITUDE_QUATERNION',changes={'time_boot_ms':1200}),wire_message('ATTITUDE',changes={'time_boot_ms':1200}),
        wire_message('SERVO_OUTPUT_RAW',changes={'time_usec':1200000}),wire_message('GLOBAL_POSITION_INT',changes={'time_boot_ms':1200})]
    link.drain();return status(link,ctx)


@pytest.mark.parametrize('outcome',['partial','error','unknown'])
def test_swallowed_or_partial_socket_write_cannot_become_a_success(outcome):
    master=Master(outcome);original=master.port
    with pytest.raises(OSError):verified_mavlink_send(master,master.mav.manual_control_encode(1,0,0,0,0,0))
    assert master.port is original


def test_success_observes_the_actual_encoded_packet_and_restores_socket():
    master=Master();original=master.port;wire=verified_mavlink_send(master,master.mav.manual_control_encode(1,11,-22,33,-44,0))
    msg=dialect.MAVLink(None).parse_char(wire)
    assert (msg.x,msg.y,msg.z,msg.r)==(11,-22,33,-44) and original.wires==[wire]
    assert master.port is original


def test_first_send_is_real_and_stable_across_same_command_refreshes(sender):
    link=driver(sender);ctx=context()
    assert status(link,ctx)['applied_transaction_id'] is None
    link.send_manual(0,0,0,0,command_context=ctx);first=status(link,ctx)
    assert first['applied_transaction_id']=='tx' and first['command_first_sent_monotonic']>0
    assert not first['command_correlation']['available'] and first['command_first_sent_plant_time_s'] is None
    link.send_manual(0,0,0,0,command_context=ctx);again=status(link,ctx)
    assert again['command_first_sent_monotonic']==first['command_first_sent_monotonic']
    assert again['manual_sent_monotonic']>=first['manual_sent_monotonic']
    assert again['command_correlation']['record']['send_count']==2


def test_matching_barrier_and_source_witnesses_produce_an_interval_not_a_point(sender):
    link=driver(sender);ctx=context();link.send_manual(0,0,0,0,command_context=ctx);s=complete(link,ctx)
    assert s['command_correlation']['available'] and verified_send_bounds(s,'tx')==pytest.approx((1.,1.101))
    assert s['command_first_sent_plant_time_s'] is None
    e=s['command_correlation']['record'];assert not e['barrier_reply']['tc1_used_as_clock_point']
    assert e['FCU_manual_acknowledged'] is False and e['mechanical_execution_verified'] is False


def test_matching_telemetry_without_an_echo_barrier_cannot_qualify_timing(sender):
    link=driver(sender);ctx=context();link.send_manual(0,0,0,0,command_context=ctx)
    e=link.command_correlation.records[link.command_correlation.current]
    link.master.queue=[wire_message('TIMESYNC',changes={'tc1':1,'ts1':e['barrier_nonce']+1}),
        wire_message('SCALED_IMU',changes={'time_boot_ms':1200})];link.drain()
    assert verified_send_bounds(status(link,ctx),'tx') is None


def test_unordered_transport_keeps_timing_unqualified_even_with_an_echo(sender):
    link=driver(sender,ordered=False);ctx=context();link.send_manual(0,0,0,0,command_context=ctx)
    s=complete(link,ctx);assert not s['command_correlation']['available'] and verified_send_bounds(s,'tx') is None
    assert s['applied_transaction_id']=='tx'  # Write identity is known; temporal proof is not.


@pytest.mark.parametrize('new_context',[context('new'),context(instance='3'*32),context(activation='4'*32),None])
def test_new_or_released_context_cannot_relabel_previous_send(sender,new_context):
    link=driver(sender);old=context();link.send_manual(0,0,0,0,command_context=old);complete(link,old)
    assert status(link,new_context)['applied_transaction_id'] is None


def test_same_tx_reaccepted_as_a_new_instance_gets_a_new_first_send(sender):
    link=driver(sender);old=context();link.send_manual(0,0,0,0,command_context=old)
    a=status(link,old);new=context(instance='3'*32);link.send_manual(0,0,0,0,command_context=new)
    b=status(link,new);assert b['command_first_sent_monotonic']>a['command_first_sent_monotonic']
    assert b['command_correlation']['record']['generation_id']!=a['command_correlation']['record']['generation_id']


def test_post_source_before_upper_witness_is_not_promoted_to_post_command(sender):
    link=driver(sender);ctx=context();link.send_manual(0,0,0,0,command_context=ctx)
    e=link.command_correlation.records[link.command_correlation.current]
    link.master.queue=[wire_message('TIMESYNC',changes={'tc1':5,'ts1':e['barrier_nonce']}),wire_message('SCALED_IMU',changes={'time_boot_ms':1100}),
        wire_message('ATTITUDE_QUATERNION',changes={'time_boot_ms':1050}),wire_message('ATTITUDE',changes={'time_boot_ms':1050})]
    link.drain();s=status(link,ctx);assert not s['command_correlation']['available']
    assert 'ATTITUDE' not in s['command_correlation']['record']['post_samples']


def test_partial_write_does_not_assign_transaction_identity(sender):
    link=driver(sender);link.master.port=Port('partial');ctx=context()
    with pytest.raises(OSError):link.send_manual(0,0,0,0,command_context=ctx)
    assert status(link,ctx)['applied_transaction_id'] is None


@pytest.mark.parametrize('field',['session_id','monotonic_domain_id','applied_transaction_id'])
def test_mismatched_status_identity_rejects_an_otherwise_valid_trace(sender,field):
    link=driver(sender);ctx=context();link.send_manual(0,0,0,0,command_context=ctx);s=complete(link,ctx)
    s[field]='wrong';assert verified_send_bounds(s,'tx') is None


def test_corrupted_time_or_packet_cannot_become_valid_by_rehashing_the_record(sender):
    link=driver(sender);ctx=context();link.send_manual(0,0,0,0,command_context=ctx);s=complete(link,ctx)
    original=deepcopy(s)
    s['command_correlation']['record']['plant_time_bounds']['upper_s']=1.01
    s['command_correlation']['record_hash']=digest(s['command_correlation']['record'])
    assert verified_send_bounds(s,'tx') is None
    original['command_correlation']['record']['wire_hex']='00'
    original['command_correlation']['record_hash']=digest(original['command_correlation']['record'])
    assert verified_send_bounds(original,'tx') is None


def test_tdoc_confirmation_requires_valid_correlated_bounds_for_new_status(sender):
    link=driver(sender);ctx=context();link.send_manual(0,0,0,0,command_context=ctx);s=complete(link,ctx)
    adapter=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter)
    adapter._option=lambda k,d=None:{'require_body_attitude_quaternion':True,'telemetry_max_age_s':.2}.get(k,d)
    assert adapter._confirmed(s,{}, {'u_cmd':[0,0,0,0]},'tx')
    s['command_correlation']['available']=False
    assert not adapter._confirmed(s,{}, {'u_cmd':[0,0,0,0]},'tx')


def test_ack_matches_transaction_and_peer_instead_of_first_datagram(monkeypatch):
    import layer6.client as module
    monkeypatch.setattr(module.uuid,'uuid4',lambda:SimpleNamespace(hex='a'*32))
    tx='my-tx'
    good=json.dumps({'ok':True,'transaction_id':tx,'intake_request_id':'a'*32}).encode()
    sock=FakeSocket([(good,('127.0.0.1',99)),(json.dumps({'ok':True,'transaction_id':'old'}).encode(),('127.0.0.1',31337)),(json.dumps({'ok':True,'transaction_id':tx,'intake_request_id':'b'*32}).encode(),('127.0.0.1',31337)),(good,('127.0.0.1',31337))])
    c=Layer6Client.__new__(Layer6Client);c._sock=sock;c.endpoint=('127.0.0.1',31337);c.timeout=.1
    assert c.submit({'transaction_id':tx,'u_cmd':[0,0,0,0]}).transaction_id==tx and sock.timeout==7.


def test_intake_timeout_and_negative_matching_ack_are_distinct(monkeypatch):
    import layer6.client as module
    monkeypatch.setattr(module.uuid,'uuid4',lambda:SimpleNamespace(hex='a'*32))
    c=Layer6Client.__new__(Layer6Client);c.endpoint=('127.0.0.1',31337);c.timeout=.01;c._sock=FakeSocket([])
    with pytest.raises(Layer6TimeoutError):c.submit({'transaction_id':'tx','u_cmd':[0,0,0,0]})
    c._sock=FakeSocket([(json.dumps({'ok':False,'transaction_id':'tx','intake_request_id':'a'*32,'error':'rejected'}).encode(),c.endpoint)])
    with pytest.raises(Layer6RemoteError,match='rejected'):c.submit({'transaction_id':'tx','u_cmd':[0,0,0,0]})


def test_conflicting_ids_are_rejected_before_transmission():
    c=Layer6Client.__new__(Layer6Client);c._sock=FakeSocket([])
    with pytest.raises(Layer6ClientError,match='mismatch'):c.submit({'transaction_id':'a','layer6_package':{'transaction_id':'b'}})
    assert c._sock.sent==[]


def test_virtual_state_transports_stable_identity_and_distinct_admissions():
    c=VirtualPilotClient.__new__(VirtualPilotClient);c.addr=('127.0.0.1',123);c.seq=0;c.sock=FakeSocket([])
    cmd=PilotCommand(0.,0.,0.,0.)
    c.state(cmd,transaction_id='tx',command_instance_id='1'*32);c.state(cmd,transaction_id='tx',command_instance_id='1'*32)
    a,b=[m for m,_ in c.sock.sent];assert a['command_id']==b['command_id'] and a['command_instance_id']==b['command_instance_id']
    a['_gui_activation_id']='2'*32;assert context_from_state(a) is not None
    a['roll']=.1;assert context_from_state(a) is None


def test_correlation_storage_is_bounded_and_does_not_reset_first_send_on_reads():
    tracker=CommandCorrelation('session','domain',ordered_stream=True,source_verified=True)
    master=Master();wire=verified_mavlink_send(master,master.mav.manual_control_encode(1,0,0,0,0,0))
    for i in range(40):tracker.record_send(context(str(i)),{'aileron':0.,'elevator':0.,'throttle':0.,'rudder':0.},wire,100.,100.01,write_verified=True)
    assert len(tracker.records)==16
    a=tracker.snapshot(context('39'));b=tracker.snapshot(context('39'))
    assert a==b and a['record']['first_sent_monotonic']==100.01


def test_expired_GUI_state_cannot_reuse_an_active_correlation_context():
    ctx=context();state={**ctx['requested_gui_state'],'transaction_id':'tx',
        'command_id':ctx['command_id'],'command_instance_id':ctx['instance_id'],
        '_gui_activation_id':ctx['activation_id'],'command_mode':None,'command_discrete':[]}
    assert context_from_state(state,deadline=10.,now=9.)==ctx
    assert context_from_state(state,deadline=10.,now=10.01) is None


@pytest.mark.parametrize('channel',['SCALED_IMU','ATTITUDE_QUATERNION','ATTITUDE','SERVO_OUTPUT_RAW'])
def test_stale_post_witness_is_not_a_valid_current_confirmation(sender,channel):
    link=driver(sender);ctx=context();link.send_manual(0,0,0,0,command_context=ctx);s=complete(link,ctx)
    s['telemetry_metadata'][channel]['freshness']['fresh']=False
    assert verified_send_bounds(s,'tx') is None
