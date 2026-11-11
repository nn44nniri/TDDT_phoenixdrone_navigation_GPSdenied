"""R7.07 denial/readiness tests; no fixture establishes a positive FINAL."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import time

import pytest

from layer4.engine import Layer4Engine
from layer4.config import Layer4Config
from layer4.native_provider_contract import digest
from layer5.engine import Layer5Engine
from layer5.config import Layer5Config
from layer5.contract import Layer4ContractAdapter
from layer5.execution import Layer6Assembler
from tddt_tdoc.layer5_native_readiness import Layer5NativeReadinessChecker


def fixture(tmp_path,monkeypatch):
    root=Path(__file__).resolve().parents[2]
    spec=importlib.util.spec_from_file_location('r706_fixture',root/'layer4_v20260907_0608/tests/test_r7_06_native_layer4.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    request,service,observations=m.fixture(tmp_path)
    cfg4=Layer4Config.from_yaml(root/'layer4_v20260907_0608/configs/configuration_boulder.yaml')
    engine4=Layer4Engine(cfg4,native_reference_service=service)
    result=engine4.run(request).to_dict()
    assert result['verification']['verified']
    class Adapter:
        def verify_native_reference(self,r,s):
            assert s is service
            return engine4.run(r).to_dict()
    checker=Layer5NativeReadinessChecker(Adapter(),service)
    monkeypatch.setenv('TDDT_KSTORE_ROOT',str(tmp_path/'isolated_kstore'))
    cfg5=Layer5Config.from_yaml(root/'layer5_v20260911_1510/configs/configuration_phoenix.yaml')
    engine5=Layer5Engine(cfg5);engine5.native_readiness_checker=checker
    return engine5,request,service,result,checker


def test_real_layer5_run_distinguishes_valid_reference_from_final_readiness(tmp_path,monkeypatch):
    engine,r,service,l4,checker=fixture(tmp_path,monkeypatch)
    result=engine.run(l4,learning_mode='ONLINE_JSBSIM').to_dict()
    assert result['reference_evidence']['status']=='VALID_REFERENCE_ONLY',result
    assert not result['ok'] and result['status']=='NOT_READY' and not result['gates']['FINAL']
    assert result['D_exec'] is result['layer6_package'] is result['control_action'] is None
    assert result['decision_confidence'] is None
    assert all(result['gates'][g]['ok'] is None and result['gates'][g]['status']=='NOT_ASSESSED'
               for g in ('CI','ADM','PSF','RTA','governance'))
    assert not result['acceptance']['R7_07_end_condition_met']
    assert engine.kstore.episode_id is None and service.prediction_calls==service.observation_calls==1


@pytest.mark.parametrize('flag',['human','FINAL','CI','ADM','PSF','RTA','governance','verified_candidates'])
def test_approval_or_fabricated_gate_flags_do_not_widen_reference_certificate(tmp_path,monkeypatch,flag):
    engine,r,service,l4,checker=fixture(tmp_path,monkeypatch)
    if flag!='human':
        if flag=='verified_candidates':l4[flag]=[l4['reference_candidate']]
        else:l4.setdefault('gates',{})[flag]=True
    result=engine.run(l4,learning_mode='ONLINE_JSBSIM',human_approval=True,execute_environment=True).to_dict()
    assert not result['gates']['FINAL'] and result['D_exec'] is None and not result['environment_feedback']['executed']
    assert engine.kstore.episode_id is None and service.prediction_calls==service.observation_calls==1


@pytest.mark.parametrize('change',['missing_checker','candidate','expiry','parameter','unverified'])
def test_current_readiness_is_not_a_cached_positive_receipt(tmp_path,monkeypatch,change):
    engine,r,s,l4,checker=fixture(tmp_path,monkeypatch)
    assert engine.run(l4,learning_mode='ONLINE_JSBSIM').to_dict()['reference_evidence']['status']=='VALID_REFERENCE_ONLY'
    if change=='missing_checker':engine.native_readiness_checker=None
    if change=='candidate':l4['reference_candidate']['candidate_id']='another'
    if change=='unverified':l4['verification']['verified']=False
    if change=='parameter':
        p=s.case/'prediction/PARAMETERS.json'
        import json
        v=json.loads(p.read_text());v['SERVO1_TRIM']+=1;p.write_text(json.dumps(v))
    if change=='expiry':
        v=s.certificate['validity'];v['issued_unix_s']=time.time()-60;v['expires_unix_s']=v['issued_unix_s']+30
        v['issued_monotonic_s']=time.monotonic()-60;v['expires_monotonic_s']=v['issued_monotonic_s']+30;s.pin=digest(s.certificate)
    result=engine.run(l4,learning_mode='ONLINE_JSBSIM').to_dict()
    assert result['reference_evidence']['status']=='REJECTED' and not result['gates']['FINAL']
    assert result['layer6_package'] is None and s.prediction_calls==s.observation_calls==1


def test_reference_inspection_never_admits_offline_episode(tmp_path,monkeypatch):
    engine,r,s,l4,checker=fixture(tmp_path,monkeypatch)
    result=engine.run(l4,learning_mode='OFFLINE_SE').to_dict()
    assert not result['gates']['FINAL'] and engine.kstore.episode_id is None


@pytest.mark.parametrize('field,value',[('verification_scope','NATIVE_EXACT_REFERENCE_REVIEW'),
                                        ('verification_origin','NATIVE_REFERENCE_CURRENT_CERTIFICATE')])
def test_reference_cannot_be_promoted_to_ff_by_executed_flag(field,value):
    forged={'verified':True,'candidate_verified':True,'executed':True,field:value}
    assert not Layer4ContractAdapter._full_fidelity_authority(forged)


def test_assembler_refuses_reference_review_even_with_all_safety_flags():
    root=Path(__file__).resolve().parents[2]
    cfg=Layer5Config.from_yaml(root/'layer5_v20260911_1510/configs/configuration_phoenix.yaml')
    with pytest.raises(RuntimeError,match='no_FINAL_or_D_exec_authority'):
        Layer6Assembler(cfg).assemble([0.,0.,0.,0.],[],{}, {'psf':True,'rta':True},
            time.time(),time.time()+1,1.,{'verification_scope':'NATIVE_EXACT_REFERENCE_REVIEW','FINAL':True})
