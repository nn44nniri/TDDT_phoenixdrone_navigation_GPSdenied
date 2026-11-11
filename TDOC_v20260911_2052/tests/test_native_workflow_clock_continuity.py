"""Clock routing from decoded measured wire samples; no flight/certificate claim."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from test_online_climate_handoff_alignment_v2052 import mod as workflow
from test_r5_01_telemetry_receivers import wire_message
from test_r5_04_gui_status import make_link
from layer6.status import project_status
from layer6.telemetry_source import native_source_contract
from tddt_tdoc.online_imu import canonical_imu
from tddt_tdoc.rtmo.realtime_scheduler import RTClock


def samples(link):
    result=[]
    for record in link.telemetry_receiver.history.records:
        if record['type']!='SCALED_IMU':continue
        normalized=link.telemetry_receiver.contracts.evaluate(record)['normalized']
        result.append({**{k:normalized['fields'][k] for k in workflow._NATIVE_IMU_VALUES},
            'source':'MAVLink/SCALED_IMU','frame':'BODY_FRD','plant_time_s':record['fields']['time_boot_ms']*.001,
            'time_boot_ms':record['fields']['time_boot_ms'],'history_sequence':record['history_sequence'],
            'timestamp_monotonic':record['timestamp_monotonic'],'wire_hex':record['wire_hex'],
            'wire_sha256':record['wire_sha256'],'session_id':record['session_id'],
            'monotonic_domain_id':record['monotonic_domain_id']})
    return result


def cycle_case(end_ms=5000):
    link=make_link(native_source_contract(),[
        wire_message('HEARTBEAT',changes={'autopilot':3,'type':1,'custom_mode':18}),
        wire_message('ATTITUDE',changes={'time_boot_ms':end_ms}),
        wire_message('ATTITUDE_QUATERNION',changes={'time_boot_ms':end_ms}),
        wire_message('GLOBAL_POSITION_INT',changes={'time_boot_ms':end_ms}),wire_message('VFR_HUD'),
        *[wire_message('SCALED_IMU',changes={'time_boot_ms':stamp}) for stamp in range(1000,end_ms+1,20)]])
    projected=project_status(link,include_imu_samples=True)
    gui={**projected,'schema':'Layer6-GUI-status-v1','schema_version':1,'ok':True,
        'mavlink_connected':True,'target_system':1,'target_component':1,
        'monotonic_domain_id':projected['domain'],'telemetry_metadata':projected['metadata']}
    imu=gui['imu'];ingress={'ok':True,'source':'Layer6/GUI/MAVLink','released':True,'gui_state':gui,
        'imu_samples':samples(link),'state':{'runtime_ready':1.,'imu_time_s':imu['plant_time_s'],
            'imu_frame':imu['frame'],'imu_source':imu['source'],
            **{k:imu[k] for k in workflow._NATIVE_IMU_VALUES}}}
    proof=canonical_imu(ingress)
    cycle={'transaction_id':'clock-test-cycle','orchestration_version':'test',
        'layer_status':{'2':{'ready':True},'3':{'ready':True}},
        'layer2_packets':{'MESA_NAV':{'timestamp':end_ms*.001,
            'context':{'online_state_ingress':{'ptlib_ingress':ingress}},
            'provenance':{'online_imu_ingress':proof}}},
        'layer3_result':{'snapshot':{'reference_time_s':end_ms*.001}},
        'layer4_result':{'verified_candidates':[{'candidate_id':'unit-boundary-only'}]},
        'downstream_layers':{'5':{'output':{'result':{'gates':{'FINAL':True}}}},
            '6':{'status':'NOT_INVOKED_PRE_RELEASE_VERIFY'},'7':{'status':'NOT_INVOKED_PRE_RELEASE_VERIFY'}},
        'provenance':{'rtmo_pre_release_verify':{'enabled':True,'non_actuating':True},
            'current_online_observation':{'source':'Layer6/GUI/MAVLink','plant_time_s':1.,'plant_time_present':True}}}
    return cycle,link


def bootstrap(cycle,clock):
    publications=[]
    def publish(**kwargs):
        publications.append(kwargs)
        return SimpleNamespace(ok=kwargs['bootstrap_qualified'],reason='unit-clock-routing-only',metadata={})
    orch=SimpleNamespace(run_cycle=lambda request:SimpleNamespace(to_dict=lambda:deepcopy(cycle)),
        rtmo_l4_artifacts=SimpleNamespace(publish_bootstrap=publish),versions=SimpleNamespace(active='test'))
    result=workflow._run_pre_release_verify(orch,{'transaction_id':'unit'},phase='vertical_takeoff',
        cycle_id=1,reference_time_s=1.,rtclock=clock)
    return result[0],publications


def test_bootstrap_consumes_real_history_and_uses_mesa_endpoint_not_initial_diagnostic():
    cycle,_=cycle_case();clock=RTClock(last_sim_time_s=1.)
    evidence,publications=bootstrap(cycle,clock)
    assert clock.last_sim_time_s==5. and evidence['consumed_native_clock']['ok']
    assert evidence['consumed_native_clock']['bound_s']==pytest.approx(.15)
    assert evidence['reference_time_s']==publications[0]['reference_time_s']==5.
    assert evidence['requested_reference_time_s']==1.
    assert evidence['qualified'] and publications[0]['bootstrap_qualified']
    assert evidence['non_actuating'] and evidence['control_effect_applied'] is False


def test_missing_measured_interval_blocks_bootstrap_without_partial_clock_advance():
    cycle,_=cycle_case();clock=RTClock(last_sim_time_s=1.)
    ingress=cycle['layer2_packets']['MESA_NAV']['context']['online_state_ingress']['ptlib_ingress']
    ingress['imu_samples']=[s for s in ingress['imu_samples'] if not 1.5<s['plant_time_s']<3.]
    evidence,publications=bootstrap(cycle,clock)
    assert clock.last_sim_time_s==1.
    assert not evidence['qualified'] and not publications[0]['bootstrap_qualified']
    assert evidence['reason']=='pre_release_native_clock_continuity_failed'


@pytest.mark.parametrize('fault',['session','domain','wire','normalized','endpoint_hash','snapshot','measured','gnss','order'])
def test_consumed_clock_rejects_forgery_or_wrong_endpoint(fault):
    cycle,_=cycle_case();mesa=cycle['layer2_packets']['MESA_NAV']
    ingress=mesa['context']['online_state_ingress']['ptlib_ingress'];rows=ingress['imu_samples']
    if fault=='session':rows[0]['session_id']='a'*32
    elif fault=='domain':rows[0]['monotonic_domain_id']='other-boot'
    elif fault=='wire':rows[0]['wire_hex']='00'
    elif fault=='normalized':rows[0]['accel_x_m_s2']+=1.
    elif fault=='endpoint_hash':ingress['gui_state']['telemetry_metadata']['SCALED_IMU']['wire_sha256']='0'*64
    elif fault=='snapshot':cycle['layer3_result']['snapshot']['reference_time_s']=6.
    elif fault=='measured':mesa['provenance']['online_imu_ingress']['measured_simulated_imu']=False
    elif fault=='gnss':mesa['provenance']['online_imu_ingress']['gnss_used']=True
    else:rows[0],rows[1]=rows[1],rows[0]
    with pytest.raises(ValueError):workflow._consumed_native_imu_clock(cycle)


def test_consumed_fresh_gui_tail_needs_no_fabricated_wire_bytes():
    cycle,_=cycle_case(end_ms=1400)
    rows=cycle['layer2_packets']['MESA_NAV']['context']['online_state_ingress']['ptlib_ingress']['imu_samples']
    for row in rows:row.pop('wire_hex')
    result=workflow._consumed_native_imu_clock(cycle)
    assert result['endpoint_s']==pytest.approx(1.4) and result['samples']==rows


def test_predecision_and_execution_history_are_merged_even_when_execution_is_nonempty():
    cycle,link=cycle_case(end_ms=1400)
    for stamp in range(1420,2001,20):
        link.telemetry_receiver.ingest(wire_message('SCALED_IMU',changes={'time_boot_ms':stamp}))
    execution=samples(link)[-35:]
    merged=workflow._merge_consumed_native_clock_samples(cycle,execution)
    assert len(merged)==51 and merged[0]['plant_time_s']==1. and merged[-1]['plant_time_s']==2.
    clock=RTClock(last_sim_time_s=1.)
    assert not clock.observe_measured_samples(2.,execution)['ok']
    assert clock.last_sim_time_s==1.
    assert clock.observe_measured_samples(2.,merged)['ok'] and clock.last_sim_time_s==2.
    conflict=deepcopy(execution);conflict[0].pop('wire_hex');conflict[0]['gyro_x_rad_s']+=1.
    with pytest.raises(ValueError,match='overlapping wire values conflict'):
        workflow._merge_consumed_native_clock_samples(cycle,conflict)
