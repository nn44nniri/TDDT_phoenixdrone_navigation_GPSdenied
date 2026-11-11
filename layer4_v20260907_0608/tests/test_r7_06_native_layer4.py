"""Layer4/TDOC contract fixtures; actual Native acceptance is a separate audit."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from layer4.engine import Layer4Engine
from layer4.verification import FullFidelityVerifier
from layer4.native_provider_contract import digest
from tddt_tdoc.layer4_native_reference import Layer4NativeReferenceService
from tddt_tdoc.native_certificate import read_bundle


def fixture(tmp_path):
    root=Path(__file__).resolve().parents[2]
    path=root/'TDOC_v20260911_2052/tests/test_r7_05_certificate_authority.py'
    spec=importlib.util.spec_from_file_location('r705_authority_fixture',path)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    authority,case,c,i=m.setup(tmp_path)
    bundle=read_bundle(case)
    class Provider:
        last_evidence=bundle['evidence']
        calls=0
        def predict(self,request):
            assert request==bundle['request'];self.calls+=1
            return deepcopy(bundle['prediction'])
    observer_calls=[]
    def observe(request):
        observer_calls.append(deepcopy(request))
        return deepcopy(bundle['observation'])
    service=Layer4NativeReferenceService(None,observe,authority,case)
    service.provider=Provider()
    return bundle['request'],service,observer_calls


def test_real_engine_entrypoint_uses_provider_and_current_certificate(cfg,tmp_path):
    r,s,obs=fixture(tmp_path)
    e=Layer4Engine(cfg,native_reference_service=s)
    result=e.run(r).to_dict()
    assert result['verification']['verified'],result
    assert result['verification']['outcome_status']=='ATTEMPTED_AND_VERIFIED'
    assert result['reference_candidate']['candidate_id']==r['context']['candidate_id']
    assert result['reference_candidate']['snapshot_hash']==r['context']['snapshot_hash']
    assert result['prediction']['samples'] and len(result['verified_reference_candidates'])==1
    assert result['verified_candidates']==[] and not result['flight_authorized'] and not result['actuator_command']
    assert not result['verification']['native_controller_certified']
    assert s.prediction_calls==s.provider.calls==s.observation_calls==len(obs)==1
    # Rechecking may reuse immutable evidence, never a cached positive verdict.
    assert e.run(r).to_dict()['verification']['verified']
    assert s.provider.calls==len(obs)==1
    path=s.case/'prediction/PARAMETERS.json';p=json.loads(path.read_text());p['SERVO1_TRIM']+=1
    path.write_text(json.dumps(p))
    rejected=e.run(r).to_dict()
    assert not rejected['verification']['verified'] and 'parameter evidence' in rejected['verification']['reason']
    assert s.provider.calls==len(obs)==1


@pytest.mark.parametrize('change',['candidate','snapshot','phase','mode','plan','dt','commit','initial_state','schema'])
def test_changed_request_cannot_reuse_service_or_call_provider_again(cfg,tmp_path,change):
    r,s,obs=fixture(tmp_path);e=Layer4Engine(cfg,native_reference_service=s)
    assert e.run(r).to_dict()['verification']['verified']
    other=deepcopy(r)
    if change in ('candidate','snapshot','phase'):
        key={'candidate':'candidate_id','snapshot':'snapshot_hash','phase':'phase'}[change]
        other['context'][key]='a'*64 if change=='snapshot' else 'other'
    if change=='mode':other['backend']['initial_mode']='QHOVER'
    if change=='plan':other['pilot_plan'][0]['u_cmd'][0]=.25
    if change=='dt':other['timing']['prediction_dt_s']=.3
    if change=='commit':other['timing']['commit_segment_s']=.15
    if change=='initial_state':other['initial_state']['controller']['uncertainty']['reason']='different'
    if change=='schema':other['schema']='wrong'
    result=e.run(other).to_dict()
    assert not result['verification']['verified'] and result['prediction'] is None
    assert s.provider.calls==len(obs)==1


def test_unbound_disabled_and_flag_spoofs_never_start_native(cfg,tmp_path):
    r,s,obs=fixture(tmp_path)
    missing=Layer4Engine(cfg).run(r).to_dict()
    assert not missing['verification']['verified'] and not missing['verification']['attempted']
    cfg.data.setdefault('verification',{})['native_reference']={'enabled':False}
    assert not Layer4Engine(cfg,native_reference_service=s).run(r).to_dict()['verification']['verified']
    assert s.provider.calls==len(obs)==0
    cfg.data['verification']['native_reference']['enabled']=True
    spoof=deepcopy(r);spoof['context']['native_controller_certified']=True
    assert not Layer4Engine(cfg,native_reference_service=s).run(spoof).to_dict()['verification']['verified']
    assert s.provider.calls==len(obs)==0


def test_permissive_proof_and_backend_exception_do_not_become_verification(cfg,tmp_path):
    r,s,obs=fixture(tmp_path)
    fake=SimpleNamespace(evaluate=lambda request:{'verified':True,'flight_authorized':True})
    out=Layer4Engine(cfg,native_reference_service=fake).run(r).to_dict()
    assert not out['verification']['verified'] and out['verified_candidates']==[]
    def fail(request):raise RuntimeError('native backend disconnected')
    out=Layer4Engine(cfg,native_reference_service=SimpleNamespace(evaluate=fail)).run(r).to_dict()
    assert not out['verification']['verified'] and 'disconnected' in out['verification']['reason']


def test_expired_certificate_is_not_reissued_or_replayed(cfg,tmp_path):
    r,s,obs=fixture(tmp_path);e=Layer4Engine(cfg,native_reference_service=s)
    assert e.run(r).to_dict()['verification']['verified']
    v=s.certificate['validity'];v['issued_unix_s']=time.time()-60;v['expires_unix_s']=v['issued_unix_s']+30
    v['issued_monotonic_s']=time.monotonic()-60;v['expires_monotonic_s']=v['issued_monotonic_s']+30
    s.pin=digest(s.certificate)
    out=e.run(r).to_dict()
    assert not out['verification']['verified'] and 'expired' in out['verification']['reason']
    assert s.provider.calls==len(obs)==1


def test_invalid_nonfinite_request_has_strict_json_rejection(cfg,tmp_path):
    r,s,obs=fixture(tmp_path);r['pilot_plan'][0]['u_cmd'][0]=float('nan')
    out=Layer4Engine(cfg,native_reference_service=s).run(r).to_dict()
    assert not out['verification']['verified']
    json.dumps(out,allow_nan=False)
    assert s.provider.calls==len(obs)==0
