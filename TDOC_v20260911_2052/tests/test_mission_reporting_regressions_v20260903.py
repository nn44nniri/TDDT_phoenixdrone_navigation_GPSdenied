from __future__ import annotations
import importlib.util
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def _wf():
    p=ROOT/'scripts/run_training_workflows.py'
    spec=importlib.util.spec_from_file_location('wf_reporting_regression',p)
    m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def test_3d_hover_filters_default_zero_noise_but_keeps_informative_values():
    m=_wf()
    detail={
        'climate':{'precipitation_local':0.0,'wind_u_3dw':3.25},
        'mission':{'teacher_forced':False,'goal_distance_m':12.5},
        'sensors':{'MicroMetTwin':{'uncertainty':{'covariance':{'matrix':[[0.0,0.0],[0.0,1.0]]}}}},
    }
    h=m._decision_hover(detail)
    assert 'wind_u_3dw' in h and 'goal_distance_m' in h
    assert 'precipitation_local' not in h and 'teacher_forced' not in h
    assert 'covariance.matrix' not in h
    assert 'Cycle Detail Explorer' in h


def test_detail_timeseries_preserves_zero_fields_while_graph_series_omits_constant_zero():
    m=_wf()
    details=[{'cycle':1,'mission':{'goal_distance_m':10.0,'teacher_forced':False}}, {'cycle':2,'mission':{'goal_distance_m':5.0,'teacher_forced':False}}]
    rows=m._detail_rows(details,[0.0,1.0])
    assert rows[0]['mission.teacher_forced'] is False
    series=m._detail_numeric_series(details,'mission')
    assert 'mission.goal_distance_m' in series
    assert 'mission.teacher_forced' not in series


def test_management_dashboard_covers_3d_detail_sections_and_full_mission_axis():
    src=(ROOT/'scripts/run_training_workflows.py').read_text(encoding='utf-8')
    for token in [
        'MISSION_DETAIL_TIMESERIES.csv','3-D detail fields in 2-D','Cycle Detail Explorer',
        "'l4_mission_cost','l4_time_cost','l4_energy_cost','l4_risk_cost','l4_uncertainty_cost'",
        "range=[0,x_end]",
    ]:
        assert token in src


def test_cycle_budget_is_explicitly_a_safety_upper_bound_and_touchdown_stops_successfully():
    src=(ROOT/'scripts/run_training_workflows.py').read_text(encoding='utf-8')
    for token in ['safety_cycle_budget<=','cycle_budget_max','unused_cycle_budget','touchdown_goal_reached','cycle_budget_semantics']:
        assert token in src
    assert 'successful touchdown terminates the mission immediately' in src


def test_known_external_terrain_warning_is_filtered_only_at_tdoc_boundary_with_provenance():
    src=(ROOT/'src/tddt_tdoc/adapters/layer2.py').read_text(encoding='utf-8')
    assert 'converting a masked element to nan' in src
    assert 'terrain_truth.py' in src
    assert 'tdoc_external_warning_filter' in src
    assert 'warnings.warn_explicit' in src


def test_climate_warning_filter_suppresses_only_known_terrain_warning():
    import warnings
    from tddt_tdoc.adapters.layer2 import Layer2Adapter
    def fake(_):
        warnings.warn_explicit('Warning: converting a masked element to nan.',UserWarning,'/external/python/terrain_truth.py',177)
        warnings.warn_explicit('keep me',RuntimeWarning,'/external/python/other.py',9)
        return {'ok':1}
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter('always')
        out,filtered=Layer2Adapter._estimate_climate_with_warning_filter(fake,object())
    assert out=={'ok':1}
    assert len(filtered)==1 and filtered[0]['line']==177
    assert len(seen)==1 and str(seen[0].message)=='keep me' and seen[0].category is RuntimeWarning
