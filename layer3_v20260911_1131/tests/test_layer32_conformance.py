from __future__ import annotations
import copy
from pathlib import Path

from layer3 import Layer3Engine, Layer3FMURuntime, RuntimeRequest
from layer3.emergence import HiddenLoopMonitor
from test_layer3 import synthetic_packets
from conftest import strict_climate, strict_context, strict_anchor

LAT,LON=40.017498782173554,-105.28999780562658

def req(*,context=None,safety_gates=None,reference_time_s=.001,climate=None):
    return RuntimeRequest(
        reference_time_s=reference_time_s,regime_id=3,stage='cruise',
        climate=climate or strict_climate(LAT,LON),
        context=context or strict_context(),
        safety_gates=safety_gates or {},
        mesa_anchor=strict_anchor(),
    )

def test_coupling_execution_is_after_plan_and_safety_gate_controls_fmu(cfg_factory):
    cfg=cfg_factory()
    gate={'MicroMetTwin->WingMPET':False}
    with Layer3FMURuntime(cfg) as rt:
        r=rt.step(req(safety_gates=gate))
    ctrl=r.snapshot.std['control_status']['runtime_couplings']
    assert r.fast_record['provenance']['two_phase_coupling']
    assert r.fast_record['provenance']['plan_precedes_destination_execution']
    assert r.snapshot.topology['edges']['MicroMetTwin->WingMPET']['safety_gate']==0
    assert r.snapshot.topology['edges']['MicroMetTwin->WingMPET']['active_effective']==0
    assert 'Climate->WingMPET:op5' not in ctrl['operations']
    assert 'Climate+MESA->WingMPET:op6' not in ctrl['operations']


def test_delay_readiness_directly_controls_destination_execution(cfg_factory):
    cfg=cfg_factory()
    edge=next(e for e in cfg.network.edges if e.key=='MicroMetTwin->WingMPET')
    edge.delay_ticks=1
    with Layer3FMURuntime(cfg) as rt:
        r1=rt.step(req(reference_time_s=.001))
        r2=rt.step(req(reference_time_s=1.001))
    o1=r1.snapshot.std['control_status']['runtime_couplings']['operations']
    o2=r2.snapshot.std['control_status']['runtime_couplings']['operations']
    assert 'Climate->WingMPET:op5' not in o1
    assert 'Climate->WingMPET:op5' in o2 and o2['Climate->WingMPET:op5']['executed']
    assert r1.fast_record['coupling_effects']['edges']['MicroMetTwin->WingMPET']['delay_ready'] is False
    assert r2.fast_record['coupling_effects']['edges']['MicroMetTwin->WingMPET']['delay_ready'] is True


def test_coupled_fmu_output_enters_destination_packet_same_tick(cfg_factory):
    cfg=cfg_factory()
    with Layer3FMURuntime(cfg) as rt:
        r=rt.step(req())
    wing=r.fast_record['packets']['WingMPET']
    updates=wing['provenance'].get('layer3_coupled_updates') or []
    assert any('Climate->WingMPET:op5' in x for x in updates)
    assert 'air_temperature' in wing['state']
    assert r.fast_record['std']['representations']['WingMPET']['raw_state'].get('air_temperature') is not None


def test_registry_alignment_stability_and_coc_accept_fourth_twin(cfg_factory):
    cfg=cfg_factory(); eng=Layer3Engine(cfg)
    eng.registry.register('DT4',adapter='generic',required=False,
        coc_observations=[{'key':'aux','shared_dimensions':['Aux_X','Aux_Y'],'P':[[1.,0.],[0.,1.]]}])
    packets=synthetic_packets()
    packets['DT4']={
        'twin_id':'DT4','state':{'dummy':1.},'canonical_state':{'aux':[4.,5.]},
        'uncertainty':{'by_output':{'dummy':.1}},'quality':{'score':.9},'validity':{'score':1.},
        'reliability':{'prior':.9,'posterior':.9},'context':{},'timestamp':0.,
        'provenance':{'layer3_adapter':'generic'},
    }
    r=eng.step(packets,reference_time_s=0.,regime_id=3,stage='cruise')
    assert 'DT4' in r.snapshot.registry
    assert 'DT4' in r.snapshot.std['representations']
    assert 'DT4' in r.snapshot.stability['node_order']
    assert abs(r.snapshot.center_of_consistency['dimensions']['Aux_X']-4.)<1e-4
    assert abs(r.snapshot.center_of_consistency['dimensions']['Aux_Y']-5.)<1e-4


