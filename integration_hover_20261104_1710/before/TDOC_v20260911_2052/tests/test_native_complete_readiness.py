"""Native readiness barrier and same-cycle navigation/airmass alignment regressions."""
from types import SimpleNamespace
import pytest

from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter
from tddt_tdoc.online_imu import wing_inputs_from_navigation


def complete_status(t):
    import time
    from layer6.status import SCHEMA, CORE_CHANNELS
    from layer6.timebase import monotonic_domain
    now=time.monotonic();domain=monotonic_domain();stamp=int(round(t*1000))
    t=stamp*.001
    metadata={name:{'valid':True,'source':'MAVLink/'+name,'source_system':1,'source_component':1,
        'source_time':{'field':'time_boot_ms','value':stamp,'unit':'ms','clock_domain':'FCU_BOOT'},
        'wire_sha256':'0'*64,'received_monotonic':now,
        'freshness':{'fresh':True,'reason':'fresh_local_observation','age_s':0.,'receipt_age_s':0.,
            'evaluated_monotonic':now,'max_age_s':.15,'monotonic_domain_id':domain}}
        for name in (*CORE_CHANNELS,'SERVO_OUTPUT_RAW','SYS_STATUS','EKF_STATUS_REPORT')}
    return {'schema':SCHEMA,'schema_version':1,'ok':True,'session_id':'1'*32,
        'target_system':1,'target_component':1,'monotonic_domain_id':domain,
        'telemetry_metadata':metadata,'status_quality':{'native_session_id':'unit-only'},
        'mavlink_connected':True,'mode':'MANUAL','armed':False,'plant_time_s':t,
        'latitude_deg':40.,'longitude_deg':-105.,'altitude_msl_m':1900.,
        'heading_deg':45.,'airspeed_m_s':1.,'velocity_n_m_s':0.,'velocity_e_m_s':0.,'velocity_d_m_s':0.,
        'attitude':{'roll':0.,'pitch':0.,'yaw':0.,'roll_rate':0.,'pitch_rate':0.,'yaw_rate':0.,
            'quaternion_wxyz':[1.,0.,0.,0.],'source':'MAVLink/ATTITUDE_QUATERNION',
            'frame':'BODY_FRD_TO_NED','plant_time_s':t},'servo_output_raw':{'ready':True},
        'battery':{'remaining_percent':100.},
        'telemetry_age_s':{k:.01 for k in ('HEARTBEAT','ATTITUDE','VFR_HUD','GLOBAL_POSITION_INT','SCALED_IMU','SERVO_OUTPUT_RAW','SYS_STATUS')},
        'imu':{'frame':'BODY_FRD','source':'MAVLink/SCALED_IMU','plant_time_s':t,'wire_sha256':'0'*64,
            'gyro_x_rad_s':0.,'gyro_y_rad_s':0.,'gyro_z_rad_s':0.,
            'accel_x_m_s2':0.,'accel_y_m_s2':0.,'accel_z_m_s2':-9.80665}}


def prepared_adapter(timeout=1.):
    options={'require_live_channels':True,'startup_timeout_s':timeout}
    config=SimpleNamespace(get=lambda key,default=None:options.get(key.rsplit('.',1)[-1],default))
    layer5=SimpleNamespace(native_actuator_feedback=lambda st:{'ok':bool(st.get('servo_output_raw')),'feedback':{}},
        online_control_readiness=lambda:{'ok':True})
    a=Layer6OnlineAdapter(config,layer5)
    a._client=SimpleNamespace(stack_responding=lambda:True,ensure_mavlink=lambda **k:None,
        wait_fcu_ready=lambda **k:complete_status(1.),
        native_session_status=lambda:{'process_ready':True,'session_id':'unit-only'})
    return a


def test_complete_readiness_waits_for_imu_battery_servo_and_live_clock():
    a=prepared_adapter();calls=[]
    def poll(**kwargs):
        calls.append(1);n=len(calls);st=complete_status(1.+n*.02)
        if n==1:st['imu']={}
        if n==2:
            st['battery']={};st['telemetry_metadata'].pop('SYS_STATUS')
        if n==3:
            st['servo_output_raw']={};st['telemetry_metadata'].pop('SERVO_OUTPUT_RAW')
        return st
    a._client.gui_status=poll
    result=a.prepare_online_transport_session()
    assert result['ok'] and result['ready'] and len(calls)==6,result
    assert a._session_open and not result['startup_actuation']


def test_missing_live_imu_never_enters_online_ready():
    a=prepared_adapter(.005)
    def poll(**kwargs):
        st=complete_status(1.);st['imu']={};return st
    a._client.gui_status=poll
    result=a.prepare_online_control_session()
    assert not result['ok'] and not a._session_open
    assert 'native live-channel readiness timeout' in result['error']


