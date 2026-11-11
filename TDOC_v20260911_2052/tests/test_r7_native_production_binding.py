"""Composition/current-context boundaries. Encoded UNIT telemetry is not Native acceptance."""
from copy import deepcopy
from types import SimpleNamespace
from pathlib import Path
import time
import pytest

from layer4.config import Layer4Config
from layer4.snapshot import Layer3SnapshotAdapter
from layer6.status import project_status, SCHEMA
from layer6.telemetry_source import native_source_contract
from tddt_tdoc.native_production import NativeProductionLane
from tddt_tdoc.orchestrator import TDOCOrchestrator
from test_r5_04_gui_status import make_link


def inputs():
    native=Path(__file__).resolve().parents[2]/'layer6_v20261007_1032'
    link=make_link(native_source_contract(native));p=project_status(link,include_imu_samples=True)
    status={**p,'schema':SCHEMA,'schema_version':1,'ok':True,'mavlink_connected':p['connected'],
        'monotonic_domain_id':p['domain'],'telemetry_metadata':p['metadata'],
        'target_system':1,'target_component':1,'mode':'QHOVER','armed':False}
    s={'policy_version':'unit','reference_time_s':1.,'std':{},'topology':{},'weights':{},
        'couplings':{},'center_of_consistency':{},'uncertainty_td':{},'guidance':{},'constraints':{},
        'context':{},'provenance':{'snapshot_kind':'atomic_layer3_network_snapshot','fast_index':1}}
    ingress={'navigation_authority':'MESA_STD','measured_simulated_imu':True,'plant_time_s':1.,
             'truth_position_used':False,'truth_velocity_used':False,'gnss_used':False}
    packets={'MESA_NAV':{'provenance':{'online_imu_ingress':ingress},
        'context':{'online_state_ingress':{'ptlib_ingress':{'gui_state':deepcopy(status)}}}}}
    lane=NativeProductionLane.__new__(NativeProductionLane)
    lane.guard=SimpleNamespace(check=lambda:{'mission_origin':{},'binding_hash':'b','model_id':'m','forcing_file_sha256':'f'})
    config=Layer4Config.from_yaml(Path(__file__).resolve().parents[2]/'layer4_v20260907_0608/configs/configuration_boulder.yaml')
    lane.adapters={6:SimpleNamespace(_status=lambda:deepcopy(status)),4:SimpleNamespace(engine=SimpleNamespace(config=config))}
    return lane,s,packets,{'learning_mode':'ONLINE_JSBSIM','stage':'on_ground'},status


def test_current_seal_retains_mesa_state_and_original_source_clock_and_hashes_new_context():
    lane,s,p,r,status=inputs();out=lane.seal_snapshot(s,p,r)
    assert out['std']==s['std'] and out['uncertainty_td']==s['uncertainty_td']
    assert out['context']['native_current_context']['MESA_source_time_s']==1.
    assert out['context']['snapshot_epoch_s'] <= time.time()
    assert out['context']['truth_injection_to_mesa'] is False
    assert out['snapshot_hash']==Layer3SnapshotAdapter(lane.adapters[4].engine.config).canonical_hash(out)
    assert 'snapshot_epoch_s' not in s['context']


@pytest.mark.parametrize('change',['stale','truth','gnss','unmeasured','source','mode','ARM','phase','fcu_time'])
def test_current_snapshot_seal_rejects_stale_truth_or_domain_mismatch(change):
    lane,s,p,r,status=inputs();ingress=p['MESA_NAV']['provenance']['online_imu_ingress']
    if change=='stale':
        for m in status['telemetry_metadata'].values():m['freshness']['evaluated_monotonic']-=100
    if change=='truth':ingress['truth_position_used']=True
    if change=='gnss':ingress['gnss_used']=True
    if change=='unmeasured':ingress['measured_simulated_imu']=False
    if change=='source':s['reference_time_s']=2.
    if change=='mode':status['mode']='FBWA'
    if change=='ARM':status['armed']=True
    if change=='phase':r['stage']='hover'
    if change=='fcu_time':ingress['plant_time_s']=s['reference_time_s']=.5
    with pytest.raises(ValueError):lane.seal_snapshot(s,p,r)


def test_composition_rejects_request_dict_as_provider_before_any_model_or_adapter_access():
    with pytest.raises(ValueError,match='protocol incomplete'):
        NativeProductionLane(None,{}, {'qualified':True},None,lambda *args:None)


def test_composition_cannot_replace_a_bound_model_silently():
    obj=TDOCOrchestrator.__new__(TDOCOrchestrator);obj.native_prediction_lane=object()
    with pytest.raises(ValueError,match='no silent model replacement'):obj.bind_native_prediction(None,None,None)


def test_no_safety_envelope_is_not_repaired_using_a_fixture():
    lane,s,p,r,status=inputs()
    s['context']['native_current_context']={'schema':'TDOC-Native-current-context-v1'}
    with pytest.raises(ValueError,match='mission-derived Native safety envelope unavailable'):lane.source(s,r)


def test_fast_safe_profile_parser_preserves_real_model_semantics_and_fallback(monkeypatch):
    import yaml
    from tddt_tdoc.model_compatibility import read_safe_yaml
    root=Path(__file__).resolve().parents[2]
    paths=[root/'twins_layer2_v20260820_1233/config/phoenix_drone_v3.yaml',
           root/'layer5_v20260911_1510/resources/phoenix_drone_v3.yaml']
    expected=[yaml.safe_load(p.read_text()) for p in paths]
    assert [read_safe_yaml(p) for p in paths]==expected
    monkeypatch.delattr(yaml,'CSafeLoader',raising=False)
    assert [read_safe_yaml(p) for p in paths]==expected


def test_current_ingress_contains_imu_only_state_and_preserves_source_times():
    from tddt_tdoc.online_imu import canonical_imu
    lane,s,p,r,status=inputs()
    fresh=lane.read_current_ingress()
    assert fresh['state']['imu_time_s']==status['imu']['plant_time_s']
    assert all(k not in fresh['state'] for k in ('latitude_deg','velocity_n_m_s','qw','north_m'))
    assert canonical_imu(fresh)['measured_simulated_imu'] is True


@pytest.mark.parametrize('change',['old_source','cross_session','old_mode'])
def test_snapshot_seal_checks_original_mesa_receipt_identity_and_age(change):
    lane,s,p,r,status=inputs()
    original=p['MESA_NAV']['context']['online_state_ingress']['ptlib_ingress']['gui_state']
    if change=='old_source':original['imu']['timestamp_monotonic']-=10
    if change=='cross_session':original['session_id']='0'*32
    if change=='old_mode':original['mode']='QSTABILIZE'
    with pytest.raises(ValueError):lane.seal_snapshot(s,p,r)
