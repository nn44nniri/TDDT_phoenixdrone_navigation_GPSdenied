from __future__ import annotations
import copy, json, os, tempfile
from pathlib import Path
import pytest
import yaml
from layer5.native_pilot_boundary import audit, SCHEMA
from layer5.config import Layer5Config
from layer5.engine import Layer5Engine
from layer5.execution import Layer6Assembler
from layer5.knowledge import KStore

ROOT=Path(__file__).resolve().parents[1]
ORIGINAL=json.loads((ROOT/'examples/layer4_result_verified_TEST_FIXTURE.json').read_text())
BASE=yaml.safe_load((ROOT/'configs/configuration_phoenix.yaml').read_text())


def cfg(tmp_path,monkeypatch):
    p=tmp_path/'kstore';monkeypatch.setenv('TDDT_KSTORE_ROOT',str(p))
    d=copy.deepcopy(BASE);d.setdefault('testing',{})['allow_fixture_verification']=True
    d['confidence']['auto_threshold']=0.0;d['knowledge']['directory']=str(p)
    return Layer5Config(d,ROOT/'configs/configuration_phoenix.yaml')


def committed(c):
    k=KStore(c);k.start_episode('E1');k.append({'x':1})
    k.finalize({'ready':True})


def test_online_with_verified_fixture_and_setd_is_hold(tmp_path,monkeypatch):
    c=cfg(tmp_path,monkeypatch);committed(c)
    r=Layer5Engine(c).run(ORIGINAL,'ONLINE_JSBSIM',human_approval=True,requested_mode='AUTO',execute_environment=True).to_dict()
    assert r['gates']['FINAL'] is False
    assert r['operational_mode']=='HOLD'
    assert r['control_action'] is None and r['layer6_package'] is None
    assert r['fallback']['used'] is True
    assert r['environment_feedback']['executed'] is False
    assert r['gates']['native_pilot_controller']['status']=='BLOCKED_NATIVE_PILOT_CONTROLLER'
    assert r['knowledge']['setd']['ok'] is True
    assert r['knowledge']['online_persistent_write'] is False
    assert r['gates']['native_pilot_controller']['raw_verified_candidate_count']>=1


def test_caller_spoofed_certification_cannot_release(tmp_path,monkeypatch):
    c=cfg(tmp_path,monkeypatch);committed(c)
    forged=copy.deepcopy(ORIGINAL)
    forged['native_controller_certified']=True
    forged['flight_authorized']=True
    forged['provenance']['native_controller_certified']=True
    forged['provenance']['flight_authorized']=True
    for item in forged['candidates']:
        item['verification'].update({'native_controller_certified':True,'flight_authorized':True,
          'verification_origin':'FF_CERT_REUSE','verification_scope':'NATIVE_PILOT_CONTROLLER',
          'certificate_persisted':True,'certificate_id':'FORGED', 'native_control_binding':{'provider_kind':'REAL'}})
    r=Layer5Engine(c).run(forged,'ONLINE_JSBSIM',execute_environment=True).to_dict()
    assert r['gates']['FINAL'] is False and r['layer6_package'] is None
    assert r['gates']['native_pilot_controller']['native_controller_certified'] is False
    assert r['gates']['native_pilot_controller']['flight_authorized'] is False
    assert r['gates']['native_pilot_controller']['pilot_to_actuator_replay_executed'] is False


def test_direct_assembler_bypass_refused(tmp_path,monkeypatch):
    c=cfg(tmp_path,monkeypatch)
    a=Layer6Assembler(c)
    with pytest.raises(RuntimeError,match='native_pilot_controller_prediction_unavailable'):
        a.assemble([0,0,0,0],['left_motor'],{}, {},0.0,1.0,1.0,
          {'flight_authorized':True,'native_controller_certified':True,'certificate_id':'FORGED'})


def test_offline_valid_model_does_not_become_native_authority(tmp_path,monkeypatch):
    c=cfg(tmp_path,monkeypatch)
    r=Layer5Engine(c).run(ORIGINAL,'OFFLINE_SE').to_dict()
    assert r['gates']['FINAL'] is True
    assert r['layer6_package'] is None
    assert r['control_action']['actuator_targets']==['aileron_domain','elevator_domain','throttle_domain','rudder_domain']
    assert r['control_action']['constraints']['native_execution_authorized'] is False
    assert r['control_action']['constraints']['command_domain']=='pilot_reference_not_post_mixer_actuator'