def test_transport_readiness_with_controller_blocker_never_arms_or_starts_mission():
    a=prepared_adapter()
    a._client.gui_status=lambda **kwargs:complete_status(2.)
    a.layer5.online_control_readiness=lambda:{'ok':True,'nominal_control_ready':False,
        'flight_release_blockers':['native_pilot_controller_prediction_unavailable']}
    a._option=lambda key,default=None:True if key=='ground_arm_bootstrap' else default
    a.prepare_online_transport_session=lambda:{'ok':True,'ready':True,
        'status':'READY','startup_actuation':False,'current_gui_state':complete_status(2.)}
    def forbidden_arm():
        pytest.fail('Controller prediction blocker must be checked before ARM')
    a._prepare_ground_arm=forbidden_arm
    result=a.prepare_online_control_session()
    assert result['ok'] is False and result['ready'] is False
    assert result['status']=='NOT_READY' and result['transport_ready'] is True
    assert result['reason']=='native_pilot_controller_prediction_unavailable'
    assert result['startup_actuation'] is False and result['actuation_executed'] is False


def test_short_telemetry_stall_is_resampled_without_relaxing_age_limit():
    a=prepared_adapter();a._session_open=True;calls=[]
    def poll(**kwargs):
        calls.append(1);st=complete_status(1.)
        if len(calls)==1:st['telemetry_age_s']['ATTITUDE']=3.
        return st
    a._client.gui_status=poll
    result=a._status()
    assert len(calls)==2 and result['telemetry_age_s']['ATTITUDE']==.01


def test_native_wing_cruise_example_is_replaced_by_same_cycle_mesa_and_wind():
    values={'air_velocity_x':.5,'air_velocity_y':13.,'state_x':0.,'state_y':0.}
    navigation={'position_n':4428910.,'position_e':474395.,'position_d':-1798.75,
                'velocity_n':0.,'velocity_e':0.,'velocity_d':0.}
    wind={'wind_u_3dw':.52,'wind_v_3dw':.86,'diagnostic_vertical_wind':.08}
    proof=wing_inputs_from_navigation(values,navigation,wind)
    assert values['air_velocity_y']==pytest.approx(-.86)
    assert values['air_velocity_x']==pytest.approx(-.52)
    assert values['air_velocity_h']==pytest.approx(-.08)
    assert values['state_x']==navigation['position_e'] and values['state_y']==navigation['position_n']
    assert values['state_h']==-navigation['position_d']
    # Recombining air velocity and wind returns MESA ground velocity exactly.
    assert values['air_velocity_y']+wind['wind_v_3dw']==navigation['velocity_n']
    assert values['air_velocity_x']+wind['wind_u_3dw']==navigation['velocity_e']
    assert not proof['gnss_used'] and not proof['truth_velocity_used']


def test_missing_navigation_or_wind_is_rejected_instead_of_using_example_cruise():
    with pytest.raises(KeyError):wing_inputs_from_navigation({}, {}, {})


def test_estimator_alignment_and_prearm_health_gate():
    from tddt_tdoc.native_control_binding import fcu_estimator_ready
    st=complete_status(1.)
    assert not fcu_estimator_ready(st)
    st['telemetry_age_s']['EKF_STATUS_REPORT']=.01
    st['ekf_status']={'flags':55}
    st['fcu_health']={'onboard_control_sensors_present':268435456,'onboard_control_sensors_health':0}
    assert not fcu_estimator_ready(st)
    st['fcu_health']['onboard_control_sensors_health']=268435456
    assert fcu_estimator_ready(st)
    st['ekf_status']['flags']=1
    assert not fcu_estimator_ready(st)
    st['ekf_status']['flags']=55|1024
    assert not fcu_estimator_ready(st)
    st['ekf_status']['flags']=55
    st['telemetry_age_s']['EKF_STATUS_REPORT']=1.
    assert not fcu_estimator_ready(st)


def test_preparation_waits_for_structured_fcu_estimator_health():
    a=prepared_adapter();original=a._option
    a._option=lambda key,default=None:True if key=='require_fcu_estimator_ready' else original(key,default)
    calls=[]
    def poll(**kwargs):
        calls.append(1);n=len(calls);st=complete_status(1.+n*.02)
        st['telemetry_age_s']['EKF_STATUS_REPORT']=.01
        st['ekf_status']={'flags':1 if n<3 else 55}
        st['fcu_health']={'onboard_control_sensors_present':268435456,'onboard_control_sensors_health':268435456}
        return st
    a._client.gui_status=poll
    result=a.prepare_online_transport_session()
    assert result['ok'] and len(calls)==5 and not result['startup_actuation'],result
