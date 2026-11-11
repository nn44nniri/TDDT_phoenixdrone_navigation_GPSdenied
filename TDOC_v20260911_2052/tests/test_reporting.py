from __future__ import annotations
import csv,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; MISSION=ROOT/'reports/offline/mission'; RESET=ROOT/'reports/reset/RESET_REPORT.json'

def _reset_mode():
    # A historical reset audit may remain after a later mission run; only treat the
    # tree as currently reset when runtime is empty and generated mission reports are absent.
    # During the test suite some unit tests may create transient TDOC runtime fixtures.
    # A reset audit remains authoritative until actual mission-report evidence appears.
    mission_empty=(not MISSION.exists()) or (not any(x.is_file() for x in MISSION.rglob('*')))
    return RESET.is_file() and mission_empty
def _reset_report(): return json.loads(RESET.read_text(encoding='utf-8'))

def test_reporting_branch_exists_and_is_read_only_by_design():
    src=(ROOT/'src/tddt_tdoc/reporting.py').read_text(encoding='utf-8')
    assert 'reports/visualization' in (ROOT/'configs/tdoc.yaml').read_text(encoding='utf-8')
    assert 'report generation cannot alter orchestration state' in src and '.invoke(' not in src
    names=['MISSION_MANAGEMENT_DASHBOARD.html','MISSION_ROUTE_3D.html','WIND_CAUSALITY_REPORT.html','VTOL_PHASE_DASHBOARD.html','LAYER4_OPTIMIZATION_2D.html','LAYER4_OPTIMIZATION_3D.html','SARG_CRP_TREEMAP.html','DECISION_ACCURACY_EFFICIENCY_REPORT.html']
    if _reset_mode():
        assert _reset_report()['ok'] is True
        assert not MISSION.exists()
        assert all(not (MISSION/name).exists() for name in names)
    else:
        for name in names: assert (MISSION/name).is_file(),name

def test_generated_reports_are_nontrivial_or_intentionally_reset():
    if _reset_mode():
        r=_reset_report(); assert r['ok'] is True
        assert (ROOT/'reports').is_dir()
        files=[p.relative_to(ROOT/'reports') for p in (ROOT/'reports').rglob('*') if p.is_file()]
        # --refresh-external legitimately writes preparation evidence after reset.
        # It must not leave an old mission timeline/certificate behind.
        assert Path('reset/RESET_REPORT.json') in files
        assert all(p.parts[0] in {'reset','preparation'} for p in files)
        assert not MISSION.exists()
        return
    for name,minbytes in [('MISSION_MANAGEMENT_DASHBOARD.html',50_000),('MISSION_ROUTE_3D.html',100_000),('WIND_CAUSALITY_REPORT.html',20_000),('VTOL_PHASE_DASHBOARD.html',20_000),('LAYER4_OPTIMIZATION_2D.html',50_000),('LAYER4_OPTIMIZATION_3D.html',25_000)]:
        assert (MISSION/name).stat().st_size>minbytes,name

def test_mission_summary_matches_realized_runtime_or_reset_state():
    if _reset_mode():
        r=_reset_report(); assert r['ok'] is True
        cats={x['category'] for x in r['items'] if x['removed']}
        assert 'tdoc.runtime_and_knowledge' in cats and 'tdoc.generated_reports' in cats
        return
    s=json.loads((MISSION/'MISSION_SUMMARY.json').read_text()); n=int(s['cycles_completed']); assert n>181
    assert s['goal_reference_reached'] is True and s.get('touchdown_reached') is True and s.get('sarg_used_as_next_state') is False
    assert s['state_sources']['teacher_forced_reference_cycles']==0 and s['loop_counts']['fast']==n and s['loop_counts']['meso']>0 and s['loop_counts']['slow']>0
    assert s['flight_duration_s']==n-1
    cycles=list((ROOT/'runtime/cycles').glob('mission-offline-*.json.gz')); assert len(cycles)>=n
    assert list((ROOT/'runtime/layer5/records/chunks').glob('chunk_*.jsonl.gz'))
    assert list((ROOT/'runtime/layer5/knowledge/chunks').glob('chunk_*.jsonl.gz'))

def test_mission_timeline_parameters_or_reset_absence():
    if _reset_mode():
        assert not (MISSION/'MISSION_TIMELINE.csv').exists()
        r=_reset_report(); assert any(x['category']=='tdoc.runtime_and_knowledge' and x['removed'] for x in r['items'])
        return
    with (MISSION/'MISSION_TIMELINE.csv').open(encoding='utf-8',newline='') as f:
        r=csv.DictReader(f); row=next(r); fields=set(r.fieldnames or [])
    required={'transaction_id','reference_time_s','flight_elapsed_s','realized_x_m','realized_y_m','realized_z_msl_m','counterfactual_x_m','counterfactual_y_m','wind_counterfactual_displacement_m','agl_m','l4_best_candidate_id','l4_feasible_count','l4_advisory_applied','l5_mode','l5_final_gate','wind_speed_m_s','temperature_c','energy_remaining_wh','phase','meso_event','slow_event','sarg_used_as_next_state','teacher_forced'}
    assert required<=fields and row['transaction_id'].startswith('mission-offline-')
    assert row['teacher_forced'].lower()=='false' and row['sarg_used_as_next_state'].lower()=='false'

def test_release_gate_evidence_or_reset_audit_passes():
    if _reset_mode():
        r=_reset_report(); assert r['ok'] is True and r['dry_run'] is False
        assert 'source code' in r['policy']['preserved'] and 'external library source roots' in r['policy']['preserved']
        return
    g=json.loads((ROOT/'reports/validation/MISSION_FIX_RELEASE_GATE.json').read_text()); assert g['ok'] is True
    w=json.loads((MISSION/'WIND_CAUSALITY_METRICS.json').read_text()); assert w['wind_effect_nonzero'] is True
    v=json.loads((MISSION/'VTOL_PHASE_METRICS.json').read_text()); assert v['starts_on_ground'] and v['touchdown_seen'] and v['all_required_phases']
