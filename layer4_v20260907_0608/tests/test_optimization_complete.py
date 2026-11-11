import copy, json
import numpy as np

from layer4 import Layer4Config, Layer4Engine
from layer4.snapshot import Layer3SnapshotAdapter
from layer4.state import StateCodec
from layer4.qlpv import QLPVModel
from layer4.tube import PeriodicTubeMPC
from layer4.explicit import ExplicitParetoPolicy


def _engine_setup(root, cfg, data=None, robust_n=2):
    eng=Layer4Engine(cfg)
    d=data or json.loads((root/'examples/layer3_snapshot_example.json').read_text())
    snap=eng.adapter.adapt(d); eng.model.set_context(snap.context)
    x0=eng.codec.encode(snap); C0=eng.uq.initial_covariance(snap)
    raw=eng.scenarios.generate(snap); ups=eng.scenarios.uncertainty_pool(raw,n=robust_n)
    cases=[eng._make_case(snap,s,x0,C0) for s in ups]
    lo,hi,_=eng.tube.robust_intersection_bounds(cases,cfg.horizon)
    project=lambda U:eng.tube.project(U,lo,hi)
    return eng,snap,x0,C0,raw,cases,project


def test_periodic_terminal_rpi_certificate_is_real(root,cfg):
    s=Layer3SnapshotAdapter(cfg).adapt(json.loads((root/'examples/layer3_snapshot_example.json').read_text()))
    codec=StateCodec(root/'resources/state_schema.json'); model=QLPVModel(cfg,codec); tube=PeriodicTubeMPC(cfg,model)
    cert=tube.terminal_certificate([s.weights]*cfg.horizon,[s.couplings]*cfg.horizon,s.topology)
    assert cert['certified'] is True
    assert cert['method']=='periodic_box_RPI_fixed_point'
    assert cert['spectral_radius_per_step']<1.0 and cert['closure_error']<1e-6
    assert all(cert['containment'])


def test_objective_weights_change_optimized_actions(root,cfg):
    cfg.data['optimization']['iterations']=3
    eng,snap,x0,C0,raw,cases,project=_engine_setup(root,cfg)
    seed=copy.deepcopy(raw[0]); U0=project(np.asarray(seed.action_sequence,float))
    seed.objectives={'mission':1.0}
    Um,_,_=eng.optimizer.optimize(U0,project,eng._robust_evaluator(snap,seed,cases,x0,C0))
    seed.objectives={'energy':1.0}
    Ue,_,_=eng.optimizer.optimize(U0,project,eng._robust_evaluator(snap,seed,cases,x0,C0))
    assert np.linalg.norm(Um-Ue)>1e-3


def test_mission_target_changes_mission_optimized_actions(root,cfg):
    cfg.data['optimization']['iterations']=3
    d1=json.loads((root/'examples/layer3_snapshot_example.json').read_text())
    eng1,s1,x01,C01,raw1,cases1,project1=_engine_setup(root,cfg,d1)
    seed1=copy.deepcopy(raw1[0]); seed1.objectives={'mission':1.0}; U01=project1(np.asarray(seed1.action_sequence,float))
    U1,_,_=eng1.optimizer.optimize(U01,project1,eng1._robust_evaluator(s1,seed1,cases1,x01,C01))

    d2=copy.deepcopy(d1); d2['context']['scenario_destination_enu_m']['value']=[-5000.0,-5000.0,500.0]
    cfg2=Layer4Config.from_yaml(root/'configs/configuration_boulder.yaml'); cfg2.data['optimization']['iterations']=3
    eng2,s2,x02,C02,raw2,cases2,project2=_engine_setup(root,cfg2,d2)
    seed2=copy.deepcopy(raw2[0]); seed2.objectives={'mission':1.0}; U02=project2(np.asarray(seed2.action_sequence,float))
    U2,_,_=eng2.optimizer.optimize(U02,project2,eng2._robust_evaluator(s2,seed2,cases2,x02,C02))
    assert np.linalg.norm(U1-U2)>1e-3


def test_engine_reports_in_loop_robust_chance_and_objectives(root,cfg,tmp_path):
    cfg.data['registry']['path']=str(tmp_path/'c.jsonl'); cfg.data['registry']['provenance_path']=str(tmp_path/'p.jsonl')
    r=Layer4Engine(cfg).run(json.loads((root/'examples/layer3_snapshot_example.json').read_text()))
    assert r.conformance['C_optimizer'] and r.conformance['C_robust'] and r.conformance['C_chance'] and r.conformance['C_terminal']
    for c in r.candidates:
        o=c['provenance']['optimization']
        assert o['objective_in_loop'] and o['chance_constraint_in_loop'] and o['robust_minmax_in_loop'] and o['terminal_constraint_in_loop']
        assert c['violation_probability']==o['final_robust_evaluation']['chance_probability']


def test_reference_explicit_pwa_region_is_nonempty(root,cfg):
    ex=ExplicitParetoPolicy(cfg); status=ex.status()
    assert status['validated'] is True and status['region_count']>=1
    s=Layer3SnapshotAdapter(cfg).adapt(json.loads((root/'examples/layer3_snapshot_example.json').read_text()))
    props=ex.proposals(s)
    assert props and props[0]['validated'] is True and len(props[0]['vector'])==cfg.get('qlpv.action_dim')
