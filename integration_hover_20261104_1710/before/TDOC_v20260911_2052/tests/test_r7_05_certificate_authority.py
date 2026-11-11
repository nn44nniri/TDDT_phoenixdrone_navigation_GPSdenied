"""Public consumer rereads disk evidence; Native acceptance is a separate audit."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import shutil
import time

from layer4.native_provider_contract import digest
from layer4.native_provider_certificate import issue_certificate
from tddt_tdoc.native_certificate import NativeCertificateAuthority
from layer6.timebase import monotonic_domain


def setup(tmp_path):
    root=Path(__file__).resolve().parents[2]
    spec=importlib.util.spec_from_file_location('r705_fixture',root/'layer4_v20260907_0608/tests/test_r7_05_native_certificate.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    b,i,old=m.fixture(tmp_path)
    r,p,e,o=(b[k] for k in ('request','prediction','evidence','observation'))
    r['timing']['admission_deadline_unix_s']=time.time()+60
    r['timing']['monotonic_domain_id']=i['clock_domain']=monotonic_domain()
    p['request_sha256']=digest(r);e['request_sha256']=digest(r);e['prediction_sha256']=digest(p)
    c=issue_certificate(b,**i)
    case=tmp_path/'case';case.mkdir()
    for key,name in [('request','REQUEST.json'),('prediction','PREDICTION.json'),
                     ('evidence','FORECAST_EVIDENCE.json'),('observation','OBSERVATION.json')]:
        (case/name).write_text(json.dumps(b[key]))
    for kind,path,backend in [('prediction',b['prediction_trace'],r['backend']),
                              ('observation',b['observation_trace'],o['reference']['backend'])]:
        shutil.copytree(path.parent,case/kind)
        (case/kind/'PARAMETERS.json').write_text(json.dumps(backend['parameters']))
    authority=object.__new__(NativeCertificateAuthority)
    authority._current=lambda:(deepcopy(i['model']),deepcopy(i['identity']),i['clock_domain'])
    authority._history=lambda:deepcopy(i['history'])
    return authority,case,c,i


def test_public_consumer_rereads_parameters_instead_of_cached_pass(tmp_path):
    a,case,c,i=setup(tmp_path);pin=digest(c)
    assert a.consume(c,pin,case)['status']=='VALID_IN_EXACT_REFERENCE_SCOPE'
    path=case/'prediction/PARAMETERS.json';parameters=json.loads(path.read_text());parameters['SERVO1_TRIM']+=1
    path.write_text(json.dumps(parameters))
    result=a.consume(c,pin,case)
    assert result['status']=='REJECTED' and 'parameter evidence changed' in result['reason']


def test_change_during_consumption_rejected_after_successful_inner_check(tmp_path):
    a,case,c,i=setup(tmp_path);count=0
    def current():
        nonlocal count
        count+=1
        model=deepcopy(i['model'])
        if count>1:model['artifacts']['binary']='changed during check'
        return model,deepcopy(i['identity']),i['clock_domain']
    a._current=current
    result=a.consume(c,digest(c),case)
    assert result['status']=='REJECTED' and 'changed during' in result['reason']
