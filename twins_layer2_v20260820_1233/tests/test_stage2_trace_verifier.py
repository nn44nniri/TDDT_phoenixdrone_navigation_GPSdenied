from pathlib import Path
import importlib.util
import pytest
from twins_fmi import FormulaFMU
from twins_fmi.native_actuator_binding import ActuatorReplaySample, NativeReplayRejected, replay_native_actuator
from test_stage2_native_actuator_binding import record

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('stage2_verify', ROOT/'scripts'/'verify_stage2_native_trace.py')
trace_module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(trace_module)


def pairs(fmu, count=3):
    rows=[]
    for i in range(count):
        a=record()
        a['transaction_id']=f't{i}'
        a['snapshot_id']=f's{i}'
        a['plant_time_s']=2.0+i*.2
        a['actuator_timestamp_s']=a['plant_time_s']-.01
        a['motor_state_timestamp_s']=a['plant_time_s']-.01
        pred=replay_native_actuator(fmu,ActuatorReplaySample.from_mapping(a))['prediction']
        obs=dict(transaction_id=a['transaction_id'],snapshot_id=a['snapshot_id'],
                 controller_identity=a['controller_identity'],model_identity=a['model_identity'],
                 motor_state_source='jsbsim_property',plant_time_s=a['plant_time_s']+.01,
                 vn=pred['next_vn'],ve=pred['next_ve'],vd=pred['next_vd'],
                 omega_left=pred['next_omega_left'],omega_right=pred['next_omega_right'])
        rows.append(dict(sample=a,observation=obs))
    return rows


def test_fixture_math_comparator_positive_not_site_evidence():
    # These are model-generated fixtures, not JSBSim/native observations.
    with FormulaFMU(ROOT/'fmus'/'WingMPET.fmu') as f:
        r=trace_module.verify(pairs(f),f,1e-5,1e-4,3)
    assert len(r)==3 and all(x['within_configured_tolerance'] for x in r)


def test_trace_rejects_mismatched_transaction():
    with FormulaFMU(ROOT/'fmus'/'WingMPET.fmu') as f:
        a=pairs(f)
        a[0]['observation']['transaction_id']='wrong'
        with pytest.raises(NativeReplayRejected,match='transaction'):
            trace_module.verify(a,f,.1,1,3)


def test_trace_rejects_pilot_as_motor_measurement():
    with FormulaFMU(ROOT/'fmus'/'WingMPET.fmu') as f:
        a=pairs(f)
        a[0]['observation']['motor_state_source']='pwm'
        with pytest.raises(NativeReplayRejected,match='motor state'):
            trace_module.verify(a,f,.1,1,3)


def test_trace_requires_pairs():
    with FormulaFMU(ROOT/'fmus'/'WingMPET.fmu') as f:
        a=pairs(f,1)
        with pytest.raises(NativeReplayRejected,match='insufficient'):
            trace_module.verify(a,f,.1,1,3)