def test_coc_supports_explicit_projection_Pi_and_constraint_registry(cfg_factory):
    cfg=cfg_factory(); eng=Layer3Engine(cfg)
    eng.registry.register('DT4',adapter='generic',required=False,
        coc_observations=[{'key':'sum_ab','shared_dimensions':['A','B'],'P':[[1.,1.]]}])
    packets=synthetic_packets()
    packets['DT4']={
        'twin_id':'DT4','state':{'dummy':1.},'canonical_state':{'sum_ab':[3.]},
        'uncertainty':{'by_output':{'dummy':.1}},'quality':{'score':.95},'validity':{'score':1.},
        'reliability':{'prior':.95,'posterior':.95},'context':{},'timestamp':0.,'provenance':{}
    }
    r=eng.step(packets,reference_time_s=0.,regime_id=3,stage='cruise',constraints={'coc_constraints':[
        {'name':'diff_ab','type':'linear','hard':True,'coefficients':{'A':1.,'B':-1.},'rhs':1.}
    ]})
    dims=r.snapshot.center_of_consistency['dimensions']
    assert abs(dims['A']-2.)<1e-4 and abs(dims['B']-1.)<1e-4
    assert 'diff_ab' in r.fast_record['constraint_residuals']
    assert r.fast_record['constraint_residuals']['diff_ab']['hard'] is True
    assert hasattr(eng.consistency.constraints,'register')


def test_partial_coc_is_never_reported_as_perfect_inside(cfg_factory):
    cfg=cfg_factory(); eng=Layer3Engine(cfg)
    packets=synthetic_packets(); packets.pop('MicroMetTwin')
    plan=eng.plan_network(packets,reference_time_s=0.,context={})
    coc=plan['pre_coupling_coc']
    assert coc['status'] in {'partial_solved','unassessed'}
    assert coc['consistency_region']['inside'] is None
    assert coc['consistency_margin'] is None
    assert coc.get('missing_expected_observations') or coc['status']=='unassessed'


def test_dynamic_topology_and_top_b_cut_removable_edges(cfg_factory):
    cfg=cfg_factory(); cfg.network.top_b_per_node=1; eng=Layer3Engine(cfg)
    plan=eng.plan_network(synthetic_packets(),reference_time_s=0.,context={})
    edges=plan['graph']['edges']
    dynamic_from_climate=[k for k,v in edges.items() if v['source']=='MicroMetTwin' and not v['mandatory']]
    assert len(dynamic_from_climate)>=2
    assert sum(int(edges[k]['active_effective']) for k in dynamic_from_climate)==1
    assert any(edges[k].get('budget_cut') for k in dynamic_from_climate)
    assert edges['MESA_NAV->WingMPET']['mandatory'] and edges['MESA_NAV->WingMPET']['active_effective']==1


def test_six_coupling_classes_have_independent_K_values(cfg_factory):
    cfg=cfg_factory(); eng=Layer3Engine(cfg)
    plan=eng.plan_network(synthetic_packets(),reference_time_s=0.,context={})
    rec=plan['coupling_details']['edges']['MicroMetTwin->WingMPET']
    assert rec['K_by_type']['Data']>0 and rec['K_by_type']['Model']>0
    assert rec['K_by_type']['Data'] != rec['K_by_type']['Model']
    assert rec['K_by_type']['State']==0 and rec['K_by_type']['Control']==0
    assert rec['K_vector_order']==['Data','Model','State','Error','Objective','Control']


