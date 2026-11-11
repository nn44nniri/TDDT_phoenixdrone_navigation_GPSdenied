"""Negative acceptance through raw receiver -> status -> TDOC/IMU consumers."""
from copy import deepcopy
from types import SimpleNamespace
import time
import pytest
from test_r5_01_telemetry_receivers import wire_message
from test_r5_04_gui_status import make_link
from test_native_bridge_contracts import sample,ingress
from layer6.status import SCHEMA,CORE_CHANNELS,project_status,validate_observation_status
from layer6.telemetry import TelemetryReceiver
from layer6.telemetry_source import native_source_contract
from layer6.client import Layer6Client,Layer6TimeoutError
from layer6.feedback import build_execution_evidence
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter
from tddt_tdoc.online_imu import canonical_imu,measured_intervals
from tddt_tdoc.native_control_binding import fcu_estimator_ready


@pytest.fixture(scope='module')
def sender():
 s=native_source_contract();assert s['deployment_verified'];return s


def status(link):
 p=project_status(link,include_imu_samples=True)
 return {**p,'schema':SCHEMA,'schema_version':1,'ok':True,'timestamp_monotonic':p['now'],
  'mavlink_connected':p['connected'],'telemetry_age_s':p['ages'],'telemetry_metadata':p['metadata'],
  'monotonic_domain_id':p['domain'],'target_system':1,'target_component':1,
  'telemetry_received_monotonic':{k:v['received_monotonic'] for k,v in p['metadata'].items()},
  'command_first_sent_plant_time_s':None}


def adapter():
 a=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter)
 a._option=lambda k,d=None:{'require_body_attitude_quaternion':True,'telemetry_max_age_s':.2,'imu_max_age_s':.15}.get(k,d)
 a._environment_ready=lambda st:True  # Unit boundary; native audit preserves the real forcing rejection.
 a._origin={};a.layer5=SimpleNamespace(native_actuator_feedback=lambda st:{'ok':True,'feedback':{'unit_boundary':True}})
 return a


def test_complete_current_observations_can_reach_ingress_and_measured_IMU(sender):
 s=status(make_link(sender));a=adapter();assert validate_observation_status(s)['ok'] and a._fresh(s)
 result=a._ingress_from_status(s);packet=canonical_imu(result)
 assert packet['plant_time_s']==s['imu']['plant_time_s'] and len(packet['values'])==6
 assert not packet['truth_position_used'] and not packet['gnss_used']
 assert build_execution_evidence({'u_cmd':[0,0,0,0]},s)['evidence_type']=='simulation'


@pytest.mark.parametrize('channel',CORE_CHANNELS)
def test_required_channel_missing_is_not_ready_or_consumable(sender,channel):
 s=status(make_link(sender));del s['telemetry_metadata'][channel]
 a=adapter();assert not a._fresh(s) and a._state(s)['runtime_ready']==0 and not a._snapshot(s)['ready']
 with pytest.raises(ValueError,match='observations rejected'):a._ingress_from_status(s)
 with pytest.raises(ValueError,match='observations'):build_execution_evidence({'u_cmd':[0]*4},s)


@pytest.mark.parametrize('channel',['SYS_STATUS','SERVO_OUTPUT_RAW'])
def test_missing_health_or_actuator_cannot_reach_ingress(sender,channel):
 s=status(make_link(sender));del s['telemetry_metadata'][channel]
 with pytest.raises(ValueError,match='observations rejected'):adapter()._ingress_from_status(s)


def test_absent_optional_channels_do_not_create_an_all_channels_required_gate(sender):
 s=status(make_link(sender));assert not s['channels']['HIGHRES_IMU']['received']
 assert validate_observation_status(s)['ok'] and adapter()._fresh(s)


@pytest.mark.parametrize('channel',CORE_CHANNELS)
def test_fresh_numeric_age_cannot_override_invalid_channel_contract(sender,channel):
 s=status(make_link(sender));s['telemetry_age_s'][channel]=.001
 s['telemetry_metadata'][channel]['freshness']['fresh']=False
 assert not validate_observation_status(s)['ok'] and not adapter()._fresh(s)


def test_old_status_is_rechecked_at_consumer_time_without_rewriting_ages(sender):
 s=status(make_link(sender));before=deepcopy(s);now=max(m['freshness']['evaluated_monotonic'] for m in s['metadata'].values())+.151
 assert not validate_observation_status(s,now=now)['ok'] and s==before


@pytest.mark.parametrize('field,value',[('monotonic_domain_id','other-host'),('session_id',None),('schema_version',2),('ok',False)])
def test_identity_or_error_status_cannot_be_ready(sender,field,value):
 s=status(make_link(sender));s[field]=value;assert not adapter()._fresh(s)


