from copy import deepcopy
import math
import pytest

from twins_fmi.realtime import RealtimeTwins
from layer3.realtime import atomic_snapshot
from layer4.realtime import predict
from layer5.realtime import decide
from tddt_layer7.realtime import feedback,digest


def snapshot():
    nav={'time_s':10.,'position_ned_m':[0.,0.,-3.],'velocity_ned_m_s':[0.]*3,
         'q_wxyz':[math.sqrt(.5),0.,math.sqrt(.5),0.],'body_rates_rad_s':[0.]*3,
         'truth_used':False,'GNSS_used':False,'vision_used':False,'odometry_used':False}
    wing={'time_s':10.,'reduced_inner_loop':{'disturbance_ned_m_s2':[0.]*3,
        'inner_loop_tau_s':.35,'feedback_version':0},'maximum_total_thrust_N':10.,'mass_kg':.65,'FMU_operation':27}
    return atomic_snapshot(nav,wing,{'wind_ned_m_s':[2.,-3.,0.]},phase='hover',
        reference={'position_ned_m':[0.,0.,-3.]},transaction_id='tx',origin={})


@pytest.mark.parametrize('forbidden',['POSITION','VELOCITY','ATTITUDE','SIM_STATE','ODOMETRY','VISION_POSITION_ESTIMATE','WIND'])
def test_navigation_boundary_rejects_forbidden_measurements(forbidden):
    nav=RealtimeTwins.__new__(RealtimeTwins)
    with pytest.raises(ValueError,match='unapproved navigation'):
        nav.step({forbidden:{}},[])


def test_atomic_snapshot_does_not_share_mutable_twin_state_and_rejects_truth():
    s=snapshot();original=s['snapshot_hash'];s['twins']['MESA_NAV']['vision_used']=True
    with pytest.raises(ValueError,match='sensor set'):
        atomic_snapshot(s['twins']['MESA_NAV'],s['twins']['WingMPET'],s['twins']['MicroMetTwin'],
            phase='hover',reference=s['reference'],transaction_id='other',origin={})
    assert snapshot()['snapshot_hash']==original


def parameters():
    return {'Q_A_ANGLE_MAX':30.,'RC1_DZ':30.,'RC2_DZ':30.,'RC3_DZ':30.,
            'Q_PILOT_SPD_UP':2.5,'Q_PILOT_SPD_DN':0.,'Q_TAILSIT_INPUT':0.}


def test_same_cycle_candidate_mpc_hover_and_climb_use_qhover_semantics():
    s=snapshot();forecast=predict(s)
    assert len(forecast['candidates'])==9
    assert all(c['prediction_tube'][0]['time_s']==10. for c in forecast['candidates'])
    d=decide(s,forecast,parameters(),heading_reference=0.)
    assert d['gates']['FINAL'] and d['u_cmd']==pytest.approx([0.,0.,.5,0.])
    s['reference']['position_ned_m'][2]=-8.
    d=decide(s,predict(s),parameters(),heading_reference=0.)
    assert d['mapping']['climb_rate_up_m_s']==1.2
    assert d['u_cmd'][2]==pytest.approx(.5+.03+.47*1.2/2.5)
    forecast['snapshot_hash']='old'
    with pytest.raises(ValueError,match='snapshot mismatch'):decide(s,forecast,parameters(),heading_reference=0.)


def test_real_wire_witness_pairs_only_the_sent_command_and_original_time():
    from test_r6_03_first_move_segment import close
    from layer6.telemetry_source import native_source_contract
    _,ctx,_,proof=close(native_source_contract());st=proof['snapshot'];t=st['plant_time_s']
    requested=ctx['requested_gui_state'];u=[requested[k] for k in ('roll','pitch','throttle','yaw')]
    decision={'snapshot_hash':'source','candidate_id':'C1','u_cmd':u,'intake_instance_id':ctx['instance_id'],
        'source_time_s':t-.1,'prediction':{'prediction_tube':[
            {'time_s':t-.1,'state':[0.]*6},{'time_s':t+1.,'state':[0.]*6}]}}
    receipt={'correlation':st['command_correlation'],'execution_witness':st,'armed':True,'mode':'QHOVER'}
    nav={'time_s':t,'position_ned_m':[0.]*3,'velocity_ned_m_s':[0.]*3}
    model={'disturbance_ned_m_s2':[0.]*3,'inner_loop_tau_s':.35,'feedback_version':0}
    pending={'tx':deepcopy(decision)};out=feedback(nav,receipt,pending,model)
    assert out['ok'] and out['sensor_only'] and not out['FCU_pose_feedback']
    assert out['prediction_source_time_s']==t-.1 and not out['prediction_retimed']
    assert pending=={}
    nav['time_s']=t+3.
    assert feedback(nav,receipt,{'tx':deepcopy(decision)},model)['reason']=='observation_outside_original_prediction_time'
    nav['time_s']=t
    forged=deepcopy(receipt);forged['correlation']['record']['barrier_nonce']+=1
    forged['correlation']['record_hash']=digest(forged['correlation']['record'])
    assert feedback(nav,forged,{'tx':deepcopy(decision)},model)['ok'] is False


