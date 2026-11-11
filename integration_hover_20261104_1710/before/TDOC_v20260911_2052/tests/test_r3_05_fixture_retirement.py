"""Fixture retirement must reject stale config, requests and certificates."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter
from tddt_tdoc.native_environment import prepare_binding
from tddt_tdoc import native_ground_policy as policy

TDOC=Path(__file__).resolve().parents[1]


def config(values=None):
    return SimpleNamespace(get=lambda key,default=None:(values or {}).get(key,default))


def test_canonical_configuration_has_no_active_fixture():
    from tddt_tdoc.config import TDOCConfig
    cfg=TDOCConfig.from_yaml(TDOC/'configs/tdoc.yaml')
    assert cfg.get('layers.layer6.online.ideal_launch_fixture') is None
    assert policy.require_native_contacts(cfg)['decision']=='RETIRE_IDEAL_LAUNCH_FIXTURE'
    assert not (TDOC/'src/tddt_tdoc/launch_fixture.py').exists()


@pytest.mark.parametrize('value',[True,'true','false',1])
def test_retired_option_rejects_before_binding_reads_or_writes(value,tmp_path):
    with pytest.raises(ValueError,match='retired by R3.05'):
        prepare_binding(config({'layers.layer6.online.ideal_launch_fixture':value}),output_path=tmp_path/'binding.json')
    assert not (tmp_path/'binding.json').exists()


@pytest.mark.parametrize('payload',[
    {'launch_fixture':{}},
    {'context':{'environment_contract':{'launch_fixture_state':None}}},
    {'context':{'online_state_ingress':{'ptlib_ingress':{'environment_contract':{'launch_fixture':{}}}}}},
    {'context':{'online_state_ingress':{'ptlib_ingress':{'gui_state':{'environment_contract':{'launch_fixture_state':{}}}}}}}])
def test_stale_fixture_contracts_reject_in_each_supported_ingress(payload):
    with pytest.raises(ValueError,match='retired launch fixture'):policy.require_native_contacts(config(),payload)


def test_policy_integrity_and_revision_are_mandatory(monkeypatch):
    with pytest.raises(ValueError,match='unsupported native ground'):
        policy.require_native_contacts(config({'layers.layer6.online.ground_contact_policy':'unknown'}))
    monkeypatch.setattr(policy,'POLICY_SHA256','0'*64)
    with pytest.raises(ValueError,match='policy hash mismatch'):policy.ground_policy()


def test_old_flag_fails_before_any_layer6_client_or_stack_operation():
    a=Layer6OnlineAdapter(config({'layers.layer6.online.ideal_launch_fixture':True}),SimpleNamespace())
    a._get_client=lambda:pytest.fail('client must not be constructed')
    result=a.prepare_online_transport_session()
    assert not result['ok'] and result['stage']=='ground_policy'
    assert 'retired by R3.05' in result['error'] and not result['actuation_executed']
    assert 'ground_contact_policy_rejected' in a.preflight()['failure_reasons']


def test_transport_preparation_needs_no_missing_fixture_module_or_restart():
    calls=[]
    client=SimpleNamespace(stack_responding=lambda:True,
        ensure_mavlink=lambda **kw:calls.append('connect'),
        wait_fcu_ready=lambda **kw:calls.append('readiness') or {'status_quality':{'native_session_id':'unit-session'}},
        native_session_status=lambda:{'process_ready':True,'session_id':'unit-session'})
    a=Layer6OnlineAdapter(config(),SimpleNamespace());a._get_client=lambda:client;a._fresh=lambda s:True
    result=a.prepare_online_transport_session()
    assert result['ok'] and not result['startup_actuation'] and calls==['connect','readiness']


def test_stale_live_fixture_telemetry_is_rejected():
    a=Layer6OnlineAdapter(config(),SimpleNamespace())
    with pytest.raises(ValueError,match='retired launch fixture'):
        a._environment_ready({'environment_contract':{'launch_fixture_state':{'engaged':True}}})


def certificate_fixture(tmp_path):
    from layer4 import Layer4Config
    from layer4.models import Candidate
    from layer4.multifidelity import CertifiedMultiFidelityManager as Manager
    from layer4.snapshot import Layer3SnapshotAdapter
    root=TDOC.parent/'layer4_v20260907_0608'
    cfg=Layer4Config.from_yaml(root/'configs/configuration_boulder.yaml')
    cfg.data.setdefault('verification',{})['certificate_bank_path']=str(tmp_path/'certificates.jsonl')
    snapshot=Layer3SnapshotAdapter(cfg).adapt(json.loads((root/'examples/layer3_snapshot_example.json').read_text()))
    snapshot.context=dict(snapshot.context or {},flight_phase='hover')
    candidate=Candidate('R305','S305',snapshot.version,snapshot.hash,1.,[[0.]*19],[[[0.]*19]*19],[{}],[[0.]*5],{'mission':0.})
    verification={'verified':True,'candidate_verified':True,'executed':True,'verification_origin':'FF_CURRENT',
        'physical_actions_consumed':True,'physical_runtime_safe':True,'physical_mission_safe':True,
        'wingmpet_physical_verdict':{'authoritative':True,'non_bypassable':True,'all_rows_vtol_feasible':True,
            'rows':[{'source':'WingMPET/op27-substepped'}]}}
    return Manager,cfg,snapshot,candidate,verification


def test_certificate_issuance_rejects_even_a_previously_valid_fixture_contract(tmp_path):
    Manager,cfg,snapshot,candidate,verification=certificate_fixture(tmp_path)
    verification['wingmpet_physical_verdict']['rows'][0].update(source='WingMPET+ideal-SITL-launch-fixture',
        launch_fixture_evidence_valid=True,launch_fixture_contract_hash='previously-valid')
    manager=Manager(cfg)
    assert manager.build(candidate,snapshot,None,'model','parameters',verification,require_physical_verdict=True) is None
    assert not manager.path.exists()


@pytest.mark.parametrize('tag',['source','valid','hashes'])
def test_existing_fixture_certificate_is_not_reused_or_rewritten(tmp_path,tag):
    Manager,cfg,snapshot,candidate,verification=certificate_fixture(tmp_path)
    manager=Manager(cfg)
    cert=manager.build(candidate,snapshot,None,'model','parameters',verification,require_physical_verdict=True)
    assert cert and manager.find(candidate,snapshot,None,'model','parameters')
    evidence=cert['physical_issuance_evidence']
    if tag=='source':evidence['verdict_sources']=['WingMPET+ideal-SITL-launch-fixture']
    elif tag=='valid':evidence['launch_fixture_evidence_valid']=True
    else:evidence['launch_fixture_contract_hashes']=['legacy-hash']
    manager.path.write_text(json.dumps(cert)+'\n');before=manager.path.read_bytes()
    manager=Manager(cfg)
    assert manager.find(candidate,snapshot,None,'model','parameters') is None
    assert manager.path.read_bytes()==before
    assert manager.reuse_stats()['misses']['retired_fixture_certificate']>=1