@pytest.mark.parametrize('value',[None,True,float('nan'),float('inf')])
def test_invalid_current_IMU_axis_is_not_ready_or_projected_as_measured(sender,value):
 s=status(make_link(sender));s['imu']['accel_x_m_s2']=value
 assert not adapter()._fresh(s)
 with pytest.raises(ValueError):canonical_imu({'ok':True,'source':adapter().SOURCE,'released':True,'gui_state':s,'state':adapter()._state(s)})


def test_actual_wire_reverse_is_latched_and_keeps_source_timestamps(sender):
 r=TelemetryReceiver(1,1,source_contract=sender)
 for stamp in (1000,1020,900,1040):r.ingest(wire_message('SCALED_IMU',changes={'time_boot_ms':stamp}))
 page=r.history.page();assert [s['source_time']['value'] for s in page['samples']]==[1000,1020,900,1040]
 assert page['samples'][2]['source_time_relation']=='reverse'
 assert all(not c['freshness']['fresh'] for c in page['sample_contracts'])
 assert r.snapshot()['SCALED_IMU']['contract']['freshness']['reason']=='invalid_source_clock'


@pytest.mark.parametrize('times,match',[
 ([.02,.06,.04,.08],'Unordered'),([.02,.04,.04,.06],'duplicate'),([.02,.08],'interval'),
 ([.02,.12,.08],'future'),([-.02,.02,.04],'Unordered')])
def test_invalid_history_cannot_be_sorted_deduplicated_or_filled(times,match):
 data=ingress([sample(t) for t in times]);before=deepcopy(data)
 with pytest.raises(ValueError,match=match):measured_intervals(data,0.,canonical_imu(data),.025)
 assert data==before


def test_same_time_conflicting_packet_cannot_overwrite_the_buffer():
 data=ingress([sample(.02),sample(.04),sample(.06)]);p=canonical_imu(data);p['values']['imu_ax']=999.
 with pytest.raises(ValueError,match='differs'):measured_intervals(data,0.,p,.025)


def test_small_interval_still_checks_reverse_and_history_order():
 data=ingress([sample(.03),sample(.02)]);p=canonical_imu(data)
 with pytest.raises(ValueError,match='Unordered'):measured_intervals(data,0.,p,.15)
 with pytest.raises(ValueError,match='reversed'):measured_intervals(ingress([sample(.01)]),.02,canonical_imu(ingress([sample(.01)])),.15)


def test_actual_history_session_and_clock_fields_are_bound_to_current_view(sender):
 s=status(make_link(sender));a=adapter();data=a._ingress_from_status(s);p=canonical_imu(data)
 assert measured_intervals(data,.98,p,.025) is None
 for field,value in [('session_id','wrong'),('monotonic_domain_id','wrong'),('time_boot_ms',999)]:
  bad=deepcopy(data);bad['imu_samples'][0][field]=value
  with pytest.raises(ValueError,match='mismatch'):measured_intervals(bad,.98,p,.025)


def test_replay_coverage_must_include_the_consumer_cursor():
 data=ingress([sample(.20),sample(.22),sample(.24)])
 with pytest.raises(ValueError,match='interval'):measured_intervals(data,0.,canonical_imu(data),.025)


def test_valid_buffer_uses_arrival_order_and_exact_values():
 data=ingress([sample(i*.02,i) for i in range(1,7)]);before=deepcopy(data)
 steps=measured_intervals(data,0.,canonical_imu(data),.025)
 assert len(steps)==6 and [v['imu_ax'] for _,v in steps]==list(range(1,7))
 assert sum(dt for dt,_ in steps)==pytest.approx(.12) and data==before


def test_readiness_wait_does_not_accept_missing_IMU_because_heartbeat_is_stable(sender):
 s=status(make_link(sender));s['imu']=None;c=Layer6Client.__new__(Layer6Client);c.gui_status=lambda **kw:s
 with pytest.raises(Layer6TimeoutError):c.wait_fcu_ready(timeout=.002,stable_samples=1)
 good=status(make_link(sender));c.gui_status=lambda **kw:good
 assert c.wait_fcu_ready(timeout=.1,stable_samples=1)==good


def test_estimator_flags_do_not_override_invalid_health_metadata(sender):
 s=status(make_link(sender));s['ekf_status']['flags']=55
 assert fcu_estimator_ready(s)
 s['telemetry_metadata']['SYS_STATUS']['valid']=False
 assert not fcu_estimator_ready(s)


def test_expensive_environment_check_cannot_return_expired_status(sender,monkeypatch):
 s=status(make_link(sender));clock=[time.monotonic()];a=adapter();a._session_open=True
 a._get_client=lambda:SimpleNamespace(gui_status=lambda **kw:s)
 def environment(st):clock[0]+=.25;return True
 a._environment_ready=environment
 monkeypatch.setattr(time,'monotonic',lambda:clock[0])
 with pytest.raises(RuntimeError,match='not ready'):a._status()
