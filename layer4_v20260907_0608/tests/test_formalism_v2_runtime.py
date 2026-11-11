import json
import numpy as np
from layer4.snapshot import Layer3SnapshotAdapter
from layer4.state import StateCodec
from layer4.qlpv import QLPVModel
from layer4.uq import SquareRootCubatureUQ
from layer4.multifidelity import CertifiedMultiFidelityManager
from layer4.engine import Layer4Engine
from layer4.models import Candidate


def _snap(root,cfg):
    return Layer3SnapshotAdapter(cfg).adapt(json.loads((root/'examples/layer3_snapshot_example.json').read_text()))


def test_v2_affine_uq_preserves_psd(root,cfg):
    s=_snap(root,cfg); codec=StateCodec(root/'resources/state_schema.json'); model=QLPVModel(cfg,codec); uq=SquareRootCubatureUQ(cfg,codec,model)
    x=codec.encode(s); S=uq.initial_sqrt(s); y,Sp=uq.propagate_step_sqrt(x,S,np.zeros(5),np.zeros(6),s.weights,s.couplings,s.topology)
    assert y.shape==x.shape and Sp.shape==S.shape
    assert np.linalg.eigvalsh(Sp@Sp.T).min()>-1e-8


def test_v2_certificate_manager_never_builds_without_real_ff(root,cfg,tmp_path):
    cfg.data['verification']['certificate_bank_path']=str(tmp_path/'certs.jsonl')
    mf=CertifiedMultiFidelityManager(cfg); s=_snap(root,cfg)
    c=Candidate('C','S',s.version,s.hash,1.0,[[0.0]*19],[[[0.0]*19]*19],[{}],[[0.0]*5],{'mission':0})
    assert mf.build(c,s,None,'m','p',{'verified':True,'candidate_verified':True,'executed':False}) is None
    assert mf.find(c,s,None,'m','p') is None


def test_v2_certificate_reuse_is_phase_and_origin_gated(root,cfg,tmp_path):
    cfg.data['verification']['certificate_bank_path']=str(tmp_path/'certs.jsonl'); cfg.data['verification']['certificate_state_tolerance']=0.0; cfg.data['verification']['certificate_action_tolerance']=0.0
    mf=CertifiedMultiFidelityManager(cfg); s=_snap(root,cfg)
    c=Candidate('C','S',s.version,s.hash,1.0,[[0.0]*19],[[[0.0]*19]*19],[{}],[[0.0]*5],{'mission':0})
    cert=mf.build(c,s,None,'m','p',{'verified':True,'candidate_verified':True,'executed':True,'reason':'verified'})
    assert cert and cert['origin']=='FF_CURRENT'
    assert mf.find(c,s,None,'m','p')['certificate_id']==cert['certificate_id']
    changed=Candidate(**{**c.__dict__}); changed.actions=[list(a) for a in c.actions]; changed.actions[0][0]+=1e-12
    assert mf.find(changed,s,None,'m','p') is None


def test_parameter_identity_excludes_ephemeral_registry_paths(cfg):
    a=Layer4Engine._semantic_parameter_payload(cfg)
    cfg.data.setdefault('registry',{})['path']='/tmp/random-process-a/candidates.jsonl'
    cfg.data['registry']['provenance_path']='/tmp/random-process-a/provenance.jsonl'
    cfg.data.setdefault('verification',{})['certificate_reuse_enabled']=False
    b=Layer4Engine._semantic_parameter_payload(cfg)
    assert a==b
    cfg.data['feasibility']['alpha_risk']=0.123
    assert Layer4Engine._semantic_parameter_payload(cfg)!=a
