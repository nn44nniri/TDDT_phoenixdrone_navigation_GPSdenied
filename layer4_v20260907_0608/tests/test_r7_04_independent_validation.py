"""Synthetic/raw-codec unit fixtures; never flight or Native acceptance evidence."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mav

from layer4.native_provider_contract import digest, PREDICTION_SCHEMA
from layer4.native_reference_provider import make_reference_request, output_row, POLICY
from layer4.native_prediction_validation import (RawTrace, decode_row, decode_sample,
    quaternion_distance, residuals, sha, validate_native_pair, validation_policy)


def raw_row(message, received=100.):
    packed = message.pack(mav.MAVLink(None,srcSystem=1,srcComponent=1))
    decoded = mav.MAVLink(None).decode(bytearray(packed))
    return {'source_system':1,'source_component':1,'received_monotonic_s':received,
            'raw_MAVLink_hex':bytes(packed).hex(),'message':decoded.to_dict()}


def documents(tmp_path, *, position_offset=0., quaternion_sign=1.):
    spec=importlib.util.spec_from_file_location('r703_fixture',Path(__file__).with_name('test_r7_03_native_prediction.py'))
    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
    ref, unused=helper.fixture()
    params=ref['backend']['parameters']
    params.update({'RC%d_%s'%(i,s):v for i in range(1,5) for s,v in (('MIN',1100.),('MAX',1900.))})
    ref['backend']['parameter_snapshot_sha256']=digest(params)
    ref['plant_start_bounds_s']=[2.,2.02]
    ref['coordinate_origin']={'message':{'latitude':400100000,'longitude':-1053000000,'altitude':1798510}}
    plan=[{'domain':'LAYER6_PILOT_NORMALIZED','u_cmd':[v,-.1,0.,.1],'mode':'FBWA','discrete':[]}
          for v in (.1,-.2,.3)]
    request=make_reference_request(ref,candidate_id='UNIT_ONLY',pilot_plan=plan)
    paths=[];witness_sets=[]
    for number in range(2):
        folder=tmp_path/str(number);folder.mkdir()
        rows=[raw_row(mav.MAVLink(None).heartbeat_encode(1,3,0,0,3,3),100.+number)]
        witnesses=[]
        for offset in (0.,.1,.2,.4,.6):
            i=0 if offset<=.2 else 1 if offset<=.4 else 2
            a,e,t,r=([0.,0.,0.,0.] if offset==0 else plan[i]['u_cmd'])
            tick=round((2.+offset)*1000)
            q=quaternion_sign if number else 1.
            x=position_offset if number else 0.
            frames=[mav.MAVLink(None).local_position_ned_encode(tick,x,0.,-.14,0.,0.,0.),
                    mav.MAVLink(None).attitude_quaternion_encode(tick,q,0.,0.,0.,0.,0.,0.),
                    mav.MAVLink(None).servo_output_raw_encode(tick*1000,0,1500,1500,1000,1000,0,0,0,0),
                    mav.MAVLink(None).rc_channels_encode(tick,8,round(1500+400*a),round(1500-400*e),1100,round(1500+400*r),
                        *([0]*14),255)]
            wire_rows=[raw_row(msg,100.+number+offset) for msg in frames]
            rows.extend(wire_rows)
            witnesses.append(dict(zip(('LOCAL_POSITION_NED','ATTITUDE_QUATERNION','SERVO_OUTPUT_RAW','RC_CHANNELS'),wire_rows)))
        path=folder/'telemetry.jsonl';path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        paths.append(path);witness_sets.append(witnesses)
    (paths[0].parent/'SEALED_REFERENCE.json').write_text(json.dumps(ref))
    samples=[]
    for offset,witness in zip((0.,.1,.2,.4,.6),witness_sets[0]):
        point=decode_sample(witness)
        i=0 if offset<=.2 else 1 if offset<=.4 else 2
        a,e,t,r=([0.,0.,0.,0.] if offset==0 else plan[i]['u_cmd'])
        sample={'offset_s':offset,'mode':'FBWA','manual_control':[round(e*1000),round(a*1000),0,round(r*1000)],
                'pwm':point['pwm'],'plant_state':point['plant_state'],'plant_time_s':point['stamps']['plant_state'],
                'rc_time_s':point['stamps']['manual_control'],'pwm_time_s':point['stamps']['post_mixer_pwm']}
        samples.append(output_row(sample,sha(paths[0])))
    samples[-1]['offset_s']=3*.2
    prediction={'schema':PREDICTION_SCHEMA,'request_sha256':digest(request),'status':'PREDICTED','reason':'UNIT_ONLY',
                'samples':[samples[i] for i in (0,2,3,4)],
                'isolation':{'branch_session_id':ref['branch_session_id'],'initial_reference_sha256':digest(ref),
                             'active_mission_advanced':False,'trace_sha256':sha(paths[0])},
                'uncertainty':{'kind':'unbounded','reason':'UNIT unobservable internal state'}}
    evidence={'request_sha256':digest(request),'prediction_sha256':digest(prediction),'qualified':False,'flight_authorized':False,
              'policy':POLICY,'first_move':samples[1],'forecast_horizon_s':3*.2,
              'trace_sha256':sha(paths[0]),'coordinate_origin':ref['coordinate_origin'],
              'source_time_witnesses':witness_sets[0]}
    other=deepcopy(ref);other['branch_session_id']='independent-unit-branch'
    (paths[1].parent/'SEALED_REFERENCE.json').write_text(json.dumps(other))
    observation={'schema':'TDDT-independent-native-observation-v1','provider_or_forecast_called':False,
                 'branch_session_id':other['branch_session_id'],'reference':other,'mode':'FBWA',
                 'pilot_plan':plan,'candidate_hash':request['context']['candidate_hash'],'prediction_dt_s':.2,'commit_segment_s':.1,
                 'trace_sha256':sha(paths[1])}
    return request,prediction,evidence,observation,paths


def validate(doc, policy=None):
    a,b,c,d,paths=doc
    return validate_native_pair(a,b,c,d,prediction_trace=paths[0],observation_trace=paths[1],policy=policy or validation_policy())


def test_independent_raw_pair_and_unbounded_state_are_distinct(tmp_path):
    result=validate(documents(tmp_path))
    assert result['status']=='CONSISTENT_IN_EXPERIMENTAL_SCOPE',result
    assert len(result['residuals'])==5 and all(not r['violations'] for r in result['residuals'])
    assert not result['bounded_error_model_qualified'] and not result['flight_authorized']


def test_unbounded_uncertainty_does_not_make_bad_physics_pass(tmp_path):
    result=validate(documents(tmp_path,position_offset=.04))
    assert result['status']=='REJECTED' and result['reason']=='finite residual budget exceeded'
    assert any('position_norm_m' in r['violations'] for r in result['residuals'])


def test_quaternion_sign_is_not_an_attitude_error(tmp_path):
    assert validate(documents(tmp_path,quaternion_sign=-1.))['status']=='CONSISTENT_IN_EXPERIMENTAL_SCOPE'


@pytest.mark.parametrize('change',['session','plan','mode','parameters','origin','trace_hash','source_time','first_PWM','fake_qualification','fake_mode','duplicate_trace','policy'])
def test_bad_correlations_and_fabricated_samples_rejected(tmp_path,change):
    doc=list(documents(tmp_path));r,p,e,o,paths=doc
    policy=validation_policy()
    if change=='session':o['branch_session_id']=p['isolation']['branch_session_id']
    if change=='plan':o['pilot_plan']=deepcopy(o['pilot_plan']);o['pilot_plan'][0]['u_cmd'][0]=.5
    if change=='mode':o['mode']='QHOVER'
    if change=='parameters':
        o['reference']['backend']['parameters']['SERVO1_TRIM']+=1
        o['reference']['backend']['parameter_snapshot_sha256']=digest(o['reference']['backend']['parameters'])
    if change=='origin':o['reference']['coordinate_origin']['message']['latitude']+=1
    if change=='trace_hash':o['trace_sha256']='0'*64
    if change=='source_time':e['first_move']['channels']['plant_state']['source']['source_time_s']+=.01
    if change=='first_PWM':e['first_move']['channels']['post_mixer_pwm']['value'][0]+=150
    if change=='fake_qualification':e['qualified']=True
    if change=='fake_mode':p['samples'][1]['mode']='QHOVER';e['prediction_sha256']=digest(p)
    if change=='duplicate_trace':paths[1]=paths[0];o['trace_sha256']=sha(paths[0])
    if change=='policy':policy['limits']['PWM_max_abs_us']=1e9
    result=validate(doc,policy)
    assert result['status']=='REJECTED',result


def test_raw_crc_and_published_payload_both_checked():
    row=raw_row(mav.MAVLink(None).local_position_ned_encode(1000,1.,2.,3.,0.,0.,0.))
    assert decode_row(row)['x']==1.
    corrupt=deepcopy(row);corrupt['message']['x']=0.
    with pytest.raises(ValueError,match='payload'):decode_row(corrupt)
    corrupt=deepcopy(row);raw=bytearray.fromhex(corrupt['raw_MAVLink_hex']);raw[-1]^=1;corrupt['raw_MAVLink_hex']=raw.hex()
    with pytest.raises(Exception):decode_row(corrupt)


def test_invalid_quaternion_not_normalized_into_acceptance():
    with pytest.raises(ValueError,match='norm'):quaternion_distance([2.,0.,0.,0.],[1.,0.,0.,0.])
    assert quaternion_distance([1.,0.,0.,0.],[-1.,0.,0.,0.])==0.


def test_public_validator_rejects_corrupt_crc_without_raising(tmp_path):
    doc=documents(tmp_path)
    path=doc[-1][1]
    rows=[json.loads(line) for line in path.read_text().splitlines()]
    raw=bytearray.fromhex(rows[1]['raw_MAVLink_hex']);raw[-1]^=1
    rows[1]['raw_MAVLink_hex']=raw.hex()
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    result=validate(doc)
    assert result['status']=='REJECTED' and 'frame/CRC' in result['reason']


def test_policy_has_finite_budgets_and_cannot_be_mutated_globally():
    a=validation_policy();a['limits']['position_norm_m']=1000
    b=validation_policy()
    assert b['limits']['position_norm_m']==.02 and b['limits']['PWM_max_abs_us']==100.
    assert not b['bounded_model_qualification']
