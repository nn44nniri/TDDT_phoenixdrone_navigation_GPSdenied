import copy, json
import numpy as np
from layer4.snapshot import Layer3SnapshotAdapter
from layer4.multifidelity import CertifiedMultiFidelityManager
from layer4.models import Candidate, Scenario


def _snap(root,cfg,ref=None):
    raw=json.loads((root/'examples/layer3_snapshot_example.json').read_text())
    if ref is not None: raw['reference_time_s']=float(ref)
    return Layer3SnapshotAdapter(cfg).adapt(raw)


def _scenario(sid='S1'):
    return Scenario(sid,1.0,'G_nominal',{'nodes':['MESA_NAV','WingMPET','Climate']},[[0.1,0,0,0,0]],[[0]*6],[],
        {'disturbance_name':'nominal','degradation_name':'none','coupling_mode':'nominal','coupling_delta':{},'model_parameters':{}},
        {'safe':True},{'mission':1.0},{})


def _candidate(s,sid='S1',x0=None,actions=None,sigma=0.2):
    x=list(x0 if x0 is not None else [0.0]*19)
    cov=np.eye(19)*(sigma**2)
    return Candidate('C-'+sid,sid,s.version,s.hash,1.0,[x],[cov.tolist()],[],actions or [[0.1,0,0,0,0]],{'mission':0})


def _manager(cfg,tmp_path):
    cfg.data['verification']['certificate_bank_path']=str(tmp_path/'certs.jsonl')
    return CertifiedMultiFidelityManager(cfg)


def test_v4_uses_mission_reference_not_wall_clock(root,cfg,tmp_path):
    s=_snap(root,cfg,100.0); mf=_manager(cfg,tmp_path); c=_candidate(s); sc=_scenario()
    cert=mf.build(c,s,sc,'model','param',{'verified':True,'candidate_verified':True,'executed':True})
    rows=[json.loads(x) for x in (tmp_path/'certs.jsonl').read_text().splitlines()]
    rows[0]['created_unix_s']=1.0
    (tmp_path/'certs.jsonl').write_text(json.dumps(rows[0])+'\n'); mf._cache=None
    assert mf.find(_candidate(s,'S2'),s,_scenario('S2'),'model','param') is not None
    s_late=_snap(root,cfg,100.0+cert['freshness_s']+0.1)
    assert mf.find(_candidate(s_late,'S3'),s_late,_scenario('S3'),'model','param') is None
    # Exact-positive authority binds mission reference time; the implementation
    # may reject at the exact context index before the legacy freshness check.
    assert sum(mf.reuse_stats()['misses'].values())>=1


def test_state_envelope_is_metadata_not_positive_reuse_authority(root,cfg,tmp_path):
    s=_snap(root,cfg,10); mf=_manager(cfg,tmp_path); sc=_scenario(); c=_candidate(s,sigma=0.2)
    cert=mf.build(c,s,sc,'model','param',{'verified':True,'candidate_verified':True,'executed':True})
    # The uncertainty-aware envelope is retained as diagnostic metadata, but
    # positive authority is exact semantic replay and must reject any change.
    assert cert['state_envelope']['sigma_multiplier'] == 3.0
    x=[0.0]*19; x[0]=0.20
    assert mf.find(_candidate(s,'S2',x0=x,sigma=0.2),s,_scenario('S2'),'model','param') is None
    x[0]=0.80
    assert mf.find(_candidate(s,'S3',x0=x,sigma=0.2),s,_scenario('S3'),'model','param') is None


def test_action_envelope_is_metadata_not_positive_reuse_authority(root,cfg,tmp_path):
    s=_snap(root,cfg,10); mf=_manager(cfg,tmp_path); sc=_scenario(); c=_candidate(s,actions=[[0.1,0,0,0,0]])
    cert=mf.build(c,s,sc,'model','param',{'verified':True,'candidate_verified':True,'executed':True})
    assert cert['action_envelope']['slack_per_channel'][-1] > cert['action_tolerance']
    # The scaled envelope remains inspectable, but a changed action cannot
    # inherit verified=true from it under the exact-positive contract.
    assert mf.find(_candidate(s,'S2',actions=[[0.1,0,0,0,0.10]]),s,_scenario('S2'),'model','param') is None
    assert mf.find(_candidate(s,'S3',actions=[[0.1,0,0,0,0.30]]),s,_scenario('S3'),'model','param') is None
