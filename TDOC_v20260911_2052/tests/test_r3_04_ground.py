"""Acceptance and adversarial replays of a real native ground trace."""
import csv
import os
from pathlib import Path

import pytest

from tddt_tdoc import ground_contact_audit as audit

ROOT=Path(__file__).resolve().parents[1]
DEFAULT=ROOT/'reports/online/R3_04_20261029_ground_v4/ground_suite/native_reset/ground.csv'


@pytest.fixture(scope='module')
def native_trace():
    path=Path(os.environ.get('R304_GROUND_TRACE',str(DEFAULT)))
    if not path.is_file():
        pytest.fail('run audit_r3_04_ground.py first or set R304_GROUND_TRACE to its native_reset/ground.csv')
    return path


def test_actual_measured_ground_trace_supports_weight_on_all_feet(native_trace):
    result=audit.analyze(native_trace)
    assert result['status']=='PASS' and not result['violations']
    assert result['metrics']['mean_support_up_N']>6.
    assert all(foot['contact_duty']==1 and foot['min_compression_force_N']>1.5 for foot in result['feet'].values())
    assert not result['flight_ready'] and not result['actuation_authority']


@pytest.mark.parametrize('fault,reason',[
    ('empty','empty native ground'),('nan','non-finite'),('missing','missing'),('duplicate','duplicate'),
    ('reverse','TIME_GAP_OR_REVERSAL'),('gap','TIME_GAP_OR_REVERSAL'),
    ('collapse','CG_COLLAPSE'),('force','UNSUPPORTED_WEIGHT'),
    ('motor','MOTOR_NOT_ZERO'),('contact','UNSUPPORTED_FOOT:TAIL_FOOT_L1'),
    ('compression','UNMODELED_PENETRATION')])
def test_corrupted_or_unsafe_native_replays_do_not_pass(native_trace,tmp_path,fault,reason):
    with native_trace.open(newline='') as stream:
        data=list(csv.reader(stream))
    headers=[h.strip().removeprefix('/fdm/jsbsim/') for h in data[0]]
    index=lambda key:headers.index(key)
    if fault=='empty':data=[]
    elif fault=='nan':data[-1][index('attitude/theta-rad')]='nan'
    elif fault=='missing':
        k=index('forces/fbx-gear-lbs')
        for row in data:row.pop(k)
    elif fault=='duplicate':data[0][-1]=data[0][-2]
    elif fault=='reverse':data[-1][index('Time')]=data[-2][index('Time')]
    elif fault=='gap':data.pop(200)
    elif fault=='collapse':data[-1][index('position/h-agl-ft')]='-1'
    elif fault=='force':data[-1][index('forces/fbx-gear-lbs')]='0'
    elif fault=='motor':data[-1][index('fcs/left-motor-pos-norm')]='0.1'
    elif fault=='contact':
        for row in data[-100:]:row[index('TAIL_FOOT_L1 WOW')]='0'
    elif fault=='compression':data[-1][index('TAIL_FOOT_L1 stroke (ft)')]='0'
    path=tmp_path/'corrupt.csv'
    with path.open('w',newline='') as stream:csv.writer(stream).writerows(data)
    if fault in ('empty','nan','missing','duplicate'):
        with pytest.raises(ValueError,match=reason):audit.analyze(path)
    else:
        result=audit.analyze(path)
        assert result['status']=='FAIL' and reason in result['violations']


def test_policy_identity_cannot_be_silently_replaced(monkeypatch):
    monkeypatch.setattr(audit,'POLICY_SHA256','0'*64)
    with pytest.raises(ValueError,match='policy identity mismatch'):audit.policy()
