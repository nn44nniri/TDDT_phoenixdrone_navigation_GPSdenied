from __future__ import annotations
import csv
import copy
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest
from tddt_tdoc.online_imu import canonical_imu, CHANNELS
from tddt_tdoc.online_mission import load_mission_prior, apply_mission_prior, bind_phase_reference
from tddt_tdoc.reporting import TDOCReportGenerator
from tddt_tdoc.storage import CompressedPersistentWorkspace


def observation(released=True):
    state={'runtime_ready':1.,'sim_time_s':10.,'latitude_deg':1.,'longitude_deg':2.,
           'altitude_msl_m':3.,'velocity_n_m_s':100.,'pitch_rad':2.,'truth_wind_n_m_s':99.}
    state.update({source:float(i) for i,source in enumerate(CHANNELS.values())})
    return {'ok':True,'released':released,'state':state}


def test_imu_allowlist_excludes_truth_and_gnss():
    obs=observation();result=canonical_imu(obs)
    assert set(result['values'])==set(CHANNELS)
    assert result['truth_position_used'] is False and result['truth_velocity_used'] is False
    assert result['gnss_used'] is False and result['measured_simulated_imu'] is True
    changed=copy.deepcopy(obs)
    for key in ('latitude_deg','longitude_deg','altitude_msl_m','velocity_n_m_s','pitch_rad','truth_wind_n_m_s'):
        changed['state'][key]=-1000000.
    assert canonical_imu(changed)==result


@pytest.mark.parametrize('key', list(CHANNELS.values())+['sim_time_s'])
def test_imu_missing_or_invalid_channel_fails_closed(key):
    obs=observation();obs['state'][key]=float('nan')
    with pytest.raises(ValueError):canonical_imu(obs)
    del obs['state'][key]
    with pytest.raises((ValueError,TypeError)):canonical_imu(obs)


def test_held_tailsitter_support_force_is_not_labeled_measured():
    q=math.sqrt(.5);result=canonical_imu(observation(False),{'qw':q,'qx':0.,'qy':q,'qz':0.})
    assert result['values']['imu_ax']==pytest.approx(9.80665)
    assert abs(result['values']['imu_az'])<1e-12
    assert result['initialization_only'] and not result['measured_simulated_imu']
    with pytest.raises(ValueError):canonical_imu(observation(False))


def test_empty_explicit_transaction_list_never_reads_historical_cycles(tmp_path,monkeypatch):
    rg=TDOCReportGenerator(SimpleNamespace(package_root=tmp_path,runtime_root=tmp_path/'runtime'))
    monkeypatch.setattr(rg,'_cycle_paths',lambda:[tmp_path/'massive-offline.json.gz'])
    def forbidden(path):raise AssertionError('Attempted to read historical cycle')
    monkeypatch.setattr('tddt_tdoc.reporting.read_json_auto',forbidden)
    with pytest.raises(FileNotFoundError,match='explicit transaction ids'):
        rg.load_cycles(transaction_ids=[])
    assert rg._invocation_events(set())==[]


def test_specific_transaction_scope_preserved(tmp_path):
    runtime=tmp_path/'runtime';(runtime/'cycles').mkdir(parents=True)
    for name in ('online','offline'):(runtime/'cycles'/f'{name}.json').write_text(json.dumps({'transaction_id':name}))
    rg=TDOCReportGenerator(SimpleNamespace(package_root=tmp_path,runtime_root=runtime))
    assert rg.load_cycles(transaction_ids=['online'])==[{'transaction_id':'online'}]


def test_restore_does_not_rebuild_committed_chunks(tmp_path,monkeypatch):
    root=tmp_path/'native';w=CompressedPersistentWorkspace(root,jsonl_chunk_bytes=128)
    p=w.working_root/'data.jsonl';p.write_text(''.join(json.dumps({'i':i})+'\n' for i in range(10)))
    w.sync();w.close()
    before={str(p.relative_to(root)):(p.stat().st_mtime_ns,hashlib.sha256(p.read_bytes()).hexdigest()) for p in root.rglob('*') if p.is_file()}
    def forbidden(*a,**kw):raise AssertionError('Restore destructively rebuilt unchanged chunks')
    monkeypatch.setattr(CompressedPersistentWorkspace,'_rebuild_jsonl_chunks',forbidden)
    w2=CompressedPersistentWorkspace(root,jsonl_chunk_bytes=128)
    after={str(p.relative_to(root)):(p.stat().st_mtime_ns,hashlib.sha256(p.read_bytes()).hexdigest()) for p in root.rglob('*') if p.is_file()}
    assert before==after
    w2.close()


