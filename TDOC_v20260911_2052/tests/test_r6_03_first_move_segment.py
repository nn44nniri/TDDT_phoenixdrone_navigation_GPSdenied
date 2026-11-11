"""Plant source and real MAVLink wire segment bounds, plus negative closure proofs."""
from copy import deepcopy
import time
from types import SimpleNamespace
import pytest
from test_r5_05_command_correlation import driver,context,Master
from test_r5_01_telemetry_receivers import wire_message
from layer6.telemetry_source import native_source_contract
from layer6.command_correlation import digest,identity
from layer6.cancellation import GuiControlGate
from layer6.segments import contract,SCHEMA,SegmentExecution,sync_gui_segment,validate_result
from tddt_tdoc.layer6_bridge import Layer6Bridge,Layer6BridgeError
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter

@pytest.fixture(scope='module')
def sender():return native_source_contract()

def start(sender,deadline=60,ordered=True,origin=1000):
    link=driver(sender,ordered=ordered)
    if origin!=1000:feed(link,origin,all_channels=True)
    ctx=context();policy=contract({'execution_segment_s':.1})
    ctx.update(execution_segment=policy,control_session_id='a'*32,admission_deadline_unix=time.time()+deadline)
    ctx['command_id']=identity('tx',ctx['requested_gui_state'],None,[],policy)
    msg={**ctx['requested_gui_state'],'type':'state','control_session_id':'a'*32,'transaction_id':'tx','command_instance_id':ctx['instance_id'],'seq':1}
    gate=GuiControlGate();assert gate.admit(msg,('127.0.0.1',1))
    sock=SimpleNamespace(sendto=lambda *a:None)
    gui=SimpleNamespace(link=link,_control_gate=gate,virtual_state=msg,virtual_deadline=time.monotonic()+20,virtual_sock=sock,lbl_digital=SimpleNamespace(setText=lambda _:None))
    sent=link.send_manual(0,0,0,0,command_context=ctx)
    return link,ctx,gui,sent

def feed(link,stamp,barrier=None,all_channels=False):
    rows=[]
    if barrier is not None:rows.append(wire_message('TIMESYNC',changes={'tc1':100,'ts1':barrier}))
    rows.append(wire_message('SCALED_IMU',changes={'time_boot_ms':stamp}))
    if all_channels:
        rows.extend([wire_message(n,changes={'time_boot_ms':stamp}) for n in ('ATTITUDE','ATTITUDE_QUATERNION','GLOBAL_POSITION_INT')])
        rows.append(wire_message('SERVO_OUTPUT_RAW',changes={'time_usec':stamp*1000}))
    link.master.queue=rows;link.drain()

def close(sender,deadline=60,overrun=False):
    link,ctx,gui,sent=start(sender,deadline)
    assert sent is not False
    e=link.command_correlation.records[link.command_correlation.current]
    feed(link,1020,e['barrier_nonce'],True)
    feed(link,1060);sync_gui_segment(gui)
    r=link.segments.current_record();assert gui.virtual_state is None and gui._cancel_output_inhibited
    feed(link,1120 if overrun else 1080,r['close']['nonce'],True)
    feed(link,1140 if overrun else 1090,all_channels=True)
    return link,ctx,gui,link.segments.current_record()

@pytest.mark.parametrize('deadline',[1,60,3600])
def test_deadline_does_not_extend_bounded_segment(sender,deadline):
    link,ctx,gui,r=close(sender,deadline)
    assert r['state']=='CLOSED',r
    assert r['duration_bounds_s']==pytest.approx({'lower_s':.039,'upper_s':.081})
    assert validate_result(r,'tx',ctx['instance_id'],ctx['execution_segment'])
    count=len(link.master.port.wires)
    for _ in range(4):assert link.send_manual(0,0,0,0,command_context=ctx) is False
    assert link.send_manual(0,0,0,0,command_context=None) is False
    assert len(link.master.port.wires)==count
    old={'type':'state','control_session_id':'a'*32,'transaction_id':'tx','command_instance_id':ctx['instance_id'],'seq':100}
    assert not gui._control_gate.admit(old,('127.0.0.1',1))

def test_delayed_barrier_over_cap_never_success(sender):
    link,ctx,gui,r=close(sender,overrun=True)
    assert r['state']=='FAILED' and not r['qualified']
    assert 'overrun' in r['failure']


