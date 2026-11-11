import json
from layer4 import Layer4Engine
from layer4.dependency import check_dependencies


def test_engine_end_to_end(root,cfg,tmp_path,monkeypatch):
    cfg.data['registry']['path']=str(tmp_path/'cand.jsonl'); cfg.data['registry']['provenance_path']=str(tmp_path/'prov.jsonl')
    d=json.loads((root/'examples/layer3_snapshot_example.json').read_text()); r=Layer4Engine(cfg).run(d)
    assert r.actuator_command is False and r.network_commit is False
    assert len(r.candidates)<=cfg.reduced_scenarios and len(r.candidates)>0
    assert all(c['snapshot_hash']==r.snapshot_hash for c in r.candidates)
    assert all(c['provenance']['not_actuator_command'] for c in r.candidates)
    assert r.conformance['C_atomic'] and r.conformance['C_no_commit'] and r.conformance['C_UQ']

def test_unverified_is_strict(root,cfg,tmp_path):
    cfg.data['registry']['path']=str(tmp_path/'c.jsonl'); cfg.data['registry']['provenance_path']=str(tmp_path/'p.jsonl')
    r=Layer4Engine(cfg).run(json.loads((root/'examples/layer3_snapshot_example.json').read_text()))
    assert all((not c['verified']) or c['verification'].get('executed') for c in r.candidates)

def test_network_future_fields(root,cfg,tmp_path):
    cfg.data['registry']['path']=str(tmp_path/'c.jsonl'); cfg.data['registry']['provenance_path']=str(tmp_path/'p.jsonl')
    r=Layer4Engine(cfg).run(json.loads((root/'examples/layer3_snapshot_example.json').read_text()))
    for c in r.candidates:
        assert len(c['network_trajectory'])==cfg.horizon+1
        assert all(set(('W','K','G','C_star','J_C','mu')).issubset(n) for n in c['network_trajectory'])

def test_budget_fields(root,cfg,tmp_path):
    cfg.data['registry']['path']=str(tmp_path/'c.jsonl'); cfg.data['registry']['provenance_path']=str(tmp_path/'p.jsonl')
    r=Layer4Engine(cfg).run(json.loads((root/'examples/layer3_snapshot_example.json').read_text()))
    assert r.budget['horizon']<=cfg.get('edge.horizon_max') and r.budget['reduced_scenarios']<=cfg.get('edge.reduced_scenarios_max')


def test_full_fidelity_chance_is_not_surrogate_reuse(root,cfg):
    from layer4.verification import FullFidelityVerifier
    from layer4.snapshot import Layer3SnapshotAdapter
    from layer4.state import StateCodec
    from layer4.models import Candidate
    snap=Layer3SnapshotAdapter(cfg).adapt(json.loads((root/'examples/layer3_snapshot_example.json').read_text()))
    codec=StateCodec(root/'resources/state_schema.json'); vf=FullFidelityVerifier(cfg,codec)
    cand=Candidate('C','S',snap.version,snap.hash,1.0,[],[],[],[[0,0,0,0,0]],{},feasible=True,violation_probability=0.0)
    rows=[{'step':1,'snapshot':{'std':{'representations':{'WingMPET':{'canonical_state':{'airspeed_m_s':999.0}},'MicroMetTwin':{'canonical_state':{'wind_ned_m_s':[0,0,0]}}}},'context':snap.context,'guidance':{'risk_signal':0.0}}}]
    ff=vf._full_feasibility(rows,cand,snap,None)
    assert ff['P_viol_full']==1.0 and not ff['safe']