def test_disabling_one_required_typed_K_blocks_composite_fmu_operator(cfg_factory):
    cfg=cfg_factory()
    edge=next(e for e in cfg.network.edges if e.key=='MicroMetTwin->WingMPET')
    edge.coupling_type_bounds['Data']=[0.0,0.0]
    with Layer3FMURuntime(cfg) as rt:
        r=rt.step(req())
    detail=r.fast_record['coupling_details']['edges']['MicroMetTwin->WingMPET']
    ops=r.snapshot.std['control_status']['runtime_couplings']['operations']
    assert detail['K_by_type']['Data']==0.0 and detail['K_by_type']['Model']>0.0
    assert 'Climate->WingMPET:op5' not in ops
    assert 'Climate+MESA->WingMPET:op6' not in ops


def test_mesa_to_climate_uses_actual_mesa_geodetic_query_when_origin_exists(cfg_factory):
    cfg=cfg_factory()
    base_lat,base_lon=LAT+.001,LON+.001
    context=strict_context(geodetic_origin=(LAT,LON,1800.0))
    climate=strict_climate(base_lat,base_lon)
    with Layer3FMURuntime(cfg) as rt:
        r=rt.step(req(context=context,climate=climate))
    op=r.snapshot.std['control_status']['runtime_couplings']['operations']['MESA_NAV->MicroMetTwin:query']
    assert op['executed'] and op['provenance']['geodetic_origin_available']
    assert not op['provenance']['geodetic_origin_inferred']
    assert op['provenance']['climate_contract_time_binding']=='mission_epoch_naive_iso'
    assert abs(op['query']['latitude']-base_lat)>1e-9 or abs(op['query']['longitude']-base_lon)>1e-9
    assert 'MESA_NAV->MicroMetTwin:query' in r.fast_record['packets']['MicroMetTwin']['provenance']['layer3_coupled_updates']


def test_actual_counterfactual_emergence_and_generic_observed_lci(cfg_factory):
    cfg=cfg_factory()
    context=strict_context(geodetic_origin=(LAT,LON,1800.0))
    with Layer3FMURuntime(cfg) as rt:
        r=rt.step(req(context=context))
    assert r.snapshot.emergence['actual_counterfactual_available']
    assert r.snapshot.emergence['counterfactual_method']=='actual_runtime_counterfactual'
    assert r.snapshot.hidden_loop['cycle_count']>=1
    assert r.snapshot.hidden_loop['observed_lci_available']

    # The cycle finder itself is not tied to the configured three-Twin profile.
    topo={'nodes':['A','B','C','D'],'active_edges':['A->B','B->C','C->A','C->D','D->C'],
          'edges':{k:{'delay_ticks':0} for k in ['A->B','B->C','C->A','C->D','D->C']}}
    cpl={k:.5 for k in topo['active_edges']}
    states={n:{'direct':{'x':0.},'observed':{'x':float(i)}} for i,n in enumerate(topo['nodes'])}
    h=HiddenLoopMonitor(cfg).evaluate(cpl,{},topo,counterfactual_states=states)
    assert any('D' in cyc['cycle'] for cyc in h['cycles'])
    assert h['observed_lci_available']


def test_partial_meso_aggregate_is_strict_json_serializable(cfg_factory):
    """Partial CoC assessment must use JSON null, never NaN, in Meso output."""
    import json
    from layer3 import Layer3Engine
    from layer3.runtime import RuntimeRequest, Layer3FMURuntime
    from pathlib import Path

    cfg = cfg_factory(meso=3, slow=2)
    base = json.loads((Path(__file__).resolve().parents[1] / "examples/runtime_request.json").read_text(encoding="utf-8"))
    results=[]
    with Layer3FMURuntime(cfg) as rt:
        for i in range(3):
            d=json.loads(json.dumps(base))
            d["reference_time_s"]=float(base.get("reference_time_s",0.001))+i*cfg.time.fast_dt_s
            results.append(rt.step(RuntimeRequest.from_mapping(d)).to_dict())
    assert results[-1]["meso_event"] is not None
    agg=results[-1]["meso_event"]["aggregate"]
    assert agg["mean_consistency_margin"] is None or isinstance(agg["mean_consistency_margin"], float)
    json.dumps(results, allow_nan=False)
