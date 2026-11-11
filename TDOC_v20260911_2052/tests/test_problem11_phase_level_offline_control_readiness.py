from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
spec=spec_from_file_location('wf', ROOT/'scripts'/'run_training_workflows.py')
wf=module_from_spec(spec); spec.loader.exec_module(wf)

def row(phase, ok=False):
    return {'phase':phase,'l4_candidate_verified':ok,'l5_final_gate':ok,'layer2_control_effect_applied':ok}

def test_global_success_in_three_phases_is_not_control_ready():
    rows=[]
    for p in wf.CONTROL_REQUIRED_PHASES:
        rows.append(row(p, p in {'transition_forward','climb','cruise'}))
    cov=wf._phase_control_coverage(rows)
    assert cov['all_required_control_phases'] is False
    assert set(cov['missing_control_phases']) == set(wf.CONTROL_REQUIRED_PHASES)-{'transition_forward','climb','cruise'}

def test_all_control_phases_require_joint_same_cycle_proof():
    rows=[row(p,True) for p in wf.CONTROL_REQUIRED_PHASES]
    cov=wf._phase_control_coverage(rows)
    assert cov['all_required_control_phases'] is True
    assert all(v['joint_proof_cycles']==1 for v in cov['coverage'].values())

def test_split_evidence_does_not_count_as_joint_control_proof():
    p='turn'
    rows=[{'phase':p,'l4_candidate_verified':True,'l5_final_gate':False,'layer2_control_effect_applied':False},
          {'phase':p,'l4_candidate_verified':False,'l5_final_gate':True,'layer2_control_effect_applied':True}]
    rows += [row(x,True) for x in wf.CONTROL_REQUIRED_PHASES if x!=p]
    cov=wf._phase_control_coverage(rows)
    assert cov['coverage'][p]['verified_cycles']==1
    assert cov['coverage'][p]['l5_final_cycles']==1
    assert cov['coverage'][p]['joint_proof_cycles']==0
    assert cov['all_required_control_phases'] is False
