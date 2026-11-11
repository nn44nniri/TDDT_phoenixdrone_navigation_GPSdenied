"""Certificate adversarial contract tests, not Native acceptance evidence."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

from layer4.native_provider_contract import digest
from layer4.native_provider_certificate import issue_certificate, check_certificate


def fixture(tmp_path):
    spec=importlib.util.spec_from_file_location('r704_fixture',Path(__file__).with_name('test_r7_04_independent_validation.py'))
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    r,p,e,o,paths=m.documents(tmp_path)
    model={'schema':'UNIT_DEPENDENCIES','artifacts':{'binary':'unit-only'}};model['model_id']=digest(model)
    r['context']['model_id']=model['model_id'];r['timing']['admission_deadline_unix_s']=1100.
    seal=json.loads((paths[0].parent/'SEALED_REFERENCE.json').read_text())
    seal['context']['model_id']=model['model_id']
    (paths[0].parent/'SEALED_REFERENCE.json').write_text(json.dumps(seal))
    r['isolation']['initial_reference_sha256']=r['context']['snapshot_hash']=digest(seal)
    p['isolation']['initial_reference_sha256']=digest(seal);p['request_sha256']=digest(r)
    e['request_sha256']=digest(r);e['prediction_sha256']=digest(p)
    o['reference']['context']['model_id']=model['model_id']
    (paths[1].parent/'SEALED_REFERENCE.json').write_text(json.dumps(o['reference']))
    bundle={'request':r,'prediction':p,'evidence':e,'observation':o,
            'prediction_trace':paths[0],'observation_trace':paths[1]}
    inputs={'model':model,'identity':{'model_id':model['model_id'],'binding_hash':r['context']['binding_hash'],
             'handoff_hash':r['context']['handoff_hash'],'forcing_file_sha256':r['context']['forcing_sha256']},
            'clock_domain':r['timing']['monotonic_domain_id'],
            'history':{'known_out_of_budget_rejection_preserved':True,'scope_expansion_permitted':False,'evidence_sha256':'e'*64}}
    c=issue_certificate(bundle,**inputs,now_unix=1000.,now_monotonic=50.)
    return bundle,inputs,c


def consume(b,i,c,**kw):
    opts={'now_unix':1001.,'now_monotonic':51.,**kw}
    return check_certificate(c,digest(c),b,**i,**opts)


def test_positive_exact_scope_roundtrip_and_no_flight_authority(tmp_path):
    b,i,c=fixture(tmp_path)
    copy=json.loads(json.dumps(c))
    result=consume(b,i,copy)
    assert result['status']=='VALID_IN_EXACT_REFERENCE_SCOPE',result
    assert not result['flight_authorized'] and not result['operational_prediction_qualified']
    assert copy['claims']['scope']['uncertainty']=='unbounded'
    assert not copy['claims']['scope']['future_rollouts_covered']


@pytest.mark.parametrize('change',['candidate','snapshot','phase','session','model','parameter','gain','binary','mode',
    'forcing','binding','handoff','dt','commit','plant_time','clock_mapping','boot','initial_state',
    'prediction','observation','policy','history','raw_trace','missing_seal','CRC'])
def test_changed_consumption_inputs_rejected_even_with_same_certificate(tmp_path,change):
    b,i,c=fixture(tmp_path)
    r=b['request']
    if change in ('candidate','snapshot','phase','session'):
        key={'candidate':'candidate_hash','snapshot':'snapshot_hash','phase':'phase','session':'native_session_id'}[change]
        r['context'][key]='changed'
    if change=='model':i['model']['artifacts']['binary']='new'
    if change=='parameter':r['backend']['parameters']['SERVO1_TRIM']+=1
    if change=='gain':r['backend']['gui_gains']['max_roll']=.1
    if change=='binary':r['backend']['arduplane_sha256']='f'*64
    if change=='mode':r['backend']['initial_mode']='QHOVER'
    if change in ('forcing','binding','handoff'):
        key={'forcing':'forcing_file_sha256','binding':'binding_hash','handoff':'handoff_hash'}[change]
        i['identity'][key]='f'*64
    if change in ('dt','commit','plant_time','clock_mapping'):
        key={'dt':'prediction_dt_s','commit':'commit_segment_s','plant_time':'plant_start_bounds_s','clock_mapping':'clock_mapping_sha256'}[change]
        r['timing'][key]={'dt':.3,'commit':.15,'plant_time':[4.,4.02],'clock_mapping':'f'*64}[change]
    if change=='boot':i['clock_domain']='f'*64
    if change=='initial_state':r['initial_state']['previous_pilot']['uncertainty']['reason']='changed initial state'
    if change=='prediction':b['prediction']['samples'][1]['channels']['post_mixer_pwm']['value'][0]+=1
    if change=='observation':b['observation']['candidate_hash']='f'*64
    if change=='policy':c['claims']['validation_policy']['limits']['PWM_max_abs_us']=10000
    if change=='history':i['history']['known_out_of_budget_rejection_preserved']=False
    if change=='raw_trace':b['observation_trace'].write_text('')
    if change=='missing_seal':(b['observation_trace'].parent/'SEALED_REFERENCE.json').unlink()
    if change=='CRC':
        path=b['observation_trace'];rows=[json.loads(x) for x in path.read_text().splitlines()]
        raw=bytearray.fromhex(rows[1]['raw_MAVLink_hex']);raw[-1]^=1;rows[1]['raw_MAVLink_hex']=raw.hex()
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    assert consume(b,i,c)['status']=='REJECTED'


@pytest.mark.parametrize('wall,mono',[(1030.,80.),(1000.,80.),(999.,51.),(1001.,49.),(1005.,51.),(float('nan'),51.)])
def test_expiry_reverse_clocks_discontinuity_and_nonfinite_fail_closed(tmp_path,wall,mono):
    b,i,c=fixture(tmp_path)
    assert consume(b,i,c,now_unix=wall,now_monotonic=mono)['status']=='REJECTED'


@pytest.mark.parametrize('usage',['FLIGHT','LAYER5_FINAL','CURRENT_MISSION_PREDICTION','FUTURE_REFERENCE_ROLLOUT'])
def test_reference_evidence_certificate_cannot_release_an_execution(tmp_path,usage):
    b,i,c=fixture(tmp_path)
    assert consume(b,i,c,usage=usage)['status']=='REJECTED'


def test_certificate_self_hash_is_not_a_trust_anchor(tmp_path):
    b,i,c=fixture(tmp_path);pin=digest(c)
    c['claims']['scope']['flight_authorized']=True
    assert check_certificate(c,pin,b,**i,now_unix=1001.,now_monotonic=51.)['status']=='REJECTED'
    # Even a caller repinning altered claims cannot satisfy normative scope.
    assert consume(b,i,c)['status']=='REJECTED'


def test_issuer_refuses_invalid_stale_and_negative_inputs(tmp_path):
    b,i,c=fixture(tmp_path)
    with pytest.raises(ValueError,match='expired'):
        issue_certificate(b,**i,now_unix=1100.,now_monotonic=50.)
    with pytest.raises(ValueError,match='lifetime'):
        issue_certificate(b,**i,lifetime_s=31.,now_unix=1000.,now_monotonic=50.)
    b['prediction']['status']='UNAVAILABLE';b['prediction']['samples']=[]
    with pytest.raises(ValueError,match='rejected'):
        issue_certificate(b,**i,now_unix=1000.,now_monotonic=50.)


def test_mutable_input_and_certificate_cannot_alias(tmp_path):
    b,i,c=fixture(tmp_path)
    old=deepcopy(c)
    i['history']['evidence_sha256']='f'*64
    assert c==old and consume(b,i,c)['status']=='REJECTED'