def test_canonical_closure_snapshot_has_one_copy_of_each_telemetry_contract(sender):
    import json
    link,ctx,gui,r=close(sender)
    snapshot=r['snapshot']
    assert validate_result(json.loads(json.dumps(r)),'tx',ctx['instance_id'],ctx['execution_segment'])
    assert all(k not in snapshot for k in ('metadata','ages','domain','now','connected','channels'))
    assert snapshot['telemetry_metadata'] and snapshot['telemetry_age_s'] and snapshot['monotonic_domain_id']
    assert snapshot['telemetry_channels']

@pytest.mark.parametrize('duration,guard',[(.1,0),(.1,.1),(.1,.2),(True,.01),(float('nan'),.01),(.1,float('inf'))])
def test_invalid_policy(duration,guard):
    with pytest.raises(ValueError):contract({'execution_segment':{'schema':SCHEMA,'max_duration_s':duration,'close_guard_s':guard}})

def test_conflicting_aliases_reject():
    with pytest.raises(ValueError):contract({'execution_segment_s':.1,'layer6_package':{'execution_segment_s':2}})

@pytest.mark.parametrize('ordered,deadline',[(False,60),(True,-1)])
def test_unordered_or_expired_never_writes(sender,ordered,deadline):
    link,ctx,gui,sent=start(sender,deadline,ordered)
    assert sent is False and not link.master.port.wires
    sync_gui_segment(gui);assert link.segments.current_record()['state']=='FAILED'

def test_duplicate_source_never_refreshes_write(sender):
    link,ctx,gui,sent=start(sender)
    count=len(link.master.port.wires)
    assert link.send_manual(0,0,0,0,command_context=ctx) is False
    assert len(link.master.port.wires)==count

@pytest.mark.parametrize('fault',['hash','nonce','instance','fence','scope','event','clock','duration','source'])
def test_false_proof_even_rehashed_rejects(sender,fault):
    link,ctx,gui,r=close(sender);r=deepcopy(r)
    if fault=='hash':r['result_hash']='wrong'
    elif fault=='nonce':r['close']['nonce']+=1
    elif fault=='instance':r['context']['instance_id']='b'*32
    elif fault=='fence':r['GUI_fence']['target_absent']=False
    elif fault=='scope':r['physical_stabilization_verified']=True
    elif fault=='event':r['first_event']['send_count']+=1
    elif fault=='clock':r['clock_rate_assumed']=True
    elif fault=='duration':r['duration_bounds_s']['upper_s']=.05
    elif fault=='source':r['close']['pre_source']['source_system']=2
    if fault!='hash':r['result_hash']=digest({k:v for k,v in r.items() if k!='result_hash'})
    assert not validate_result(r,'tx',ctx['instance_id'],ctx['execution_segment'])

def test_bridge_uses_fast_proof_and_actual_cancel_then_l7(sender):
    link,ctx,gui,r=close(sender);calls=[]
    ack=SimpleNamespace(ok=True,transaction_id='tx',raw={'command_instance_id':ctx['instance_id'],'execution_segment':ctx['execution_segment']})
    client=SimpleNamespace(submit=lambda *a,**kw:ack,
        segment_status=lambda **kw:{'available':True,'segment':r},
        cancel=lambda **kw:calls.append('cancel') or SimpleNamespace(ok=True,raw={'cancelled':True,'command_instance_id':ctx['instance_id']}),
        build_layer7_input=lambda *a,**kw:calls.append('L7') or {'ExecutionEvidence':{}},
        gui_status=lambda **kw:pytest.fail('heavy status called during segment'))
    adapter=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter);adapter._option=lambda k,d=None:d
    before={'schema':'Layer6-GUI-status-v1','session_id':r['session_id']}
    result=Layer6Bridge(client,fresh=lambda _:True,confirmed=adapter._confirmed).exchange(
        {'transaction_id':'tx','layer6_package':{'transaction_id':'tx','u_cmd':[0,0,0,0],'tau_deadline':time.time()+60}},before=before,timeout_s=1,segment_s=.1)
    ev=result['layer7_input']['ExecutionEvidence']
    assert ev['native_execution_segment_s'] is None and ev['native_execution_duration_bounds_s']['upper_s']<=.1
    assert calls==['cancel','L7'] and ev['remaining_horizon_executed'] is False