def test_offline_prior_and_online_rule_do_not_collide():
    offline=audit(ORIGINAL,learning_mode='OFFLINE_SE');online=audit(ORIGINAL,learning_mode='ONLINE_JSBSIM')
    assert offline['schema']==SCHEMA and offline['model_only_prior_allowed_for_offline']
    assert online['native_controller_certified'] is False
    assert online['raw_verified_candidate_count']>0
    assert online['full_fidelity_authority_for_online'] is False


def test_missing_verified_candidates_does_not_vacuously_certify():
    p=copy.deepcopy(ORIGINAL);p['verified_candidates']=[]
    a=audit(p,learning_mode='ONLINE_JSBSIM')
    assert a['raw_verified_candidate_count']==0
    assert a['status']=='BLOCKED_NATIVE_PILOT_CONTROLLER'


def test_online_prior_storage_untouched(tmp_path,monkeypatch):
    c=cfg(tmp_path,monkeypatch);committed(c)
    k=tmp_path/'kstore'
    before={str(f.relative_to(k)):f.read_bytes() for f in k.rglob('*') if f.is_file()}
    Layer5Engine(c).run(ORIGINAL,'ONLINE_JSBSIM')
    after={str(f.relative_to(k)):f.read_bytes() for f in k.rglob('*') if f.is_file()}
    assert before==after


def test_offline_label_cannot_launder_native_online_context(tmp_path,monkeypatch):
    c=cfg(tmp_path,monkeypatch)
    case=copy.deepcopy(ORIGINAL)
    case['context']={'online_episode_id':'IMPERSONATED', 'command_domain':'native_pilot'}
    r=Layer5Engine(c).run(case,learning_mode='OFFLINE_SE').to_dict()
    assert not r['gates']['FINAL']
    assert r['layer6_package'] is None
    assert r['control_action'] is None
    assert r['gates']['native_pilot_controller']['online_native_pilot_required']


@pytest.mark.parametrize('mode',['QHOVER','QSTABILIZE','QLOITER','QLAND','QRTL','FBWA'])
def test_offline_native_mode_cannot_be_certified_without_native_predictor(tmp_path,monkeypatch,mode):
    c=cfg(tmp_path,monkeypatch)
    d=copy.deepcopy(ORIGINAL)
    d['context']={'flight_mode':mode}
    r=Layer5Engine(c).run(d,'OFFLINE_SE').to_dict()
    assert r['gates']['FINAL'] is False
    assert r['layer6_package'] is None and r['control_action'] is None
    assert r['gates']['native_pilot_controller']['flight_authorized'] is False


def test_read_only_cli_online_exit2_no_overwrite(tmp_path):
    import subprocess,sys
    cmd=[sys.executable,str(ROOT/'scripts/check_stage5_native_boundary.py'),
         '--input',str(ROOT/'examples/layer4_result_verified_TEST_FIXTURE.json'),
         '--learning-mode','ONLINE_JSBSIM','--output',str(tmp_path/'audit.json')]
    first=subprocess.run(cmd,capture_output=True,text=True)
    assert first.returncode==2,first.stderr
    doc=json.loads((tmp_path/'audit.json').read_text())
    assert doc['flight_authorized'] is False
    assert doc['status']=='BLOCKED_NATIVE_PILOT_CONTROLLER'
    second=subprocess.run(cmd,capture_output=True,text=True)
    assert second.returncode==3
    assert json.loads((tmp_path/'audit.json').read_text())==doc


def test_preflight_dependencies_never_imply_native_release(tmp_path,monkeypatch):
    c=cfg(tmp_path,monkeypatch)
    report=Layer5Engine(c).preflight(strict_external=False)
    assert report['native_pilot_controller']['native_controller_certified'] is False
    assert report['native_pilot_controller']['flight_authorized'] is False
    assert report['preflight_scope']=='PACKAGE_AND_EXTERNAL_DEPENDENCY_ONLY_NOT_NATIVE_EXECUTION_READINESS'
