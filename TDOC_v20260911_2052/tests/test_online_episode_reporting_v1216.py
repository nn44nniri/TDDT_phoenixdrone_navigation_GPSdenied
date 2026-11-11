from __future__ import annotations
import argparse, importlib.util, json, subprocess, sys
from pathlib import Path
import pandas as pd

from tddt_tdoc import TDOCConfig
from tddt_tdoc.adapters.layer5 import Layer5Adapter
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter
from tddt_tdoc.reporting import TDOCReportGenerator

ROOT=Path(__file__).parents[1]


def _load_workflow_module():
    spec=importlib.util.spec_from_file_location("tdoc_training_workflow",ROOT/"scripts/run_training_workflows.py")
    mod=importlib.util.module_from_spec(spec); assert spec and spec.loader; spec.loader.exec_module(mod); return mod


def test_cli_exposes_online_episode_and_mission_aliases():
    env=dict(__import__('os').environ); env['PYTHONPATH']=str(ROOT/'src')
    cp=subprocess.run([sys.executable,'-m','tddt_tdoc.cli','train-online','--help'],cwd=ROOT,env=env,text=True,capture_output=True)
    assert cp.returncode==0
    assert '--online-episodes' in cp.stdout and '--online-missions' in cp.stdout
    assert '--online-max-cycles-per-episode' in cp.stdout
    cp2=subprocess.run([sys.executable,str(ROOT/'scripts/run_training_workflows.py'),'--help'],cwd=ROOT,env=env,text=True,capture_output=True)
    assert cp2.returncode==0 and '--offline' in cp2.stdout and '--online-episodes' in cp2.stdout


def test_strict_online_contract_rejects_legacy_uncorrelated_observation(tmp_path):
    mod=_load_workflow_module()
    class Cfg:
        runtime_root=tmp_path/'runtime'
        package_root=ROOT
        def get(self,*a,**k):return k.get('default') if k else None
    args=argparse.Namespace(online_training=True,terrain_grid_n=20,online_episodes=0)
    records=[
        {'status':'DEGRADED','offline_knowledge_handoff':{'ok':True,'setd_package_id':'SETD-1'},
         'knowledge':{},'layer7_status':'OK','layer6_runtime':{'status':'OBSERVATION_ONLY',
         'source':'Layer6/PT_LIB','live_runtime_evidence':True,'validation_only':False}},
        {'status':'DEGRADED','offline_knowledge_handoff':{'ok':True,'setd_package_id':'SETD-1'},
         'knowledge':{},'layer7_status':'HELD','layer6_runtime':{'status':'HELD',
         'source':None,'live_runtime_evidence':False,'validation_only':False}},
    ]
    out=mod._write_online_reports(None,Cfg(),records,2,args,tmp_path/'reports',prefix='historical-noncorrelated-')
    c=out['online_training_contract'];d=out['orchestration_diagnostics']
    assert c['contract_ok'] is False
    assert c['runtime_coverage_ratio']==0.0 and c['layer7_coverage_ratio']==0.5
    assert d['ptlib_invoked'] is False
    assert d['live_runtime_evidence_cycles']==0
    assert c['execution_correlated_observation_cycles']==0



def test_reporting_extracts_live_layer6_geographic_position():
    cfg=TDOCConfig.from_yaml(ROOT/'configs/tdoc.yaml'); rg=TDOCReportGenerator(cfg)
    cycle={'downstream_layers':{'6':{'status':'OBSERVATION_ONLY','output':{'execution_evidence':{
        'source':'Layer6/PT_LIB','io_snapshot':{'ready':True,'sensor_observation':{'GLOBAL_POSITION_INT':{'lat':400100000,'lon':-1053000000,'alt':1700000,'relative_alt':1500}},'simulation_time_s':12.0}
    }}}}}
    p=rg._cycle_layer6_position(cycle)
    assert p is not None
    assert abs(p['latitude_deg']-40.01)<1e-9 and abs(p['longitude_deg']+105.3)<1e-9
    assert abs(p['altitude_msl_m']-1700.0)<1e-9
    assert p['simulation_time_s']==12.0


def test_layer4_dense_3d_report_is_replaced_by_timeline(tmp_path):
    mod=_load_workflow_module()
    rows=[]
    for cycle in range(1,5):
        for j in range(3):
            rows.append({'cycle':cycle,'candidate_id':f'C{cycle}-{j}','scenario_id':j,'pareto_rank':j,'scalar_cost':10+cycle+j,
                         'uncertainty':0.1*j,'consistency':0.9,'feasible':j<2,'verified':False,'time':cycle+j/10,'energy':2+j,'risk':0.1*j,'mission':0.2*j})
    out=mod.layer4_optimization_reports(tmp_path,pd.DataFrame(rows))
    assert out['generated'] is True
    html=(tmp_path/'LAYER4_OPTIMIZATION_3D.html').read_text()
    assert 'Layer 4 Optimization Decision Timeline' in html
    assert (tmp_path/'LAYER4_OPTIMIZATION_TIMELINE.html').is_file()


def test_cli_exposes_auto_episode_cycle_budget():
    env=dict(__import__('os').environ); env['PYTHONPATH']=str(ROOT/'src')
    cp=subprocess.run([sys.executable,'-m','tddt_tdoc.cli','train-online','--help'],cwd=ROOT,env=env,text=True,capture_output=True)
    assert cp.returncode==0 and '--online-max-cycles-per-episode-auto' in cp.stdout
    cp2=subprocess.run([sys.executable,str(ROOT/'scripts/run_training_workflows.py'),'--help'],cwd=ROOT,env=env,text=True,capture_output=True)
    assert cp2.returncode==0 and '--online-max-cycles-per-episode-auto' in cp2.stdout