@pytest.mark.parametrize('fault',['missing','reverse','policy_change','context_lost'])
def test_lost_or_changed_clock_context_never_extends_segment(sender,fault):
    link,ctx,gui,_=start(sender);count=len(link.master.port.wires)
    if fault=='missing':link.telemetry_receiver.latest.pop('SCALED_IMU')
    elif fault=='reverse':feed(link,900)
    elif fault=='policy_change':ctx=deepcopy(ctx);ctx['execution_segment']['max_duration_s']=5
    if fault=='context_lost':ctx=None
    assert link.send_manual(0,0,0,0,command_context=ctx) is False
    sync_gui_segment(gui)
    assert gui.virtual_state is None and gui._cancel_output_inhibited
    assert len(link.master.port.wires)<=count+1 # closure barrier only, never another MANUAL
    assert not link.segments.current_record()['qualified']

def test_old_closure_cannot_clear_newer_GUI_generation(sender):
    link,ctx,gui,_=start(sender);feed(link,1060)
    new={'type':'state','control_session_id':'a'*32,'transaction_id':'new','command_instance_id':'b'*32,'seq':2}
    assert gui._control_gate.admit(new,('127.0.0.1',1));gui.virtual_state=new
    sync_gui_segment(gui)
    assert gui.virtual_state is new and not getattr(gui,'_cancel_output_inhibited',False)
    assert link.segments.current_record()['GUI_fence']['matched_active'] is False

def test_public_fast_API_matches_request_identity_and_peer(monkeypatch):
    import json
    from test_r5_02_public_history import FakeSocket
    from layer6.virtual_pilot import VirtualPilotClient
    from layer6.segments import STATUS_SCHEMA
    import layer6.virtual_pilot as module
    monkeypatch.setattr(module.uuid,'uuid4',lambda:SimpleNamespace(hex='c'*32))
    good={'schema':STATUS_SCHEMA,'request_id':'c'*32,'transaction_id':'tx','command_instance_id':'1'*32,'available':False}
    peer=('127.0.0.1',123)
    sock=FakeSocket([(json.dumps(good).encode(),('127.0.0.1',124)),(json.dumps({**good,'request_id':'d'*32}).encode(),peer),(json.dumps(good).encode(),peer)])
    c=VirtualPilotClient.__new__(VirtualPilotClient);c.addr=peer;c.sock=sock;c.seq=0
    assert c.segment_status(transaction_id='tx',command_instance_id='1'*32,timeout=.1)==good
    assert sock.timeout==7.

@pytest.mark.parametrize('failure',['unconfirmed','cancel','stale'])
def test_bridge_failures_never_return_success(sender,failure):
    link,ctx,gui,r=close(sender);calls=[]
    ack=SimpleNamespace(ok=True,transaction_id='tx',raw={'command_instance_id':ctx['instance_id'],'execution_segment':ctx['execution_segment']})
    client=SimpleNamespace(submit=lambda *a,**kw:ack,segment_status=lambda **kw:{'available':True,'segment':r},
        cancel=lambda **kw:SimpleNamespace(ok=failure!='cancel',raw={'cancelled':failure!='cancel','command_instance_id':ctx['instance_id']}),
        build_layer7_input=lambda *a,**kw:pytest.fail('invalid execution reached L7'))
    if failure=='stale':time.sleep(.3)  # Original metadata expires at actual consumption; never redate it.
    a=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter);a._option=lambda k,d=None:d
    with pytest.raises(Layer6BridgeError):Layer6Bridge(client,fresh=lambda _:True,confirmed=(lambda *a:False) if failure=='unconfirmed' else a._confirmed).exchange(
        {'transaction_id':'tx','layer6_package':{'transaction_id':'tx','u_cmd':[0,0,0,0],'tau_deadline':time.time()+60}},before={'schema':'Layer6-GUI-status-v1','session_id':r['session_id']},timeout_s=1,segment_s=.1)

@pytest.mark.parametrize('origin',[9646,10086,10529,11289,11999,15006])
def test_float_threshold_never_waits_an_extra_source_tick(sender,origin):
    link,ctx,gui,_=start(sender,origin=origin)
    feed(link,origin+60)
    assert link.segments.current_record()['state']=='STOP_REQUESTED'
    assert link.segments.current_record()['stop_reason']=='plant_close_guard_reached'