def test_mission_reference_binds_absolute_objective_without_touching_state(tmp_path):
    import rasterio
    from rasterio.transform import from_origin
    from pyproj import Transformer
    origin=(-105.3,40.01);goal=(-105.299,40.011)
    tx=Transformer.from_crs('EPSG:4326','EPSG:32613',always_xy=True);e,n=tx.transform(*origin)
    maproot=tmp_path/'map';maproot.mkdir()
    with rasterio.open(maproot/'terrain_obstacles.tif','w',driver='GTiff',width=40,height=40,count=1,
                       dtype='float32',crs='EPSG:32613',transform=from_origin(e-100,n+300,10,10)) as dst:
        dst.write(np.full((1,40,40),1800,dtype=np.float32))
    report=tmp_path/'reports/offline/mission';report.mkdir(parents=True)
    with (report/'MISSION_TIMELINE.csv').open('w',newline='') as stream:
        wr=csv.writer(stream);wr.writerow(['phase','reference_x_m','reference_y_m','reference_z_msl_m'])
        wr.writerow(['on_ground',e+10,n+10,1795]);wr.writerow(['vertical_takeoff',e+10,n+10,1797]);wr.writerow(['hover',e+10,n+10,1807])
    twins=Path(__file__).resolve().parents[2]/'twins_layer2_v20260820_1233'
    cfg=SimpleNamespace(path_value=lambda key,**kw:twins if key=='TDDT_TWINS_ROOT' else maproot,get=lambda *a,**kw:kw.get('default',a[1] if len(a)>1 else None))
    prior=load_mission_prior(cfg,tmp_path/'reports',origin,goal)
    # The navigation origin is the Phoenix CG, not the terrain contact point.
    assert prior['initial_state']['pd']==pytest.approx(-1800.14)
    assert prior['initial_state']['ground_cg_height_m']==pytest.approx(0.14)
    assert prior['initial_state']['pe']==pytest.approx(e)
    assert prior['initial_state']['source'].startswith('mission_known_origin')
    req={'stage':'on_ground','mesa_inputs':{'dt':{'value':.01}},'wing_inputs':{'state_x':123},'context':{'target_enu_m':{'value':[100,0,1800]}}}
    original_mesa=copy.deepcopy(req['mesa_inputs']);original_wing=copy.deepcopy(req['wing_inputs'])
    apply_mission_prior(req,prior,prior['initial_state'])
    target=req['context']['target_projected_enu_m']
    attitude_ref=req['context']['vertical_attitude_reference']
    assert target['value']==pytest.approx([e,n,1802.14])
    assert 'phase_target_agl_m' not in req['context']
    assert req['context']['online_phase_reference_target_agl_m']['vertical_takeoff']==pytest.approx(2.14)
    assert target['frame']=='PROJECTED_ENU' and 'target_enu_m' not in req['context']
    assert attitude_ref['quaternion_wxyz']==pytest.approx(
        [prior['initial_state'][k] for k in ('qw','qx','qy','qz')])
    assert attitude_ref['initialization_reference_only'] is True
    assert attitude_ref['actuation_authority'] is False
    assert attitude_ref['truth_injection_to_mesa'] is False
    assert req['mesa_inputs']==original_mesa and req['wing_inputs']==original_wing
    bind_phase_reference(req,'hover');assert req['context']['target_projected_enu_m']['value'][2]==pytest.approx(1812.14)
    assert req['context']['phase_target_agl_m']==pytest.approx(12.14)
    assert req['context']['online_phase_reference_target_agl_m']['hover']==pytest.approx(12.14)
    assert prior['endpoint_ground_msl_m']=={'origin':1800.,'destination':1800.}
    assert prior['mission_certified'] is False


def test_elapsed_offline_seconds_rescaled_to_online_clock(tmp_path):
    import importlib.util
    from tddt_tdoc import TDOCConfig
    root=Path(__file__).parents[1]
    spec=importlib.util.spec_from_file_location("workflow_v2_budget",root/"scripts/run_training_workflows.py")
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    cfg=TDOCConfig.from_yaml(root/"configs/tdoc.yaml")
    d=tmp_path/"reports/offline/mission";d.mkdir(parents=True)
    (d/"MISSION_SUMMARY.json").write_text(json.dumps({'cycles_completed':100,'reference_cycle_count':100,
        'cycle_budget_max':100,'flight_duration_s':99.0,'mission_complete':False}))
    args=SimpleNamespace(online_max_cycles_per_episode=None,online_max_cycles_per_episode_auto=True)
    cap,meta=mod._resolve_online_episode_cycle_cap(cfg,args,tmp_path/'reports',[-105.3,40.01],[-105.28,40.025])
    assert cap==1220
    assert meta['source']=='offline_elapsed_time_rescaled'
    assert meta['offline_step_estimate_s']==1.0 and meta['online_control_period_s']==0.1
    assert not meta['mission_completion_authority']


def test_snapshot_dt_respects_operation_domain():
    import inspect
    from tddt_tdoc.adapters.layer2 import Layer2Adapter
    source=inspect.getsource(Layer2Adapter)
    assert 'else 1e-5)' in source
    assert 'interval below operation-5 domain' in source