def test_stationary_vertical_tailsitter_attitude_is_not_euler_singular():
    import numpy as np
    from scipy.spatial.transform import Rotation
    from twins_fmi.realtime import stationary_attitude
    reference=np.array([.20663262,.02780473,.47078424])
    expected=Rotation.from_euler('z',45.6,degrees=True)*Rotation.from_euler('y',90,degrees=True)
    acc=expected.inv().apply([0.,0.,-9.80665])
    mag=expected.inv().apply(reference)
    for noise in (-1e-5,1e-5):
        measured=acc+np.array([0.,noise,noise])
        actual=stationary_attitude(measured,mag,reference)
        assert (expected.inv()*actual).magnitude()<1e-4
    with pytest.raises(ValueError,match='independent'):
        stationary_attitude([1.,0.,0.],[1.,0.,0.],reference)


def test_ground_motor_cut_requires_sensor_qualified_contact_and_mode_matches_receipt():
    s=snapshot();s['phase']='contact_settle';s['twins']['MESA_NAV']['position_ned_m'][2]=-.2
    s['reference']['contact_evidence']={'qualified':True,'qualified_time_s':9.8,
        'source':'MESA/barometer/IMU only'}
    d=decide(s,predict(s),parameters(),heading_reference=0.)
    assert d['mode']=='QSTABILIZE' and d['u_cmd']==[0.]*4
    s['reference']['contact_evidence']['qualified_time_s']=-3.
    with pytest.raises(ValueError,match='no safe model candidate'):
        decide(s,predict(s),parameters(),heading_reference=0.)
    s['reference']['contact_evidence']['qualified_time_s']=9.8
    s['twins']['MESA_NAV']['position_ned_m'][2]=-3.
    with pytest.raises(ValueError,match='no safe model candidate'):
        decide(s,predict(s),parameters(),heading_reference=0.)


def test_landing_contact_does_not_confuse_flare_descent_with_ground_support():
    from tddt_tdoc.realtime import ContactSupport
    support=ContactSupport()
    for k in range(40):
        assert support.update(k*.1,.8-k*.02,.2,.03) is None
    for k in range(10):
        certificate=support.update(4.+k*.1,.015,.01,.05)
    assert certificate['qualified'] and certificate['condition']=='barometric_stall'
    assert support.update(5.1,.02,1.,8.) is certificate


def test_only_near_ground_impact_after_descent_can_latch_motor_cut():
    from tddt_tdoc.realtime import ContactSupport
    support=ContactSupport()
    assert support.update(1.,2.,.3,4.) is None
    assert support.update(1.1,.2,.2,.02) is None
    certificate=support.update(1.2,.04,.1,2.)
    assert certificate['condition']=='near_ground_IMU_impact'
    s=snapshot();s['phase']='contact_settle';n=s['twins']['MESA_NAV'];n['position_ned_m'][2]=-.04
    n['body_rates_rad_s']=[9.,9.,9.]
    s['reference']['contact_evidence']={**certificate,'qualified_time_s':9.9}
    assert decide(s,predict(s),parameters(),heading_reference=0.)['u_cmd']==[0.]*4
    s['phase']='hover'
    with pytest.raises(ValueError,match='body-rate'):
        decide(s,predict(s),parameters(),heading_reference=0.)


def test_native_mesa_fmu_keeps_powered_translation_and_liftoff_impulse():
    import numpy as np
    from pathlib import Path
    from scipy.spatial.transform import Rotation
    from tddt_tdoc.config import TDOCConfig
    from tddt_tdoc.adapters.layer2 import Layer2Adapter
    from twins_fmi.realtime import geomagnetic_reference
    root=Path(__file__).resolve().parents[2]
    cfg=TDOCConfig.from_yaml(root/'TDOC_v20260911_2052/configs/tdoc.yaml')
    adapter=Layer2Adapter(cfg)
    try:
        attitude=Rotation.from_euler('y',90,degrees=True)
        reference,_=geomagnetic_reference(root/'layer6_v20261007_1032/FlightStack/ardupilot/libraries/AP_Declination/tables.cpp',40.01,-105.3)
        mag=attitude.inv().apply(reference)
        def sample(t,acc):
            force=attitude.inv().apply(acc)
            return {'plant_time_s':t,'wire_sha256':str(t),'data':dict(zip(
                ('xacc','yacc','zacc','xgyro','ygyro','zgyro','xmag','ymag','zmag','pressure_alt','fields_updated'),
                [*force,0.,0.,0.,*mag,1000.,0]),mavpackettype='HIGHRES_IMU')}
        cal=[sample(i*.01,[0.,0.,-9.80665]) for i in range(301)]
        twins=RealtimeTwins(adapter,{'latitude_deg':40.01,'longitude_deg':-105.3,'heading_deg':0.},cal,
            {'wind_ned_m_s':[0.,0.,0.],'air_density_kg_m3':1.})
        twins.takeoff_requested=True
        for i in range(1,201):
            row=sample(3.+i*.01,[.5,0.,-10.30665])
            nav=twins.step({'HIGHRES_IMU':row,'ULTRASONIC_WIND':{'present':False}},[row])
        assert nav['velocity_ned_m_s']==pytest.approx([1.,0.,-1.],abs=.005)
        assert nav['initial_ground_support'] is False
        assert nav['airborne_gravity_pseudo_measurement'] is False
    finally:adapter.close()


