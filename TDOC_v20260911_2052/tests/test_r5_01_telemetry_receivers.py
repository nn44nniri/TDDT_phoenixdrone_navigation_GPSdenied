"""Real encoded/decoded MAVLink fixtures; independent native evidence is separate."""
import ast
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from pymavlink.dialects.v10 import ardupilotmega as v1
from pymavlink.dialects.v20 import ardupilotmega as v2

from layer6.telemetry import TelemetryReceiver,REQUEST_HZ,SPECS
from layer6.mavlink_link import MavlinkLink

ROOT=Path(__file__).resolve().parents[2];NATIVE=ROOT/'layer6_v20261007_1032'


def wire_message(name,*,version=2,system=1,component=1,changes=None):
    module=v2 if version==2 else v1;cls=getattr(module,'MAVLink_'+name.lower()+'_message')
    values={}
    for field in inspect.signature(cls).parameters:
        values[field]=0
    values.update({k:100+i for i,k in enumerate(('xacc','yacc','zacc','xgyro','ygyro','zgyro','xmag','ymag','zmag'))})
    values.update(time_boot_ms=1234,time_usec=5678000,q1=.8,q2=.6,q3=0.,q4=0.,repr_offset_q=[1.,0.,0.,0.],
        rollspeed=.1,pitchspeed=.2,yawspeed=.3,onboard_control_sensors_present=31,
        onboard_control_sensors_enabled=15,onboard_control_sensors_health=7,voltage_battery=12000,current_battery=100,
        battery_remaining=80,actuator=[.25]*32,flags=3)
    values.update({'servo%d_raw'%i:1500+i for i in range(1,17)})
    if changes:values.update(changes)
    values={k:v for k,v in values.items() if k in inspect.signature(cls).parameters}
    data=cls(**values).pack(module.MAVLink(None,srcSystem=system,srcComponent=component))
    return v2.MAVLink(None).parse_char(data)


@pytest.mark.parametrize('name',list(REQUEST_HZ))
@pytest.mark.parametrize('version',[1,2])
def test_required_channels_come_from_actual_wire_and_keep_raw_units(name,version):
    r=TelemetryReceiver(1,1);msg=wire_message(name,version=version);sample=r.ingest(msg)
    assert sample['valid'] and sample['wire_version']==version
    assert bytes.fromhex(sample['wire_hex'])==bytes(msg.get_msgbuf())
    assert sample['source_system']==1 and sample['source_component']==1
    assert not sample['guidance_state_qualified'] and not sample['navigation_truth_injection']
    if name=='SCALED_IMU':
        assert sample['fields']['xacc']==100 and sample['units']['xacc']=='mG'
        assert sample['units']['xgyro']=='mrad/s' and sample['source_time']['value']==1234
    elif name=='ATTITUDE_QUATERNION':assert sample['fields']['q1']==pytest.approx(.8)
    elif name=='SYS_STATUS':assert sample['fields']['onboard_control_sensors_health']==7
    elif name=='SERVO_OUTPUT_RAW':assert sample['fields']['servo1_raw']==1501


def test_absent_channels_are_unavailable_and_truth_is_not_a_substitute():
    r=TelemetryReceiver(1,1)
    assert r.ingest(wire_message('SIMSTATE')) is None
    assert r.ingest(wire_message('ATTITUDE')) is None
    assert all(not row['available'] and not row['received'] and row['sample'] is None for row in r.snapshot().values())


@pytest.mark.parametrize('system,component',[(2,1),(1,2)])
def test_foreign_vehicle_or_component_does_not_fill_channels(system,component):
    r=TelemetryReceiver(1,1)
    assert r.ingest(wire_message('SCALED_IMU',system=system,component=component)) is None
    assert not r.snapshot()['SCALED_IMU']['received'] and r.foreign_packets==1


def test_mavlink1_extensions_are_not_measured_zero_outputs():
    r=TelemetryReceiver(1,1);sample=r.ingest(wire_message('SERVO_OUTPUT_RAW',version=1))
    assert sample['valid'] and all(sample['fields']['servo%d_raw'%i] is None for i in range(9,17))


def test_unobserved_mavlink2_extensions_are_explicitly_unavailable():
    r=TelemetryReceiver(1,1);sample=r.ingest(wire_message('SERVO_OUTPUT_RAW',changes={'servo%d_raw'%i:0 for i in range(9,17)}))
    assert sample['valid'] and sample['fields']['servo9_raw'] is None


