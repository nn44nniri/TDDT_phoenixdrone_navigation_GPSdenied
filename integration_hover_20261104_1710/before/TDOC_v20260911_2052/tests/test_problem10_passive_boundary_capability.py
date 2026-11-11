from __future__ import annotations
import csv, json
from pathlib import Path
from tddt_tdoc.rtmo.capability_gate import build_capability_manifest

PHASES=['on_ground','vertical_takeoff','hover','transition_forward','climb','cruise','turn','approach','transition_back','vertical_landing','touchdown']

def _fixture(tmp_path:Path, *, good_ground=True, good_touchdown=True):
    m=tmp_path/'offline'/'mission'; m.mkdir(parents=True)
    with (m/'PHASE_HISTORY.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=['cycle','phase']); w.writeheader()
        w.writerow({'cycle':1,'phase':'on_ground' if good_ground else 'cruise'})
        w.writerow({'cycle':2,'phase':'cruise'})
        w.writerow({'cycle':3,'phase':'touchdown' if good_touchdown else 'approach'})
    (m/'VTOL_PHASE_METRICS.json').write_text(json.dumps({
        'starts_on_ground':good_ground,'touchdown_seen':good_touchdown,
        'final_agl_m':0.0 if good_touchdown else 5.0,
        'final_goal_distance_m':0.0 if good_touchdown else 100.0,
    }))
    # Empty control evidence is intentional: passive boundaries must not need it.
    with (m/'LAYER4_CANDIDATES.csv').open('w',newline='',encoding='utf-8') as f:
        csv.DictWriter(f,fieldnames=['cycle','feasible','verified']).writeheader()
    return tmp_path

def test_passive_boundaries_do_not_require_l4_l5_control_evidence(tmp_path):
    root=_fixture(tmp_path)
    cap=build_capability_manifest(root,layer6_forward_only=True,execution_overrides={'on_ground':True,'touchdown':True})
    for p in ('on_ground','touchdown'):
        x=cap.phases[p]
        assert x['semantic_role']=='passive_boundary'
        assert x['requires_l4_l5_control_evidence'] is False
        assert x['boundary_state_supported'] is True
        assert x['l4_verified'] is False and x['l5_admissible'] is False
        assert x['capability'] is True and x['blockers']==[]

def test_passive_boundaries_fail_closed_without_boundary_evidence(tmp_path):
    root=_fixture(tmp_path,good_ground=False,good_touchdown=False)
    cap=build_capability_manifest(root,layer6_forward_only=True,execution_overrides={'on_ground':True,'touchdown':True})
    for p in ('on_ground','touchdown'):
        x=cap.phases[p]
        assert x['capability'] is False
        assert 'passive_boundary_evidence_not_satisfied' in x['blockers']
        assert 'l4_not_full_fidelity_verified_for_phase' not in x['blockers']
        assert 'l5_not_final_admissible_for_phase' not in x['blockers']

def test_controlled_phase_semantics_unchanged(tmp_path):
    root=_fixture(tmp_path)
    cap=build_capability_manifest(root,layer6_forward_only=True,execution_overrides={'on_ground':True,'touchdown':True})
    x=cap.phases['cruise']
    assert x['semantic_role']=='controlled_phase'
    assert x['requires_l4_l5_control_evidence'] is True
    assert 'l4_not_full_fidelity_verified_for_phase' in x['blockers']
    assert 'l5_not_final_admissible_for_phase' in x['blockers']
