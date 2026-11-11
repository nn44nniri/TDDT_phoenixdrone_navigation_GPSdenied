from __future__ import annotations
import math
from pathlib import Path

from tddt_tdoc.vehicle_state import phoenix_state_from_ptlib_snapshot, quaternion_to_euler

ROOT=Path(__file__).parents[1]

def _snapshot():
    return {'ready':True,'groups':{
      'ATTITUDE':{'source_message':'ATTITUDE','age_s':0.02,'payload':{'roll':0.1,'pitch':-0.2,'yaw':0.3,'rollspeed':0.01,'pitchspeed':0.02,'yawspeed':0.03}},
      'AIRSPEED':{'source_message':'VFR_HUD','age_s':0.03,'payload':{'airspeed':12.5,'groundspeed':10.0,'climb':1.2,'heading':42}},
      'POSITION':{'source_message':'GLOBAL_POSITION_INT','payload':{'lat':400100000,'lon':-1053000000,'alt':1700000,'relative_alt':5000}},
      'VELOCITY':{'source_message':'GLOBAL_POSITION_INT','payload':{'vx':600,'vy':800,'vz':-120}},
      'ACTUATOR':{'source_message':'SERVO_OUTPUT_RAW','age_s':0.04,'payload':{'servo1_raw':1600,'servo2_raw':1400,'servo3_raw':1500,'servo4_raw':1250}},
    }}

def _l5():
    return {'phoenix':{'limits':{'elevon_rad':0.53,'motor_speed_rad_s':800.0},'pwm_anchors':{
      'SERVO1':{'min':1000,'trim':1500,'max':2000},'SERVO2':{'min':1000,'trim':1500,'max':2000},
      'SERVO3':{'min':1000,'trim':1000,'max':2000},'SERVO4':{'min':1000,'trim':1000,'max':2000}}}}

def test_live_phoenix_state_maps_documented_servo_outputs_and_estimates():
    s=phoenix_state_from_ptlib_snapshot(_snapshot(),_l5())
    assert abs(s['attitude']['roll_deg']-math.degrees(0.1))<1e-9
    assert s['speed']['airspeed_m_s']==12.5 and s['speed']['groundspeed_m_s']==10.0
    a=s['actuators']
    assert a['physical_mapping']=={'SERVO1':'left_elevon','SERVO2':'right_elevon','SERVO3':'left_motor','SERVO4':'right_motor'}
    assert a['left_elevon']['pwm_us']==1600 and a['right_elevon']['pwm_us']==1400
    assert abs(a['left_elevon']['estimated_angle_rad']-0.106)<1e-9
    assert a['left_motor']['estimated_speed_rad_s']==400.0 and a['right_motor']['estimated_speed_rad_s']==200.0
    assert a['left_elevon']['angle_measured'] is False and a['left_motor']['speed_measured'] is False
    assert s['motion']['state']=='MOVING'

def test_stationary_neutral_feedback_is_reported_not_fabricated_as_motion():
    snap=_snapshot(); snap['groups']['AIRSPEED']['payload']['groundspeed']=0.0; snap['groups']['VELOCITY']['payload']={'vx':0,'vy':0,'vz':0}
    snap['groups']['ACTUATOR']['payload']={'servo1_raw':1500,'servo2_raw':1500,'servo3_raw':1000,'servo4_raw':1000}
    s=phoenix_state_from_ptlib_snapshot(snap,_l5())
    assert s['motion']['state']=='NO_ACTUATION_STATIONARY'
    assert s['actuators']['left_motor']['estimated_speed_rpm']==0.0

def test_quaternion_to_euler_identity():
    e=quaternion_to_euler(1,0,0,0)
    assert e['roll_deg']==0.0 and e['pitch_deg']==0.0 and e['yaw_deg']==0.0

def test_reporting_source_is_decision_oriented_and_keeps_raw_audit_collapsed():
    src=(ROOT/'src/tddt_tdoc/reporting.py').read_text()
    assert 'TDOC — Decision / Vehicle / Actuator Dashboard' in src
    assert 'Mission progress — live PT_LIB position' in src
    assert 'Latest Phoenix actuator state / feedback' in src
    assert '<details><summary>Complete flattened parameter inventory' in src

def test_offline_actuator_report_contains_vehicle_attitude_speed_and_motor_state():
    src=(ROOT/'scripts/run_training_workflows.py').read_text()
    for token in ('vehicle_roll_deg','vehicle_pitch_deg','vehicle_yaw_deg','vehicle_airspeed_m_s','vehicle_groundspeed_m_s','left_motor_speed_rpm','right_motor_speed_rpm','left_elevon_command_deg','right_elevon_command_deg'):
        assert token in src
    assert "'actuator_vehicle_state':offline_vehicle" in src
