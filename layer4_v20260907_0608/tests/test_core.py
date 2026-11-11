import json,copy
import numpy as np
import pytest
from layer4.snapshot import Layer3SnapshotAdapter,SnapshotError
from layer4.state import StateCodec
from layer4.topology import DFATopologyManager
from layer4.knowledge import KnowledgeContext
from layer4.scenario import ScenarioManager
from layer4.trust import BetaTrustPredictor
from layer4.rls import FFRLSCouplingPredictor
from layer4.qlpv import QLPVModel
from layer4.tube import PeriodicTubeMPC
from layer4.uq import SquareRootCubatureUQ
from layer4.consistency import WLSConsistency
from layer4.pareto import ParetoEngine
from layer4.models import Candidate
from layer4.rom import PODDEIMSurrogate
from layer4.hashing import sha256_obj


def snap(root,cfg):
    d=json.loads((root/'examples/layer3_snapshot_example.json').read_text()); return Layer3SnapshotAdapter(cfg).adapt(d)

def test_snapshot_atomic(root,cfg):
    s=snap(root,cfg); assert s.version and len(s.hash)==64

def test_snapshot_stepresult_wrapper(root,cfg):
    d=json.loads((root/'examples/layer3_snapshot_example.json').read_text()); s=Layer3SnapshotAdapter(cfg).adapt({'snapshot':d,'fast_record':{}}); assert s.reference_time_s==d['reference_time_s']

def test_snapshot_missing_rejected(root,cfg):
    with pytest.raises(SnapshotError): Layer3SnapshotAdapter(cfg).adapt({'policy_version':'x'})

def test_hash_deterministic(): assert sha256_obj({'b':2,'a':1})==sha256_obj({'a':1,'b':2})

def test_state_codec(root,cfg):
    c=StateCodec(root/'resources/state_schema.json'); x=c.encode(snap(root,cfg)); assert c.dim==19 and x.shape==(19,) and np.isfinite(x).all()

def test_topology_bounded_and_mandatory(root,cfg):
    s=snap(root,cfg); modes=DFATopologyManager(cfg).modes(s); assert len(modes)<=cfg.max_topology_modes
    mandatory=[e for e,r in s.topology['edges'].items() if r.get('mandatory')]
    assert all(all(e in m.topology.get('active_edges',[]) for e in mandatory) for m in modes)

def test_scenario_reduction(root,cfg):
    s=snap(root,cfg); k=KnowledgeContext(cfg); t=DFATopologyManager(cfg); sm=ScenarioManager(cfg,t,k); raw=sm.generate(s); red=sm.reduce(raw); assert len(raw)>len(red)==cfg.reduced_scenarios; assert abs(sum(x.probability for x in red)-1)<1e-9

def test_beta_trust_normalized(root,cfg):
    s=snap(root,cfg); b=BetaTrustPredictor(cfg); st=b.initial(s); st,w=b.step(st,s,{'MESA_NAV':0.2}); assert abs(sum(w.values())-1)<1e-9 and all(0<=v<=1 for v in w.values())

def test_ffrls_bounded(root,cfg):
    s=snap(root,cfg); r=FFRLSCouplingPredictor(cfg); st=r.initial(s); st,k=r.step(st,s,s.weights,s.topology); assert set(k)==set(s.couplings) and all(0<=v<=1 for v in k.values())

def test_qlpv_vertex_weights(root,cfg):
    s=snap(root,cfg); c=StateCodec(root/'resources/state_schema.json'); m=QLPVModel(cfg,c); xi=m.weights(s.weights,s.couplings); assert xi.shape==(2,) and np.isclose(xi.sum(),1) and np.all(xi>=0)

def test_qlpv_step_dimensions(root,cfg):
    s=snap(root,cfg); c=StateCodec(root/'resources/state_schema.json'); m=QLPVModel(cfg,c); x=c.encode(s); y,_=m.step(x,np.zeros(5),np.zeros(6),s.weights,s.couplings,s.topology); assert y.shape==x.shape and np.isfinite(y).all()

def test_tube_bounds(root,cfg):
    s=snap(root,cfg); c=StateCodec(root/'resources/state_schema.json'); m=QLPVModel(cfg,c); t=PeriodicTubeMPC(cfg,m); H=cfg.horizon; W=[s.weights]*H; K=[s.couplings]*H; U=np.ones((H,5))*99; D=np.zeros((H,6)); u,x,r=t.optimize(c.encode(s),U,D,W,K,s.topology); assert np.max(u)<=2.0+1e-9 and r.shape==(H+1,c.dim) and np.all(r>=0)

def test_uq_psd(root,cfg):
    s=snap(root,cfg); c=StateCodec(root/'resources/state_schema.json'); m=QLPVModel(cfg,c); uq=SquareRootCubatureUQ(cfg,c,m); C=uq.initial_covariance(s); x=c.encode(s); y,Cp=uq.propagate_step(x,C,np.zeros(5),np.zeros(6),s.weights,s.couplings,s.topology); assert np.linalg.eigvalsh(Cp).min()>-1e-7

def test_wls_coc(root,cfg):
    s=snap(root,cfg); c=StateCodec(root/'resources/state_schema.json'); co=WLSConsistency(cfg,c); C=np.eye(c.dim); r=co.evaluate(c.encode(s),C,s.weights,s.topology); assert set(['mismatch','margin','residual']).issubset(r) and np.isfinite(r['mismatch'])

def _cand(i,obj,feasible=True): return Candidate(str(i),str(i),'v','h',0.5,[[0]],[[[1]]],[{}],[[0]],obj,feasible=feasible,violation_probability=0)

def test_pareto_dominance(cfg):
    p=ParetoEngine(cfg); a=_cand(1,{k:1 for k in cfg.get('objectives.normalization')}); b=_cand(2,{k:2 for k in cfg.get('objectives.normalization')}); f=p.rank([a,b]); assert a.pareto_rank==0 and b.pareto_rank==1

def test_rom_screen(root,cfg):
    r=PODDEIMSurrogate(cfg); x=np.zeros((4,19)); out=r.screen(x); assert out['valid'] is True and out['error']>=0
