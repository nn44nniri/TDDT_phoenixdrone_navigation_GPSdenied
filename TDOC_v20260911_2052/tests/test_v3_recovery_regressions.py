from pathlib import Path
import importlib.util
import json
import math
import numpy as np
import pandas as pd
import pytest
from layer4.optimizer import Layer4MultiObjectiveOptimizer
from tddt_tdoc.phoenix_frames import authoritative_or_fallback_attitude, euler_quaternion
from tddt_tdoc.release_gate import write_mission_release_gate, _truth

ROOT=Path(__file__).resolve().parents[2]

def test_spsa_directions_span_full_space_and_are_reproducible():
    d=np.asarray([Layer4MultiObjectiveOptimizer._direction((8,),i) for i in range(64)])
    assert np.linalg.matrix_rank(d)==8
    assert set(d.flat)=={-1.,1.}
    assert np.array_equal(d[9],Layer4MultiObjectiveOptimizer._direction((8,),9))
    with pytest.raises(ValueError): Layer4MultiObjectiveOptimizer._direction((8,),-1)

def test_spsa_can_move_in_direction_orthogonal_to_legacy_rank_one():
    target=np.array([1.,1.,0.,0.]); start=np.zeros(4)
    opt=Layer4MultiObjectiveOptimizer({'optimization.iterations':160,'optimization.step':0.4})
    evaluate=lambda u:{'augmented_cost':float(np.sum((u-target)**2))}
    u,e,_=opt.optimize(start,lambda u:np.clip(u,-2.,2.),evaluate)
    assert e['augmented_cost']<0.05
    assert np.linalg.norm(u-target)<0.25

@pytest.mark.parametrize('phase',['hover','transition_forward','climb','vertical_landing'])
def test_real_quaternion_never_replaced_with_phase_template(phase):
    q=euler_quaternion(.08,.12,.4)
    state=dict(zip(('qw','qx','qy','qz'),q))
    result=authoritative_or_fallback_attitude(state,phase)
    assert np.allclose([result[k] for k in state],q)
    assert result['basis']=='authoritative_physical_state_quaternion'

@pytest.mark.parametrize('q',[{'qw':1.},{'qw':0.,'qx':0.,'qy':0.,'qz':0.}, {'qw':math.nan,'qx':0.,'qy':0.,'qz':0.}])
def test_partial_or_invalid_quaternion_is_rejected(q):
    with pytest.raises(ValueError): authoritative_or_fallback_attitude(q,'hover')

def test_missing_attitude_uses_documented_fallback():
    result=authoritative_or_fallback_attitude({},'hover')
    assert result['pitch_rad']==pytest.approx(math.pi/2)

def test_signed_pressure_residual_not_absolute_pressure():
    path=ROOT/'twins_layer2_v20260820_1233/python/build_tddt_layer2_tep.py'
    spec=importlib.util.spec_from_file_location('tep_v3_test',path)
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    assert mod.physical_check({'pressure_anchor_residual':-12.})[0]
    assert not mod.physical_check({'pressure':-12.})[0]
    assert not mod.physical_check({'pressure_residual_variance':-12.})[0]

def test_release_gate_missing_evidence_produces_negative_report(tmp_path):
    report=write_mission_release_gate(tmp_path)
    assert not report['ok']
    assert not report['online_actuation_authority']
    assert (tmp_path/'reports/validation/MISSION_FIX_RELEASE_GATE.json').is_file()

def test_boolean_strings_do_not_promote_false():
    assert not _truth('False'); assert not _truth('0'); assert _truth('True')
    assert not _truth(float('nan'))

def test_sarg_next_point_safety_does_not_bypass_envelope_or_mechanics(monkeypatch):
    import sys
    monkeypatch.syspath_prepend(str(ROOT/'SARG_v20260821_0945'))
    from sarg_builder.fmi_execution import StrictFMIEngine
    result={'envelope':{'flight_envelope_valid':1.},'safety':{'safety_valid':0.},'mechanical':{'mechanical_constraints_valid':1.}}
    exact={'safety_valid':1.}
    assert not StrictFMIEngine.acceptance_flags(result)
    assert StrictFMIEngine.acceptance_flags(result,exact)
    result['envelope']['flight_envelope_valid']=0.
    assert not StrictFMIEngine.acceptance_flags(result,exact)
    result['envelope']['flight_envelope_valid']=1.;result['mechanical']['mechanical_constraints_valid']=0.
    assert not StrictFMIEngine.acceptance_flags(result,exact)

def _mission_fixture(root):
    phases=['on_ground','vertical_takeoff','hover','transition_forward','climb','cruise','turn','approach','transition_back','vertical_landing','touchdown']
    data=[]
    for i,p in enumerate(phases):
        data.append(dict(phase=p,agl_m=0 if i in (0,10) else 50,goal_distance_m=0 if i==10 else 100,
                         realized_x_m=i*10,realized_y_m=2 if i==5 else 0,teacher_forced='False',sarg_used_as_next_state='False',
                         wind_counterfactual_displacement_m=i,l4_advisory_applied='True',fast_event=True,meso_event=True,slow_event=True))
    d=root/'reports/offline/mission';d.mkdir(parents=True)
    pd.DataFrame(data).to_csv(d/'MISSION_TIMELINE.csv',index=False)
    summary={'mission_complete':True,'cycles_completed':11,'offline_control_ready':True,
             'knowledge_quality':{'phase_control_coverage':{'all_required_control_phases':True,'missing_control_phases':[]}}}
    (d/'MISSION_SUMMARY.json').write_text(json.dumps(summary));return d,summary

def test_release_gate_binds_complete_evidence_and_rejects_missing_control(tmp_path):
    d,s=_mission_fixture(tmp_path)
    good=write_mission_release_gate(tmp_path)
    assert good['ok']
    s['offline_control_ready']=False
    (d/'MISSION_SUMMARY.json').write_text(json.dumps(s))
    bad=write_mission_release_gate(tmp_path)
    assert not bad['ok'] and 'control_ready' in bad['failed_checks']
    assert good['source_files']['summary']['sha256'] != bad['source_files']['summary']['sha256']

def test_release_gate_detects_summary_row_count_mismatch(tmp_path):
    d,s=_mission_fixture(tmp_path);s['cycles_completed']=12
    (d/'MISSION_SUMMARY.json').write_text(json.dumps(s))
    gate=write_mission_release_gate(tmp_path)
    assert not gate['ok'] and 'summary_matches_timeline' in gate['failed_checks']


def test_release_gate_nonfinite_measurement_writes_negative_standard_json(tmp_path):
    d,_=_mission_fixture(tmp_path)
    df=pd.read_csv(d/'MISSION_TIMELINE.csv');df.loc[0,'agl_m']=float('nan')
    df.to_csv(d/'MISSION_TIMELINE.csv',index=False)
    gate=write_mission_release_gate(tmp_path)
    assert not gate['ok']
    assert 'non_finite_mission_measurement:agl_m' in gate['error']
    text=(tmp_path/'reports/validation/MISSION_FIX_RELEASE_GATE.json').read_text()
    assert 'NaN' not in text and 'Infinity' not in text
