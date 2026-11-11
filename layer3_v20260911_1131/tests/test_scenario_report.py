from __future__ import annotations
import json
from pathlib import Path

from layer3.astar_guidance import RasterAStarPlanner
from layer3.scenario_report import generate_scenario_reports, load_region_definition, prepare_region_scenario_request

ROOT=Path(__file__).resolve().parents[1]


def test_scenario_runner_astar_dependencies_and_docs_are_packaged():
    assert (ROOT/'run_scenario_report.sh').is_file()
    assert (ROOT/'examples/run_layer3_scenario.py').is_file()
    assert (ROOT/'src/layer3/astar_guidance.py').is_file()
    assert (ROOT/'src/layer3/objectives.py').is_file()
    assert (ROOT/'docs/SCENARIO_REPORT.md').is_file()
    assert 'plotly>=6.0' in (ROOT/'pyproject.toml').read_text(encoding='utf-8')


def test_region_definition_and_astar_use_authoritative_boulder_raster(cfg_factory):
    cfg=cfg_factory(); base=json.loads((ROOT/'examples/runtime_request.json').read_text())
    prepared,region=prepare_region_scenario_request(cfg,base)
    assert region['processing_crs']=='EPSG:32613' and region['computed_direct_distance_m']>2000
    planner=RasterAStarPlanner(cfg,origin_projected_m=tuple(region['start_projected_m']))
    path=planner.plan((0.,0.),(region['target_enu_m'][0],region['target_enu_m'][1]),start_altitude_msl_m=region['scenario_start_altitude_msl_m'])
    assert path.path_length_m>region['computed_direct_distance_m']
    assert path.min_terrain_clearance_m>=cfg.planner.min_terrain_clearance_m-1e-6
    assert len(path.cells)>20 and path.expanded_nodes>0
    assert prepared['context']['scenario_destination_enu_m']['frame']=='ENU'


def test_real_astar_six_tick_scenario_generates_loops_objectives_and_reports(cfg_factory,tmp_path):
    cfg=cfg_factory(meso=2,slow=2); cfg.planner.lookahead_m=450.0; cfg.planner.max_steps=6
    req=json.loads((ROOT/'examples/runtime_request.json').read_text())
    summary,art=generate_scenario_reports(cfg,req,steps=6,output_dir=tmp_path/'scenario',terrain_grid_n=32)
    assert summary['ok']
    assert summary['runtime_execution']['fast_steps']>=4
    assert summary['runtime_execution']['meso_events']>=2
    assert summary['runtime_execution']['slow_events']>=1
    assert summary['runtime_execution']['configured_fast_per_meso']==2
    assert summary['planner']['algorithm']=='8-neighbour A*'
    assert summary['planner']['replan_count']>=2
    assert summary['objective_attainment']['fast_final_pct'] is not None
    assert summary['mission']['energy_total_j']>0 and summary['mission']['flight_time_s']>0
    assert summary['tdoc']['final_failures']==0
    for p in (art.result_json,art.summary_json,art.timeseries_csv,art.report_3d_html,art.report_2d_html): assert p.is_file() and p.stat().st_size>0
    h3=art.report_3d_html.read_text(encoding='utf-8').lower(); h2=art.report_2d_html.read_text(encoding='utf-8').lower()
    assert 'a* plan' in h3 and 'terrain_obstacles.tif' in h3 and 'micromet actual wind' in h3
    assert 'objective attainment' in h2 and 'a* replan' in h2 and 'mission energy' in h2

def test_astar_runtime_feedback_expands_uncertainty_buffer_and_preserves_route(cfg_factory):
    cfg=cfg_factory(); base=json.loads((ROOT/'examples/runtime_request.json').read_text())
    _,region=prepare_region_scenario_request(cfg,base); planner=RasterAStarPlanner(cfg,origin_projected_m=tuple(region['start_projected_m']))
    goal=(region['target_enu_m'][0],region['target_enu_m'][1]); h=region['scenario_start_altitude_msl_m']
    p0=planner.plan((0.,0.),goal,start_altitude_msl_m=h,runtime_feedback={'risk':0.0,'mesa_uncertainty':0.0})
    p1=planner.plan((0.,0.),goal,start_altitude_msl_m=h,runtime_feedback={'risk':1.0,'mesa_uncertainty':0.8,'wind_e_m_s':4.0,'wind_n_m_s':-2.0})
    assert p1.planner_feedback['obstacle_buffer_m']>p0.planner_feedback['obstacle_buffer_m']
    assert p1.path_length_m>0 and p1.cost>0
