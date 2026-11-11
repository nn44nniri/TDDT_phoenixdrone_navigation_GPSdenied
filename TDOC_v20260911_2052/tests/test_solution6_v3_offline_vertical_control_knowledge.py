from pathlib import Path
import importlib.util

ROOT=Path(__file__).resolve().parents[1]

def _mod():
    spec=importlib.util.spec_from_file_location('wf_solution6_v3',ROOT/'scripts'/'run_training_workflows.py')
    m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def test_vertical_reference_boundary_dwells_only_while_real_joint_proof_is_missing():
    m=_mod()
    d=m._vertical_control_proof_dwell_decision(phase='vertical_takeoff',next_phase='hover',joint_proof_seen=False,dwell_used=0,dwell_limit=3)
    assert d['hold_reference'] is True and d['synthetic_capability'] is False
    d=m._vertical_control_proof_dwell_decision(phase='vertical_takeoff',next_phase='hover',joint_proof_seen=True,dwell_used=0,dwell_limit=3)
    assert d['hold_reference'] is False and d['reason']=='joint_control_proof_present'
    d=m._vertical_control_proof_dwell_decision(phase='hover',next_phase='transition_forward',joint_proof_seen=False,dwell_used=3,dwell_limit=3)
    assert d['hold_reference'] is False and d['reason']=='dwell_limit_exhausted_fail_closed'
    d=m._vertical_control_proof_dwell_decision(phase='cruise',next_phase='turn',joint_proof_seen=False,dwell_used=0,dwell_limit=3)
    assert d['hold_reference'] is False

def test_phase_control_coverage_requires_same_cycle_three_way_proof_for_vertical_phases():
    m=_mod(); rows=[]
    for phase in m.CONTROL_REQUIRED_PHASES:
        rows.append({'phase':phase,'l4_candidate_verified':True,'l5_final_gate':True,'layer2_control_effect_applied':True})
    cov=m._phase_control_coverage(rows)
    assert cov['all_required_control_phases'] is True
    for p in ('vertical_takeoff','hover','vertical_landing'):
        assert cov['coverage'][p]['joint_proof_cycles']==1
    rows[0]['layer2_control_effect_applied']=False
    cov=m._phase_control_coverage(rows)
    assert cov['coverage']['vertical_takeoff']['control_capable'] is False
    assert 'vertical_takeoff' in cov['missing_control_phases']

def test_priority6_dwell_does_not_change_single_episode_or_control_authority_contract():
    src=(ROOT/'scripts'/'run_training_workflows.py').read_text()
    assert "single-episode" in src
    assert "synthetic_capability':False" in src
    assert "L4 verified + L5 FINAL + realized Layer2/WingMPET control effect" in src
    assert "offline-vertical-proof-dwell-cycles" in src

def test_terminal_reposition_pre_spools_with_landing_physics_collective_not_legacy_low_throttle():
    m=_mod()
    p=object.__new__(m.OfflineWingMPETPlant)
    p._vtol_reference={'vertical_takeoff':0.82,'hover':0.82,'vertical_landing':0.81}
    p._vtol_reference_source='test_physics_seed'
    a=p._actuator_ref('approach',substate='terminal_reposition')
    assert a['left_motor_cmd']==0.81 and a['right_motor_cmd']==0.81
    assert a['left_elevon_rad']==0.0 and a['right_elevon_rad']==0.0
