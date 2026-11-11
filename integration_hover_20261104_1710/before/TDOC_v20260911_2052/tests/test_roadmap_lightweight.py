"""Boundary regressions for redundant work and first-session lifecycle."""
import copy
from types import SimpleNamespace

import pytest

from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.adapters.layer2 import Layer2Adapter
from tddt_tdoc.adapters.layer4 import Layer4Adapter
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter


def isolated_config(tmp_path):
    cfg=TDOCConfig.from_yaml('configs/tdoc.yaml')
    cfg.data['runtime']['root']=str(tmp_path/'runtime')
    return cfg


def manager(tmp_path):
    adapter=Layer4Adapter(isolated_config(tmp_path))
    adapter._ensure()
    return adapter,adapter.engine.mf


def certificate(model):
    return {'schema':'Layer4-FF-certificate-v4','origin':'FF_CURRENT','verified':True,
        'phase':'cruise','freshness_clock':'mission_reference','reference_time_s':2.,
        'freshness_s':30.,'state_envelope':{'ok':True},'action_envelope':{'ok':True},
        'context_signature':'C','scenario_semantics_hash':'S','ff_evidence_hash':'F',
        'model_dependencies':model,'verification_scope':'TRANSDOMAIN_MODEL_NATIVE_EXECUTION_DIAGNOSTIC',
        'model_prediction_only':True,'native_controller_certified':False,'flight_authorized':False}


@pytest.mark.parametrize('changed',[False,True])
def test_bank_scan_reads_model_twice_and_rejects_mid_scan_change(changed,tmp_path):
    adapter,mf=manager(tmp_path)
    try:
        current={'model_id':'M','artifacts':{'fmu':'sha'}};calls=[]
        def provider():
            calls.append(1)
            return {'model_id':'changed'} if changed and len(calls)>1 else copy.deepcopy(current)
        mf.model_dependencies_provider=provider
        mf._cache=[certificate(current) for _ in range(500)]
        assert mf.capability({'cruise':[3.]})==([] if changed else ['cruise'])
        assert len(calls)==2
        # A separate consumption still reads the live provider.
        assert mf._model_dependencies_match(certificate(current)) is (not changed)
        assert len(calls)==3
    finally:adapter.close()


def test_unused_prepared_first_session_is_adopted_once():
    a=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter)
    a._session_open=True;a._episode_id=None;a._active_tx=None;a._session_generation=2
    calls=[];a.reset_online_session=lambda *args:calls.append(args) or {'ok':True}
    result=a.begin_online_episode('E1')
    assert result['session_restarted'] is False and a._episode_id=='E1' and calls==[]
    a.begin_online_episode('E2')
    assert calls==[('online_episode_start','E2')]


@pytest.mark.parametrize('opened,active',[(False,None),(True,'previous-command')])
def test_non_pristine_session_is_not_adopted(opened,active):
    a=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter)
    a._session_open=opened;a._episode_id=None;a._active_tx=active
    calls=[];a.reset_online_session=lambda *args:calls.append(args) or {'ok':True}
    a.begin_online_episode('E1')
    assert calls==[('online_episode_start','E1')]


def test_real_upstream_deadline_precedes_downstream_artifact_hold():
    from tddt_tdoc.participation import cycle_decision_gate_trace
    cycle={'provenance':{'learning_mode':'ONLINE_JSBSIM'},'layer_status':{
        '2':{'ready':True,'invocation':{'ok':False,'late_output_discarded':True,
             'orchestration_timing':{'elapsed_s':6.,'deadline_s':5.}}},
        '4':{'ready':True}},'future_layers':{'5':{'status':'HELD'}}}
    trace=cycle_decision_gate_trace(cycle)
    assert trace['first_stop_gate']=='L2_DEADLINE_EXCEEDED'
    assert trace['layer5']['FINAL'] is None


def ground_ingress():
    from test_native_complete_readiness import complete_status
    from tddt_tdoc.online_imu import CHANNELS
    status=complete_status(2.)
    status.update(landed_state='ON_GROUND',throttle=0.)
    status['imu'].update(accel_x_m_s2=9.7674234,accel_z_m_s2=-1.04931155,gyro_y_rad_s=-.002)
    return {'ok':True,'source':'Layer6/GUI/MAVLink','released':True,'gui_state':status,
        'state':{'runtime_ready':1.,'imu_time_s':2.,'imu_frame':'BODY_FRD',
            'imu_source':'MAVLink/SCALED_IMU',**{k:status['imu'][k] for k in CHANNELS.values()}}}