@pytest.mark.parametrize('change',[{'q1':0.,'q2':0.,'q3':0.,'q4':0.},{'q1':float('nan')}])
def test_invalid_quaternion_does_not_become_identity_or_euler_fallback(change):
    r=TelemetryReceiver(1,1);sample=r.ingest(wire_message('ATTITUDE_QUATERNION',changes=change))
    assert not sample['valid'] and not r.snapshot()['ATTITUDE_QUATERNION']['available']
    json.dumps(r.snapshot(),allow_nan=False)


def test_missing_field_is_not_replaced_with_zero():
    r=TelemetryReceiver(1,1);msg=wire_message('SCALED_IMU');del msg.xacc
    sample=r.ingest(msg)
    assert not sample['valid'] and sample['fields']['xacc'] is None


def test_health_without_source_clock_does_not_inherit_previous_imu_time():
    r=TelemetryReceiver(1,1);r.ingest(wire_message('SCALED_IMU'))
    for name in ('SYS_STATUS','EKF_STATUS_REPORT'):
        sample=r.ingest(wire_message(name));assert sample['valid'] and sample['source_time'] is None


def test_optional_channels_keep_their_own_units_and_clock_domain():
    r=TelemetryReceiver(1,1)
    for name in ('RAW_IMU','HIGHRES_IMU','ACTUATOR_OUTPUT_STATUS'):
        sample=r.ingest(wire_message(name));assert sample['valid']
        assert sample['source_time']['field']=='time_usec' and sample['source_time']['clock_domain']=='message_native_origin_unqualified'


def link_with_messages(messages):
    link=MavlinkLink({'mavlink':{'connection':'unit-only'}})
    queue=list(messages);link.target_system=link.target_component=1
    link.telemetry_receiver=TelemetryReceiver(1,1)
    link.master=SimpleNamespace(recv_match=lambda **kw:queue.pop(0) if queue else None)
    return link


def test_production_link_drains_new_channels_and_keeps_servo_extensions_absent():
    link=link_with_messages([wire_message(n,version=1) for n in REQUEST_HZ]);link.drain()
    assert all(link.received_channels()[n]['available'] for n in REQUEST_HZ)
    assert link.servo_output_raw['servo1_raw']==1501 and link.servo_output_raw['servo9_raw'] is None
    assert set(REQUEST_HZ).issubset(link.telemetry_latest)


def test_unknown_battery_sentinels_are_unavailable_not_zero_or_65_volts():
    link=link_with_messages([wire_message('SYS_STATUS',changes={'voltage_battery':65535,'current_battery':-1,'battery_remaining':-1})])
    link.drain();assert all(v is None for v in link.battery.values())
    assert link.received_channels()['SYS_STATUS']['sample']['fields']['voltage_battery']==65535


def test_euler_packet_updates_only_legacy_display_and_never_quaternion():
    link=link_with_messages([wire_message('ATTITUDE',changes={'roll':1.2})]);link.drain()
    assert link.attitude['roll']==pytest.approx(1.2)
    assert not link.received_channels()['ATTITUDE_QUATERNION']['available']


def test_connect_only_requests_intervals_and_ack_does_not_count_as_sample(monkeypatch):
    import layer6.mavlink_link as module
    hb=wire_message('HEARTBEAT',changes={'autopilot':3,'type':1,'mavlink_version':3})
    sent=[];master=SimpleNamespace(wait_heartbeat=lambda **kw:hb,mode_mapping=lambda:{'MANUAL':0},
        mav=SimpleNamespace(command_long_send=lambda *a:sent.append(a)))
    monkeypatch.setattr(module.mavutil,'mavlink_connection',lambda *a,**kw:master)
    link=MavlinkLink({'mavlink':{'connection':'unit-only'}});link.connect()
    assert link.target_system==link.target_component==1 and len(sent)==9
    assert all(a[2]==511 for a in sent)  # SET_MESSAGE_INTERVAL, never ARM/DISARM.
    assert all(not row['available'] for row in link.received_channels().values())


def test_gui_uses_the_same_packaged_receiver_and_exposes_diagnostic_channels():
    tree=ast.parse((NATIVE/'src/gui/phoenix_f310_gui.py').read_text())
    assert any(isinstance(n,ast.ImportFrom) and n.module=='layer6.mavlink_link' for n in tree.body)
    assert not any(isinstance(n,ast.ClassDef) and n.name=='MavlinkLink' for n in tree.body)
    status=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_virtual_status_payload')
    assert any(isinstance(n,ast.Constant) and n.value=='telemetry_channels' for n in ast.walk(status))
