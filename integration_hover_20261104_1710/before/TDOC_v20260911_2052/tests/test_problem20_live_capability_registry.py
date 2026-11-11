from __future__ import annotations
import csv, gzip, json
from pathlib import Path

from tddt_tdoc.rtmo.capability_registry import LiveCapabilityRegistry
from tddt_tdoc.rtmo.capability_gate import build_capability_manifest
from tddt_tdoc.rtmo.phase_manager import PHASE_ORDER

CONTROL=[p for p in PHASE_ORDER if p not in {'on_ground','touchdown'}]

def _fake_reports(root: Path, ready: bool=True):
    m=root/'offline'/'mission'; m.mkdir(parents=True,exist_ok=True)
    with (m/'PHASE_HISTORY.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=['cycle','phase']); w.writeheader()
        for i,p in enumerate(PHASE_ORDER,1): w.writerow({'cycle':i,'phase':p})
    with (m/'LAYER4_CANDIDATES.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=['cycle','verified','feasible','reference_time_s']); w.writeheader()
        for i,p in enumerate(PHASE_ORDER,1): w.writerow({'cycle':i,'verified':str(ready),'feasible':str(ready),'reference_time_s':float(i)})
    arr=[]
    for i,p in enumerate(PHASE_ORDER,1):
        arr.append({'cycle':i,'mission':{'phase':p},'layer5_decision':{'gates':{'FINAL':bool(ready)},'decision':{'approval':{'final_gate':bool(ready)}}}})
    with gzip.open(m/'DECISION_POINT_DETAILS.json.gz','wt',encoding='utf-8') as f: json.dump(arr,f)
    (m/'VTOL_PHASE_METRICS.json').write_text(json.dumps({'starts_on_ground':True,'touchdown_seen':True,'final_agl_m':0.0,'final_goal_distance_m':0.0}))

def _l4(verified: bool, n: int):
    return {'snapshot_version':f'v{n}','snapshot_hash':f'h{n}','verified_candidates':[{'candidate_id':'C1'}] if verified else [],
            'provenance':{'model_hash':'model-A','parameter_hash':'param-A'}}

def _l5(final: bool, n: int):
    return {'gates':{'FINAL':bool(final)},'decision':{'decision_id':f'D{n}','approval':{'final_gate':bool(final)}}}

def test_reports_cannot_promote_live_registry(tmp_path: Path):
    reports=tmp_path/'reports'; _fake_reports(reports,ready=True)
    reg=LiveCapabilityRegistry(tmp_path/'runtime'/'rtmo'/'capability_registry.json.gz')
    reg.reset(run_id='run1',episode_id='OFFLINE_SE-001')
    # Real registry deliberately has no control proof.
    reg.record_cycle(phase='on_ground',cycle_id=1,reference_time_s=0.0,layer4_result=_l4(False,1),layer5_result=_l5(False,1),control_effect_applied=False,boundary_evidence={'supported':True})
    reg.record_cycle(phase='touchdown',cycle_id=2,reference_time_s=1.0,layer4_result=_l4(False,2),layer5_result=_l5(False,2),control_effect_applied=False,boundary_evidence={'supported':True})
    man=build_capability_manifest(reports,layer6_forward_only=False,capability_registry_path=reg.path,require_live_registry=True,require_certificate_guard=False)
    assert man.phases['cruise']['capability'] is False
    assert 'l4_not_full_fidelity_verified_for_phase' in man.phases['cruise']['blockers']
    assert man.phases['on_ground']['capability'] is True
    assert man.source.startswith('TDOC live capability registry')

def test_live_registry_can_supply_all_base_phase_evidence(tmp_path: Path):
    reports=tmp_path/'reports'; _fake_reports(reports,ready=False)
    reg=LiveCapabilityRegistry(tmp_path/'runtime'/'rtmo'/'capability_registry.json.gz')
    reg.reset(run_id='run1',episode_id='OFFLINE_SE-001')
    cyc=0
    for p in PHASE_ORDER:
        cyc+=1
        if p in {'on_ground','touchdown'}:
            reg.record_cycle(phase=p,cycle_id=cyc,reference_time_s=float(cyc),layer4_result=_l4(False,cyc),layer5_result=_l5(False,cyc),control_effect_applied=False,boundary_evidence={'supported':True,'source':'runtime'})
        else:
            reg.record_cycle(phase=p,cycle_id=cyc,reference_time_s=float(cyc),layer4_result=_l4(True,cyc),layer5_result=_l5(True,cyc),control_effect_applied=True)
    man=build_capability_manifest(reports,layer6_forward_only=False,capability_registry_path=reg.path,require_live_registry=True,require_certificate_guard=False)
    assert man.ready_11phase is True
    assert man.phases['cruise']['live_registry_evidence']['model_hashes']==['model-A']
    assert man.phases['cruise']['l5_admissible'] is True

def test_missing_live_registry_is_fail_closed_even_when_reports_ready(tmp_path: Path):
    reports=tmp_path/'reports'; _fake_reports(reports,ready=True)
    man=build_capability_manifest(reports,layer6_forward_only=False,capability_registry_path=tmp_path/'missing.json.gz',require_live_registry=True,require_certificate_guard=False)
    assert man.ready_11phase is False
    assert all('live_capability_registry_unavailable' in man.phases[p]['blockers'] for p in PHASE_ORDER)
