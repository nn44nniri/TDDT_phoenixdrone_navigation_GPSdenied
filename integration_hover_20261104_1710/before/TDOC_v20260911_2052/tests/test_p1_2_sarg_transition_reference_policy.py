from __future__ import annotations
import importlib.util
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('wf_p1_2', ROOT/'scripts'/'run_training_workflows.py')
wf=importlib.util.module_from_spec(spec); spec.loader.exec_module(wf)


def _write_dataset(tmp_path: Path, invalid_transition: bool):
    rows=[]
    for phase in range(1,7):
        invalid = invalid_transition and phase == 1
        rows.append({
            'scenario_id':3,'regime_id':3,'crp_id':4,'phase_id':phase,
            'energy_used_wh':1.0,'flight_time_s':10.0,'min_obstacle_clearance_m':50.0,
            'min_ground_clearance_m':30.0,'flight_ok':0 if invalid else 1,'nav_ok':1,
            'record_status':3 if invalid else 0,'wing_status':5 if invalid else 0,
            'mesa_status':0,'phase_used':1,
        })
    pd.DataFrame(rows).to_csv(tmp_path/'sarg_records.csv',index=False)
    rd=tmp_path/'routes'; rd.mkdir()
    pd.DataFrame({
        'flight_valid':[1.0,1.0], 'navigation_valid':[1.0,1.0],
        'clearance_m':[30.0,30.0], 'goal_distance_m':[100.0,0.0],
    }).to_csv(rd/'S03_C004.csv',index=False)


def test_invalid_transition_sarg_route_is_reference_only(tmp_path):
    _write_dataset(tmp_path, True)
    out=wf.route_candidates(tmp_path,3,100.0)
    assert len(out)==1
    r=out[0]
    assert r['transition_reference_eligible'] is False
    assert r['strict_admissible'] is False
    assert r['control_readiness_eligible'] is False
    assert r['admission_status']=='DEGRADED_REFERENCE_ONLY'
    assert 'transition_sarg_reference_not_flight_navigation_admissible' in r['admission_reasons']


def test_valid_transition_sarg_route_can_remain_strict(tmp_path):
    _write_dataset(tmp_path, False)
    out=wf.route_candidates(tmp_path,3,100.0)
    r=out[0]
    assert r['transition_reference_eligible'] is True
    assert r['strict_admissible'] is True
    assert r['control_readiness_eligible'] is True