def test_scalar_pitot_without_wind_observation_cannot_anchor_ground_velocity():
    import numpy as np
    nav=RealtimeTwins.__new__(RealtimeTwins)
    nav.wind=np.array([2.,-1.,.4]);nav.P=np.eye(9)
    initial_cov=nav.P[:6,:6].copy();state=np.array([0.,0.,0.,.5,.2,-.1])
    for _ in range(100):out=nav._pitot_wind_only(1.8,state)
    assert out['navigation_state_corrected'] is False
    assert np.array_equal(nav.P[:6,:6],initial_cov)
    assert np.array_equal(state,np.array([0.,0.,0.,.5,.2,-.1]))


def test_vtol_readiness_accepts_const_position_only_with_valid_vertical_ekf_and_real_prearm_health():
    from tddt_tdoc.native_control_binding import fcu_estimator_ready
    s={'mode':'QHOVER','telemetry_age_s':{'SYS_STATUS':.1,'EKF_STATUS_REPORT':.1},
       'ekf_status':{'flags':167},'fcu_health':{'onboard_control_sensors_present':268435456,
       'onboard_control_sensors_health':268435456}}
    assert not fcu_estimator_ready(s)
    assert fcu_estimator_ready(s,require_horizontal_position=False)
    s['fcu_health']['onboard_control_sensors_health']=0
    assert not fcu_estimator_ready(s,require_horizontal_position=False)
    s['fcu_health']['onboard_control_sensors_health']=268435456;s['mode']='QLOITER'
    assert not fcu_estimator_ready(s,require_horizontal_position=False)


def test_physical_launch_check_requires_same_session_and_no_navigation_use():
    import time
    from tddt_tdoc.native_control_binding import native_physical_origin_reference
    status={'status_quality':{'native_session_id':'session'}}
    p={'latitude_deg':40.,'longitude_deg':-105.,'altitude_msl_m':1800.,'received_monotonic_s':time.monotonic(),
       'source':'SIM_STATE; visualization only','navigation_input':False}
    n={'session_id':'session','panels':{'evidence':{'session_id':'session','physical_map':{
       'ready':True,'navigation_input':False,'position':p}}}}
    assert native_physical_origin_reference(n,status)['latitude_deg']==40.
    n['session_id']='other'
    with pytest.raises(ValueError,match='same-session'):native_physical_origin_reference(n,status)
    n['session_id']='session';p['navigation_input']=True
    with pytest.raises(ValueError,match='validation reference'):native_physical_origin_reference(n,status)


def test_earth_rates_use_own_navigation_state_and_coriolis_adds_no_energy():
    import numpy as np
    from twins_fmi.realtime import navigation_earth_model
    origin={'latitude_deg':40.,'longitude_deg':-105.,'altitude_msl_m':1800.}
    lat,lon,transport,earth=navigation_earth_model(origin,np.array([0.,0.,0.,10.,0.,0.]))
    assert (lat,lon)==pytest.approx((40.,-105.))
    assert transport[1]<0 and earth[0]>0 and earth[2]<0
    correction=-np.cross(2*earth+transport,[10.,0.,0.])
    assert correction[1]>0 and correction@np.array([10.,0.,0.])==pytest.approx(0.)
    _,_,stationary,_=navigation_earth_model(origin,np.zeros(6))
    assert stationary==pytest.approx([0.,0.,0.])


def test_landing_flare_levels_attitude_and_limits_descent_without_zero_velocity_anchor():
    s=snapshot();s['phase']='landing_flare';s['twins']['MESA_NAV']['position_ned_m'][2]=-.7
    s['twins']['MESA_NAV']['velocity_ned_m_s']=[.3,-.2,.1]
    s['reference']['position_ned_m']=[0.,0.,.8]
    forecast=predict(s);d=decide(s,forecast,parameters(),heading_reference=1.)
    assert len(forecast['candidates'])==1 and d['mode']=='QHOVER'
    assert d['u_cmd']==pytest.approx([0.,0.,.423,0.])
    assert forecast['candidates'][0]['prediction_tube'][0]['state'][3:5]==[.3,-.2]


def test_atomic_network_rejects_wing_using_a_different_climate_sample():
    s=snapshot();wing=s['twins']['WingMPET'];climate=s['twins']['MicroMetTwin']
    climate.update(time_s=10.,forcing_sha256='forcing')
    wing.update(climate_input_time_s=9.,climate_input_forcing_sha256='forcing')
    with pytest.raises(ValueError,match='climate inputs differ'):
        atomic_snapshot(s['twins']['MESA_NAV'],wing,climate,phase=s['phase'],reference=s['reference'],transaction_id='new',origin={})
