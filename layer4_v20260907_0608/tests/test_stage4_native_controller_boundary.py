import json, math, subprocess, sys
from pathlib import Path
from layer4.native_controller_contract import needs_native_controller, readiness
from layer4.models import AtomicSnapshot
from layer4.multifidelity import CertifiedMultiFidelityManager
from layer4.verification import FullFidelityVerifier
from layer4.snapshot import Layer3SnapshotAdapter
from layer4.models import Candidate, Scenario


def snapshot(ctx=None):
    return AtomicSnapshot('v',29.001,'hash',{}, {},0,{}, {}, {}, 1,{}, {}, {},ctx or {},{}, {},{})


def candidate():
    return Candidate('C','S','v','hash',1.0, [[0.0]*19]*2, [[[0.0]*19]*19], [{}], [[0.1,0,0,0,0]], {'mission':0})


def test_native_pilot_contexts_are_mandatorily_detected():
    for c in ({'online_episode_id':'episode'}, {'rtmo_run_context':{}}, {'pilot_command_domain':'pilot'},
              {'flight_mode':'QSTABILIZE'}, {'flight_mode':'QHOVER'}, {'flight_mode':'FBWA'},
              {'control_domain':'pilot_reference'}, {'native_controller_evidence':{'verified':True}}):
        assert needs_native_controller(snapshot(c))
    for c in ({}, {'flight_mode':'hover'}, {'flight_mode':'cruise'}, {'stage':'vertical_takeoff'}):
        assert not needs_native_controller(snapshot(c))


def test_native_false_certification_even_with_spoofed_telemetry(cfg):
    for fake in ({}, {'verified':True,'controller_hash':'a'*64, 'rpm':240},
                 {'native_controller_certified':True,'flight_authorized':True}):
        s=snapshot({'pilot_command':{'value':0.4},'native_controller_evidence':fake})
        d=readiness(s,candidate(),cfg)
        assert d['native_controller_certified'] is False
        assert d['flight_authorized'] is False
        assert d['provider_kind']=='UNAVAILABLE_NOT_IMPLEMENTED_IN_LAYER4'


def test_commit_forecast_rejections(cfg):
    for commit in (-1,0,math.nan, math.inf, 100):
        s=snapshot({'online_episode_id':'x','execution_segment_s':commit})
        assert readiness(s,candidate(),cfg)['reason']=='invalid_commit_forecast_horizon'
    s=snapshot({'online_episode_id':'x','execution_segment_s':0.1})
    assert readiness(s,candidate(),cfg)['reason']=='native_pilot_controller_prediction_unavailable'


def test_native_verifier_denies_before_unsafe_external_invocation(cfg):
    v=FullFidelityVerifier(cfg,None)
    s=snapshot({'online_episode_id':'x', 'flight_mode':'QHOVER'})
    out=v.verify(candidate(),s)
    assert out['outcome_status']=='NOT_ATTEMPTED'
    assert out['reason']=='native_pilot_controller_prediction_unavailable'
    assert out['native_controller_certified'] is False
    assert not out['verified']
    assert out['flight_authorized'] is False


def test_native_certificates_not_issuable_or_reusable(cfg,tmp_path):
    cfg.data['verification']['certificate_bank_path']=str(tmp_path/'certs.jsonl')
    mf=CertifiedMultiFidelityManager(cfg)
    s=snapshot({'flight_mode':'QHOVER', 'online_episode_id':'x'})
    c=candidate()
    fake={'verified':True,'candidate_verified':True,'executed':True,'verification_origin':'FF_CURRENT','native_controller_certified':True}
    assert mf.build(c,s,None,'model','param',fake) is None
    assert not (tmp_path/'certs.jsonl').exists()
    assert mf.find(c,s,None,'model','param') is None
    assert 'native_controller_certificate_not_supported' in mf.reuse_stats()['misses']


def test_old_model_only_cert_cannot_be_mistaken_for_online_native(cfg,tmp_path):
    cfg.data['verification']['certificate_bank_path']=str(tmp_path/'certs.jsonl')
    mf=CertifiedMultiFidelityManager(cfg)
    c=candidate(); s=snapshot({'stage':'hover'})
    fake={'verified':True,'candidate_verified':True,'executed':True,'verification_origin':'FF_CURRENT'}
    cert=mf.build(c,s,None,'model','param',fake)
    assert cert is not None and cert['verification_scope']=='MODEL_ONLY_LAYER3_FMU_REPLAY'
    assert cert['native_controller_certified'] is False
    assert mf.find(c,s,None,'model','param') is not None
    s.context['online_episode_id']='x'
    assert mf.find(c,s,None,'model','param') is None


def test_native_readiness_cli_blocks_example(root,tmp_path):
    import os
    cmd=[sys.executable,str(root/'scripts/check_stage4_native_readiness.py'),
         '--snapshot',str(root/'examples/stage4_native_controller_EXAMPLE_NOT_SITE_EVIDENCE.json'),
         '--output',str(tmp_path/'report.json')]
    env=os.environ.copy();env['PYTHONPATH']=str(root/'src')
    r=subprocess.run(cmd,capture_output=True,text=True,env=env)
    assert r.returncode==2, r.stderr
    data=json.loads((tmp_path/'report.json').read_text())
    assert data['status']=='BLOCKED_NATIVE_CONTROLLER' and data['flight_authorized'] is False
    assert subprocess.run(cmd,capture_output=True,text=True,env=env).returncode==2
    assert json.loads((tmp_path/'report.json').read_text())==data


def test_native_scope_unverified_no_omega_conflation():
    assert 'measured_or_native_speed_if_omega_claimed' in readiness(snapshot({'flight_mode':'QHOVER'}))['required_evidence']


def test_engine_native_pilot_never_upgrades_model_only_ff(root,cfg,tmp_path):
    from layer4.engine import Layer4Engine
    data=json.loads((root/'examples/layer3_snapshot_example.json').read_text())
    data['context']['flight_mode']='QHOVER'
    data['context']['online_episode_id']='test-episode'
    data.pop('snapshot_hash',None)
    data['provenance'].pop('snapshot_hash',None)
    cfg.data['registry']['path']=str(tmp_path/'cand.jsonl')
    cfg.data['registry']['provenance_path']=str(tmp_path/'events.jsonl')
    cfg.data['verification']['certificate_bank_path']=str(tmp_path/'certs.jsonl')
    result=Layer4Engine(cfg).run(data)
    assert result.candidates
    assert result.conformance['C_native_pilot_controller_required'] is True
    assert result.conformance['C_native_pilot_controller_prediction'] is False
    assert result.conformance['C_FF_verified_count']==0
    assert not result.verified_candidates
    for cand in result.candidates:
        assert cand['verification']['native_control_binding']['flight_authorized'] is False
        assert cand['verification']['reason'] in {'native_pilot_controller_prediction_unavailable','invalid_commit_forecast_horizon'}
        assert cand['verified'] is False