def test_ground_imu_alignment_reduces_real_fmu_gravity_error_and_is_one_time(tmp_path):
    import math
    from tddt_tdoc.online_imu import CHANNELS
    adapter=Layer2Adapter(isolated_config(tmp_path));adapter._ensure()
    known={'pn':100.,'pe':200.,'pd':-1800.,'vn':0.,'ve':0.,'vd':0.,
        'qw':math.cos(math.radians(89)/2),'qx':0.,'qy':math.sin(math.radians(89)/2),'qz':0.}
    ingress=ground_ingress();unchanged=copy.deepcopy(ingress)
    payload={'learning_mode':'ONLINE_JSBSIM','context':{'online_episode_id':'E',
        'rtmo_data_authority':{'navigation':'MESA_STD'},'rtmo_navigation_initial_state':known,
        'rtmo_allow_initial_navigation_alignment':True,'online_state_ingress':{'ptlib_ingress':ingress}}}
    try:
        aligned=adapter._align_mesa_simulation_state(adapter.mesa,payload,{},source='test')
        assert aligned['applied'] and aligned['initial_attitude']['gnss_used'] is False,aligned
        adapter.mesa.fmu.set(**{t:ingress['state'][s] for t,s in CHANNELS.items()})
        packet=adapter.mesa.evaluate_packet(5,inputs={'dt':.02},context={},communication_step=.02)
        assert math.hypot(packet.state['velocity_n'],packet.state['velocity_e'])<1e-4
        before=adapter.mesa.fmu.get('rN','rE','rD','qw','qx','qy','qz')
        ingress['gui_state']['armed']=True
        second=adapter._align_mesa_simulation_state(adapter.mesa,payload,{},source='test')
        assert second['reason']=='mission_initialization_already_consumed'
        assert adapter.mesa.fmu.get(*before)==before
        ingress['gui_state']['armed']=False
        assert ingress==unchanged and known['qy']==math.sin(math.radians(89)/2)
    finally:adapter.close()


@pytest.mark.parametrize('fault',['armed','airborne','throttle','acceleration','angular_rate','frame','time'])
def test_initial_imu_attitude_rejects_unqualified_source(fault):
    from tddt_tdoc.online_imu import initial_ground_attitude
    ingress=ground_ingress();state=ingress['state'];gui=ingress['gui_state']
    if fault=='armed':gui['armed']=True
    elif fault=='airborne':gui['landed_state']='IN_AIR'
    elif fault=='throttle':gui['throttle']=.1
    elif fault=='acceleration':state['accel_x_m_s2']=gui['imu']['accel_x_m_s2']=15.
    elif fault=='angular_rate':state['gyro_y_rad_s']=gui['imu']['gyro_y_rad_s']=.1
    elif fault=='frame':state['imu_frame']='BODY_FLU'
    else:state['imu_time_s']=1.
    with pytest.raises(ValueError):initial_ground_attitude(ingress,{'qw':1.,'qx':0.,'qy':0.,'qz':0.})


def test_isolated_ff_identity_scope_is_local_and_rehashed_before_return(monkeypatch,tmp_path):
    import threading
    from tddt_tdoc.adapters.layer5 import Layer5Adapter
    import tddt_tdoc.model_compatibility as models
    cfg=isolated_config(tmp_path);a=Layer5Adapter(cfg);calls=[]
    body={'schema':models.SCHEMA,'revision':models.REVISION,'policy_sha256':models.POLICY_SHA256,
        'artifacts':{'test':{'sha256':'x','bytes':1}}}
    current={**body,'model_id':models.digest(body)};cert={'model_dependencies':current}
    def capture(config):calls.append(threading.current_thread().name);return copy.deepcopy(current)
    monkeypatch.setattr(models,'capture_model_dependencies',capture)
    try:
        with a.isolated_verification_model_scope():
            for _ in range(30):assert a._certificate_model_gate(cert)['ok']
            thread=threading.Thread(target=lambda:a._certificate_model_gate(cert),name='live-decision')
            thread.start();thread.join()
        assert len(calls)==3 and calls[1]=='live-decision'
        assert a._certificate_model_gate(cert)['ok'] and len(calls)==4
        with pytest.raises(ValueError,match='changed during isolated'):
            with a.isolated_verification_model_scope():current['model_id']='changed'
        assert a._isolated_model_dependencies.get() is None
    finally:a.close()


