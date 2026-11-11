from __future__ import annotations
import json, os
from pathlib import Path
import pytest
from tddt_tdoc import TDOCConfig
from tddt_tdoc.adapters.layer7 import Layer7Adapter
from tddt_tdoc.feedback import FeedbackRouter

ROOT=Path(__file__).parents[1]

def test_layer7_is_external_and_tdoc_owned_in_source():
    env=(ROOT/'configs/environment.sh').read_text()
    cfg=(ROOT/'configs/tdoc.yaml').read_text()
    src=(ROOT/'src/tddt_tdoc/adapters/layer7.py').read_text()
    assert 'layer7_v20260901_0921' in env
    assert 'TDDT_LAYER7_ROOT' in env and '$TDDT_LAYER7_ROOT/src' in env
    assert 'expected_contract: tdoc-layer7-v2' in cfg
    assert 'Layer7SideModule' in src and 'direct_actuation' in src
    assert not (ROOT/'src/tddt_layer7').exists()

def test_layer7_actual_package_contract_when_configured(tmp_path):
    l7=os.environ.get('TDDT_LAYER7_ROOT')
    if not l7: pytest.skip('TDDT_LAYER7_ROOT not configured')
    cfg=TDOCConfig.from_yaml(ROOT/'configs/tdoc.yaml'); cfg.data['runtime']['root']=str(tmp_path/'runtime')
    a=Layer7Adapter(cfg); pf=a.preflight()
    assert pf['ok'] and pf['version']=='0.3.0' and pf['contract_ok']
    d=pf['registration_descriptor']
    assert d['contract_version']=='tdoc-layer7-v2' and d['trigger_owner']=='TDOC'
    assert d['side_module'] is True and d['direct_actuation'] is False

def test_layer7_builder_classifies_jsbsim_as_simulation():
    snap={'policy_version':'v1','reference_time_s':1.0,'std':{},'weights':{'A':1.0},'couplings':{},'topology':{},'mismatch':0.1,'consistency_margin':0.5,'uncertainty_td':{},'guidance':{},'constraints':{},'context':{},'stability':{'stable':True}}
    e={'command':[0.0,0.0,0.5,0.0],'write_result':{'aileron':0.0,'elevator':0.0,'throttle':0.5,'rudder':0.0},'io_snapshot':{},'evidence_hash':'h'}
    p=Layer7Adapter.build_input(transaction_id='t',version='v1',reference_time_s=1.0,execution_evidence=e,layer4_result={'candidates':[]},layer5_result={'result':{'decision':{'confidence':1.0}}},network_snapshot=snap,request={})
    assert p['ExecutionEvidence']['evidence_type']=='simulation'
    assert p['ExecutionEvidence']['version']==p['PredictionEvidence']['version']==p['NetworkSnapshot']['version']=='v1'
    assert p['ExecutionEvidence']['output_semantics']==p['PredictionEvidence']['output_semantics']
    assert p['AdaptationHints']['authorized'] is False

def test_layer7_versioned_feedback_router(tmp_path):
    r=FeedbackRouter(tmp_path/'feedback')
    out={'feedback_layer2':{'DeltaX':{'x':0.1},'nu_new':'v2'},'feedback_layer31':{},'feedback_layer32':{'DeltaK':{'A->B':-0.1},'nu_new':'v2'},'feedback_layer33':{}}
    rr=r.route_layer7(out,transaction_id='tx',base_version='v1')
    assert rr['routed'] and set(rr['destinations'])=={'Layer2','Layer3.2'}
    pending=r.pending_context(); assert pending['Layer2']['base_version']=='v1' and pending['Layer3.2']['packet']['nu_new']=='v2'

def test_layer7_feedback_is_scoped_to_current_online_run_and_episode(tmp_path):
    r=FeedbackRouter(tmp_path/'feedback')
    out={'feedback_layer2':{'DeltaX':{'x':0.1},'nu_new':'v2'}}
    run_a={'run_id':'run-a','episode_id':'episode-1','mission_version':'m1','cycle_id':7,'phase_id':'hover'}
    r.route_layer7(out,transaction_id='tx-a',base_version='v1',run_context=run_a)
    assert r.pending_context(run_context={**run_a,'cycle_id':8})['Layer2']['transaction_id']=='tx-a'
    assert r.pending_context(run_context={**run_a,'episode_id':'episode-2'})=={}
    assert r.pending_context(run_context={**run_a,'run_id':'run-b'})=={}

def test_layer7_legacy_unscoped_feedback_is_rejected_by_online_scope(tmp_path):
    r=FeedbackRouter(tmp_path/'feedback')
    out={'feedback_layer2':{'DeltaX':{'x':0.1},'nu_new':'v2'}}
    r.route_layer7(out,transaction_id='legacy-tx',base_version='v1')
    assert r.pending_context()['Layer2']['transaction_id']=='legacy-tx'
    assert r.pending_context(run_context={'run_id':'run-new','episode_id':'episode-1'})=={}

def test_layer7_actual_side_module_rejects_simulation_ack_without_physical_prediction(tmp_path):
    l7=os.environ.get('TDDT_LAYER7_ROOT')
    if not l7: pytest.skip('TDDT_LAYER7_ROOT not configured')
    cfg=TDOCConfig.from_yaml(ROOT/'configs/tdoc.yaml'); cfg.data['runtime']['root']=str(tmp_path/'runtime')
    a=Layer7Adapter(cfg)
    snap={'policy_version':'v1','reference_time_s':1.0,'std':{},'weights':{'TwinA':1.0},'couplings':{},'topology':{},'mismatch':0.0,'consistency_margin':0.5,'uncertainty_td':{},'guidance':{},'constraints':{},'context':{},'stability':{'stable':True}}
    ev={'command':[0.0,0.0,0.5,0.0],'write_result':{'aileron':0.0,'elevator':0.0,'throttle':0.5,'rudder':0.0},'io_snapshot':{},'evidence_hash':'contract-test'}
    payload=Layer7Adapter.build_input(transaction_id='l7-real-contract',version='v1',reference_time_s=1.0,execution_evidence=ev,layer4_result={'candidates':[]},layer5_result={'result':{'decision':{'approval':True,'confidence':1.0}}},network_snapshot=snap,request={})
    out=a.invoke(payload)
    assert out['ok'] is False and out['result']['evidence_ok'] is False
    assert out['result']['mode']=='REJECT' and out['no_change'] is False
    assert out['result']['commit']['status']=='REJECTED'
    assert out['registration_descriptor']['trigger_owner']=='TDOC'
