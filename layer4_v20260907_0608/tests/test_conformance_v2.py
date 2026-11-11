import json
import numpy as np
from layer4 import Layer4Engine
from layer4.snapshot import Layer3SnapshotAdapter
from layer4.topology import DFATopologyManager
from layer4.knowledge import KnowledgeContext
from layer4.scenario import ScenarioManager
from layer4.explicit import ExplicitParetoPolicy
from layer4.state import StateCodec
from layer4.qlpv import QLPVModel
from layer4.uq import SquareRootCubatureUQ
from layer4.tube import PeriodicTubeMPC
from layer4.rom import PODDEIMSurrogate
from layer4.dependency import check_dependencies


def _snap(root,cfg):
    return Layer3SnapshotAdapter(cfg).adapt(json.loads((root/'examples/layer3_snapshot_example.json').read_text()))


def _run(root,cfg,tmp_path):
    cfg.data['registry']['path']=str(tmp_path/'cand.jsonl'); cfg.data['registry']['provenance_path']=str(tmp_path/'prov.jsonl')
    return Layer4Engine(cfg).run(json.loads((root/'examples/layer3_snapshot_example.json').read_text()))


def test_scenario_has_bounded_dynamic_topology_and_coupling_modes(root,cfg):
    s=_snap(root,cfg); ex=ExplicitParetoPolicy(cfg); sm=ScenarioManager(cfg,DFATopologyManager(cfg),KnowledgeContext(cfg),ex)
    raw=sm.generate(s)
    assert len(raw)<=cfg.get('edge.raw_scenarios_max')
    assert any(x.parameters.get('coupling_mode')!='nominal_coupling' for x in raw)
    degraded=[x for x in raw if x.topology_mode!='G_nominal']
    assert degraded and any(x.topology_sequence[0]!=x.topology_sequence[-1] for x in degraded)


def test_square_root_uq_reconstructs_covariance(root,cfg):
    s=_snap(root,cfg); codec=StateCodec(root/'resources/state_schema.json'); model=QLPVModel(cfg,codec); uq=SquareRootCubatureUQ(cfg,codec,model)
    C=uq.initial_covariance(s); S=uq.initial_sqrt(s)
    assert np.allclose(C,S@S.T,rtol=1e-6,atol=1e-6)
    x=codec.encode(s); y,Sp=uq.propagate_step_sqrt(x,S,np.zeros(5),np.zeros(6),s.weights,s.couplings,s.topology)
    assert y.shape==x.shape and Sp.shape==C.shape and np.linalg.eigvalsh(Sp@Sp.T).min()>-1e-8


def test_tube_exposes_tightening_and_terminal_diagnostic(root,cfg):
    s=_snap(root,cfg); codec=StateCodec(root/'resources/state_schema.json'); model=QLPVModel(cfg,codec); tube=PeriodicTubeMPC(cfg,model); H=cfg.horizon
    u,x,r,meta=tube.optimize(codec.encode(s),np.zeros((H,5)),np.zeros((H,6)),[s.weights]*H,[s.couplings]*H,s.topology,return_meta=True)
    assert len(meta['tightened_action_bounds'])==H
    assert 'contraction_ratio' in meta['terminal'] or 'reason' in meta['terminal']
    for b in meta['tightened_action_bounds']:
        assert np.all(np.asarray(b['lower'])<=np.asarray(b['upper']))


def test_pod_deim_is_active(root,cfg):
    rom=PODDEIMSurrogate(cfg); out=rom.screen(np.zeros((4,19)))
    assert out['deim_used'] is True and len(out['deim_indices'])>0


def test_layer4_details_candidate_contract(root,cfg,tmp_path):
    r=_run(root,cfg,tmp_path); assert r.candidates
    c=r.to_dict()['candidates'][0]
    required={'candidate_id','snapshot_version','scenario_id','predicted_trajectory','predicted_uncertainty','predicted_twin_weights','predicted_couplings','predicted_consistency','predicted_network_margin','mission_cost','time_cost','energy_cost','risk_cost','uncertainty_cost','consistency_cost','feasible','chance_constraint_risk','verified','pareto_rank','provenance'}
    assert required.issubset(c)


def test_network_state_and_no_commit_acceptance(root,cfg,tmp_path):
    r=_run(root,cfg,tmp_path)
    assert r.actuator_command is False and r.network_commit is False
    assert all(all({'W','K','G','C_star','J_C','mu'}.issubset(n) for n in c['network_trajectory']) for c in r.candidates)
    assert all(c['snapshot_version']==r.snapshot_version and c['snapshot_hash']==r.snapshot_hash for c in r.candidates)


def test_robust_worst_case_summary(root,cfg,tmp_path):
    r=_run(root,cfg,tmp_path)
    assert r.robust_summary['families']
    assert all('worst_case_scenario_id' in f and 'robust_score' in f for f in r.robust_summary['families'])


def test_all_three_edge_budgets_are_checked(root,cfg,tmp_path):
    r=_run(root,cfg,tmp_path); b=r.budget
    assert {'within_cpu_budget','within_memory_budget','within_energy_budget','all_within_budget'}.issubset(b)
    assert r.conformance['C_bounded']==b['all_within_budget']


def test_environment_dependencies_have_runtime_usage_metadata(root,cfg):
    dep=check_dependencies(cfg)
    names={x['name'] for x in dep['dependencies']}
    assert names=={'TDDT_LAYER3_ROOT','TDDT_TWINS_ROOT','TDDT_CLIMATE_ROOT','TDDT_TDOC_ROOT','TDDT_SARG_PACKAGE_ROOT','TDDT_BOULDER_ROOT'}
    assert all(x['used_by'] for x in dep['dependencies'])


def test_external_datasets_not_reselected_in_layer4(root):
    text=(root/'src/layer4/knowledge.py').read_text()
    assert 'Layer3 guidance first' in text
    assert 'argmin' not in text and 'Select(' not in text


def test_snapshot_accepts_meso_or_slow_boundary(root,cfg):
    import copy
    d=json.loads((root/'examples/layer3_snapshot_example.json').read_text())
    for keep in ('meso_index','slow_index'):
        x=copy.deepcopy(d); prov=x['provenance']; sched=prov.get('time_scheduler',{})
        for k in ('fast_index','meso_index','slow_index'):
            if k!=keep:
                prov.pop(k,None); sched.pop(k,None)
        assert Layer3SnapshotAdapter(cfg).adapt(x).version==str(x['policy_version'])


def test_tube_state_tightening_can_reject_nominal_boundary(root,cfg):
    from layer4.feasibility import FeasibilityEngine
    s=_snap(root,cfg); codec=StateCodec(root/'resources/state_schema.json'); fe=FeasibilityEngine(cfg,codec)
    x=codec.encode(s); H=cfg.horizon; X=np.tile(x,(H+1,1));
    mode=str((s.context or {}).get('flight_mode') or (s.context or {}).get('stage') or 'cruise').lower(); amin=fe.vtol.limits(mode)['min_airspeed_m_s']
    X[:,codec.index['airspeed_m_s']]=amin+0.01
    net=[{'J_C':0.0,'mu':1.0,'W':s.weights,'K':s.couplings} for _ in range(H+1)]
    tube=np.zeros_like(X); tube[:,codec.index['airspeed_m_s']]=0.02
    out=fe.evaluate_trajectory(X,net,s,actions=np.zeros((H,5)),tube=tube)
    assert not out['feasible'] and 'airspeed' in out['reasons'] and out['robust_state_tightening']