def test_boulder_reader_shares_only_source_data_and_preserves_actual_results(tmp_path):
    import json
    from tddt_tdoc.adapters.layer3 import Layer3Adapter
    from layer3.boulder_runtime import BoulderRuntimeAdapter
    adapter=Layer3Adapter(isolated_config(tmp_path))
    try:
        adapter._ensure();cfg=adapter.layer3_config
        shared=adapter._boulder_reader(cfg)
        isolated_cfg=copy.deepcopy(cfg)
        isolated_cfg.paths.snapshots_dir='/unused-isolated-workspace'
        assert adapter._boulder_reader(isolated_cfg) is shared
        assert not any(hasattr(shared,k) for k in ('mesa','wing','engine'))
        standalone=BoulderRuntimeAdapter(isolated_cfg)
        for lon,lat,hour in [(-105.3,40.01,16),(-105.29,40.015,19)]:
            a=shared.sample(lon,lat,hour=hour);b=standalone.sample(lon,lat,hour=hour)
            assert json.dumps(a,sort_keys=True,default=str)==json.dumps(b,sort_keys=True,default=str)
    finally:adapter.close()


def test_boulder_reader_invalidates_changed_file_and_path(tmp_path,monkeypatch):
    from tddt_tdoc.adapters.layer3 import Layer3Adapter
    import layer3.boulder_runtime as source
    cfg=SimpleNamespace(map_root=tmp_path/'map',crp_root=tmp_path/'crp',boulder_root=tmp_path/'code')
    for p in (cfg.map_root,cfg.crp_root,cfg.boulder_root):p.mkdir()
    data=cfg.crp_root/'crp_cell_assignments.csv.gz';data.write_bytes(b'first accepted input')
    constructed=[]
    def make(c):
        reader=SimpleNamespace(source_bytes=(c.crp_root/'crp_cell_assignments.csv.gz').read_bytes())
        constructed.append(reader);return reader
    monkeypatch.setattr(source,'BoulderRuntimeAdapter',make)
    adapter=Layer3Adapter(isolated_config(tmp_path))
    first=adapter._boulder_reader(cfg)
    assert adapter._boulder_reader(cfg) is first
    data.write_bytes(b'new input')
    second=adapter._boulder_reader(cfg)
    assert second is not first and second.source_bytes==b'new input'
    new=tmp_path/'other';new.mkdir();(new/data.name).write_bytes(b'another source')
    cfg.crp_root=new
    assert adapter._boulder_reader(cfg).source_bytes==b'another source'
    assert len(constructed)==3


def passive_entry_inputs():
    status=ground_ingress()['gui_state']
    status['telemetry_age_s']['EKF_STATUS_REPORT']=.01
    status['ekf_status']={'flags':1|2|4|16|32}
    status['fcu_health']={'onboard_control_sensors_present':0,'onboard_control_sensors_health':0}
    bootstrap={'schema':'TDOC-RTMO-pre-release-verify-v1','phase':'vertical_takeoff',
        'qualified':True,'non_actuating':True,'l5_final':True,'reference_time_s':1.,
        'layer2_current_state':True,'layer3_atomic_state':True,'l4_full_fidelity_verified':True,
        'control_effect_applied':False,'transaction_id':'actual-pre-release',
        'l4_artifact_bootstrap':{'ok':True}}
    return status,bootstrap


def test_passive_entry_changes_no_navigation_clock_or_control_authority():
    from tddt_tdoc.online_mission import passive_native_entry
    status,bootstrap=passive_entry_inputs();before=copy.deepcopy((status,bootstrap))
    result=passive_native_entry(status,bootstrap,expected_session_id='unit-only',environment_ready=True,max_age_s=30.)
    assert result['ok'] and result['lifecycle_plant_time_s']==2.
    assert not result['actuation_authority'] and not result['navigation_freshness_promoted']
    assert not result['estimator_reanchored'] and (status,bootstrap)==before


@pytest.mark.parametrize('fault',['armed','airborne','throttle','session','environment','stale','unverified','actuating','missing_L2'])
def test_passive_entry_cannot_promote_bad_or_expired_evidence(fault):
    from tddt_tdoc.online_mission import passive_native_entry
    status,bootstrap=passive_entry_inputs();session='unit-only';env=True
    if fault=='armed':status['armed']=True
    elif fault=='airborne':status['landed_state']='IN_AIR'
    elif fault=='throttle':status['throttle']=.1
    elif fault=='session':session='another-native-session'
    elif fault=='environment':env=False
    elif fault=='stale':bootstrap['reference_time_s']=-40.
    elif fault=='unverified':bootstrap['qualified']=False
    elif fault=='actuating':bootstrap['non_actuating']=False
    else:bootstrap['layer2_current_state']=False
    assert not passive_native_entry(status,bootstrap,expected_session_id=session,environment_ready=env,max_age_s=30.)['ok']