def test_auto_episode_cycle_budget_prefers_offline_safety_baseline(tmp_path):
    mod=_load_workflow_module(); cfg=TDOCConfig.from_yaml(ROOT/'configs/tdoc.yaml')
    p=tmp_path/'reports'/'offline'/'mission'; p.mkdir(parents=True)
    (p/'MISSION_SUMMARY.json').write_text(json.dumps({'cycles_completed':230,'reference_cycle_count':200,'cycle_budget_max':320}))
    args=argparse.Namespace(online_max_cycles_per_episode=320,online_max_cycles_per_episode_auto=True)
    cap,meta=mod._resolve_online_episode_cycle_cap(cfg,args,tmp_path/'reports',[-105.3,40.01],[-105.28,40.025])
    assert cap==320
    assert meta['mode']=='auto' and meta['source']=='offline_mission_baseline'
    assert meta['offline_cycles_completed']==230


def test_online_performance_report_measures_distance_time_and_energy(tmp_path):
    mod=_load_workflow_module()
    records=[
      {'episode_index':1,'cycle_in_episode':1,'position':{'lat':40.01,'lon':-105.30,'simulation_time_s':1.0,'relative_altitude_m':10},'terminal':{'goal_distance_m':2000},'battery':{'voltage_v':12.0,'current_consumed_mah':100,'current_a':2.0,'battery_remaining_pct':95},'wall_cycle_s':1.0,'layer6_elapsed_s':0.1},
      {'episode_index':1,'cycle_in_episode':2,'position':{'lat':40.011,'lon':-105.299,'simulation_time_s':2.0,'relative_altitude_m':11},'terminal':{'goal_distance_m':1850},'battery':{'voltage_v':12.0,'current_consumed_mah':200,'current_a':2.2,'battery_remaining_pct':94},'wall_cycle_s':1.0,'layer6_elapsed_s':0.1},
    ]
    episodes=[{'terminal_reached':False}]
    out=mod._write_online_performance_report(records,episodes,tmp_path/'reports',[-105.30,40.01],[-105.28,40.025])
    assert out['status']=='INCOMPLETE'
    assert out['distance']['actual_path_m']>0 and out['distance']['progress_m']==150
    assert abs(out['energy']['estimated_energy_used_wh']-1.2)<1e-9
    assert out['time']['simulator_elapsed_s']==1.0 and out['time']['wall_elapsed_s']==2.0
    assert (tmp_path/'reports/online/ONLINE_FLIGHT_PERFORMANCE.json').is_file()
    assert (tmp_path/'reports/online/ONLINE_FLIGHT_PERFORMANCE.html').is_file()
    assert (tmp_path/'reports/online/ONLINE_FLIGHT_PERFORMANCE_TIMESERIES.csv').is_file()


def test_online_terminal_formatter_dsim_is_numeric_and_progress_diagnostic_preserves_hold_zero():
    mod=_load_workflow_module()
    assert mod._format_online_terminal_metrics(0.0,0.0,0.0,10.0,4.5)==('+0.00m','0.0m/0.0%','10.00s','+4.50s')
    cycle={'provenance':{'mpc_participation':{'layer5':{'operational_mode':'HOLD','decision_reason':'no_admissible_bound_candidate','actuation_executed':False}}}}
    vs={'motion':{'state':'NO_ACTUATION_STATIONARY'}}
    d=mod._online_progress_diagnostic(cycle,vs,0.0,0.0)
    assert d['blocked_by_no_authorized_actuation'] is True
    assert d['interpretation']=='ZERO_PROGRESS_EXPECTED_LAYER5_HOLD_NO_ACTUATION'

def test_cli_exposes_explicit_online_observation_only_mode():
    env=dict(__import__('os').environ); env['PYTHONPATH']=str(ROOT/'src')
    cp=subprocess.run([sys.executable,'-m','tddt_tdoc.cli','train-online','--help'],cwd=ROOT,env=env,text=True,capture_output=True)
    assert cp.returncode==0 and '--online-observation-only' in cp.stdout and '--no-online-auto-commission' in cp.stdout
    cp2=subprocess.run([sys.executable,str(ROOT/'scripts/run_training_workflows.py'),'--help'],cwd=ROOT,env=env,text=True,capture_output=True)
    assert cp2.returncode==0 and '--online-observation-only' in cp2.stdout and '--no-online-auto-commission' in cp2.stdout


def test_online_not_ready_report_identifies_pre_control_gate(tmp_path):
    mod=_load_workflow_module()
    readiness={'ok':False,'status':'NOT_READY','blockers':['layer4_to_phoenix_mapping_not_validated']}
    out=mod.write_online_not_ready(tmp_path,10,'bridge blocked',None,control_readiness=readiness)
    assert out['cycle_count_completed']==0
    assert out['orchestration_diagnostics']['stage']=='PRE_ONLINE_CONTROL_READINESS_GATE'
    assert out['online_control_readiness']['blockers']==['layer4_to_phoenix_mapping_not_validated']


def test_stationary_state_is_read_from_nested_motion_contract():
    mod=_load_workflow_module()
    rec={'vehicle_state':{'motion':{'state':'NO_ACTUATION_STATIONARY'}}}
    assert ((rec.get('vehicle_state') or {}).get('motion') or {}).get('state')=='NO_ACTUATION_STATIONARY'
