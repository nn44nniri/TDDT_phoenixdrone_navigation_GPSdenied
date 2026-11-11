import copy
import math
from pathlib import Path
import pytest
from twins_fmi import FormulaFMU
from twins_fmi.native_actuator_binding import (ActuatorReplaySample, NativeReplayRejected,
                                                replay_native_actuator)

ROOT = Path(__file__).resolve().parents[1]


def record():
    v = dict(dt=0.01, pn=0, pe=0, pd=-20, vn=0, ve=0, vd=0,
             qw=1,qx=0,qy=0,qz=0,p_rate=0,q_rate=0,r_rate=0,
             omega_left=100, omega_right=100, soc=.9,
             left_motor_cmd=.7, right_motor_cmd=.7,
             left_elevon_rad=0, right_elevon_rad=0,wind_n=0,wind_e=0,wind_d=0,
             air_density=1.225)
    return dict(values=v, transaction_id='txn01',snapshot_id='snap01',
                controller_identity='FCU-mode-QHOVER-params-sha256',model_identity='jsbsim-sha256',
                forcing_identity='forcing-sha256',mode='QHOVER',
                actuator_source='fcu_servo_output_with_verified_mapping',
                motor_state_source='jsbsim_property', plant_time_s=2.0,
                actuator_timestamp_s=1.99, motor_state_timestamp_s=1.99)


def test_op27_actuator_replay_model_only():
    with FormulaFMU(ROOT/'fmus'/'WingMPET.fmu') as f:
        result = replay_native_actuator(f,ActuatorReplaySample.from_mapping(record()))
    assert result['operation_code']==27
    assert result['native_controller_prediction_certified'] is False
    assert result['flight_authorized'] is False
    assert result['model_domain']=='NATIVE_ACTUATOR_OUTPUT_SURROGATE'
    assert result['prediction']['next_omega_left']>100
    assert result['prediction']['vtol_feasible'] in (0,1)
    assert len(result['model_fmu_sha256'])==64


def test_ground_contact_is_explicit_not_synthetic():
    a=record(); a['ground_supported']=True; a['ground_contact_evidence']='jsbsim_ground_contact_at_plant_time'
    with FormulaFMU(ROOT/'fmus'/'WingMPET.fmu') as f:
        result=replay_native_actuator(f,ActuatorReplaySample.from_mapping(a))
    assert result['operation_code']==35


@pytest.mark.parametrize('field,value', [
    ('actuator_source','pilot_stick'),('motor_state_source','pwm'),
    ('mode','AUTO'),('plant_time_s',math.inf),
    ('actuator_timestamp_s',2.2),('motor_state_timestamp_s',1.0),
    ('forcing_identity',''),('ground_supported',1),
])
def test_reject_metadata(field,value):
    a=record();a[field]=value
    with pytest.raises(NativeReplayRejected):
        ActuatorReplaySample.from_mapping(a).validated()


@pytest.mark.parametrize('field,value', [
    ('dt',0),('dt',.2),('omega_left',999),('left_motor_cmd',1.1),
    ('right_motor_cmd',float('nan')),('left_elevon_rad',.7),
    ('soc',-1),('qw',0),('air_density',0),
])
def test_reject_physical_inputs(field,value):
    a=record();a['values'][field]=value
    if field=='qw': a['values'].update(qx=0,qy=0,qz=0)
    with pytest.raises(NativeReplayRejected):
        ActuatorReplaySample.from_mapping(a).validated()


def test_estimated_omega_uncertainty_required():
    a=record();a['motor_state_source']='dynamic_motor_estimator'
    with pytest.raises(NativeReplayRejected):
        ActuatorReplaySample.from_mapping(a).validated()
    a['motor_state_uncertainty_rad_s']=15.0
    assert ActuatorReplaySample.from_mapping(a).validated()


def test_no_missing_input_zero_fill():
    a=record();del a['values']['omega_right']
    with pytest.raises(NativeReplayRejected):
        ActuatorReplaySample.from_mapping(a).validated()


def test_ground_without_evidence_rejected():
    a=record();a['ground_supported']=True
    with pytest.raises(NativeReplayRejected):
        ActuatorReplaySample.from_mapping(a).validated()


def test_fmu_fail_closed_native_direct_bad_command():
    a=record()['values'];a['right_motor_cmd']=1.5
    with FormulaFMU(ROOT/'fmus'/'WingMPET.fmu') as f:
        out=f.evaluate_operation(27,use_config=False,require_all_inputs=True,inputs=a)
    assert out['vtol_feasible']==0.0 and out['quality_score']==0.0
    assert all(math.isfinite(x) for x in out.values())


def test_fmu_fail_closed_native_direct_nonfinite():
    a=record()['values'];a['omega_left']=float('nan')
    with FormulaFMU(ROOT/'fmus'/'WingMPET.fmu') as f:
        out=f.evaluate_operation(27,use_config=False,require_all_inputs=True,inputs=a)
    assert out['vtol_feasible']==0.0 and out['quality_score']==0.0
    assert all(math.isfinite(x) for x in out.values())


def test_motor_transient_strict_increase_and_decay():
    a=record()['values']
    with FormulaFMU(ROOT/'fmus'/'WingMPET.fmu') as f:
        up=f.evaluate_operation(28, use_config=False,require_all_inputs=True,
                                dt=.01,omega_left=100,omega_right=100,
                                left_motor_cmd=.8,right_motor_cmd=.8)
        down=f.evaluate_operation(28,use_config=False,require_all_inputs=True,
                                  dt=.01,omega_left=up['next_omega_left'],omega_right=up['next_omega_right'],
                                  left_motor_cmd=0,right_motor_cmd=0)
    assert 100 < up['next_omega_left'] < 640
    assert 0 < down['next_omega_left'] < up['next_omega_left']


def test_motor_lag_matches_logged_jsbsim_coefficient_not_an_online_certificate():
    # JSBSim Phoenix log reports LAG_FILTER C[1]=55.555556 s^-1: tau=0.018 s.
    tau=1.0 / 55.555556
    with FormulaFMU(ROOT/'fmus'/'WingMPET.fmu') as f:
        up=f.evaluate_operation(28, use_config=False, require_all_inputs=True,
             dt=.01,omega_left=100,omega_right=100,left_motor_cmd=.8,right_motor_cmd=.8)
        down=f.evaluate_operation(28, use_config=False, require_all_inputs=True,
             dt=.01,omega_left=500,omega_right=500,left_motor_cmd=0,right_motor_cmd=0)
    assert up['next_omega_left']==pytest.approx(640+(100-640)*math.exp(-.01/tau),rel=1e-6)
    assert down['next_omega_left']==pytest.approx(500*math.exp(-.01/tau),rel=1e-6)
