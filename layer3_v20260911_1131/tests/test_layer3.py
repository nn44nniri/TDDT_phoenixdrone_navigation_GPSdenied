from __future__ import annotations
import math
from pathlib import Path
from layer3 import Layer3Engine
from layer3.knowledge import CRPRepository, SARGRepository
from layer3.weights import WeightManager

ROOT=Path(__file__).resolve().parents[1]

def packet(twin,state,t=0.,q=.9,v=1.,r=.9):
    return {"twin_id":twin,"state":state,"uncertainty":{"by_output":{k:.2 for k,x in state.items() if isinstance(x,(int,float))}},
            "quality":{"score":q},"validity":{"score":v},"reliability":{"prior":r,"posterior":r},
            "context":{},"timestamp":t,"provenance":{"test":True}}

def synthetic_packets(t=0.):
    return {
      "MESA_NAV":packet("MESA_NAV",{"velocity_n":10.,"velocity_e":2.,"velocity_d":0.,"position_n":0.,"position_e":0.,"position_d":-100.},t),
      "WingMPET":packet("WingMPET",{"airspeed_norm":math.sqrt(65.),"flightpath_angle":0.,"air_heading":0.1243549945},t),
      "MicroMetTwin":packet("MicroMetTwin",{"wind_u_3dw":1.,"wind_v_3dw":2.,"diagnostic_vertical_wind":0.,"temperature_local":15.,"relative_humidity_local":50.,"wind_direction_terrain_deg":243.,"surface_pressure_pa":82000.,"air_density":1.0},t),
    }

def test_bounded_simplex_preserves_bounds():
    x=WeightManager._bounded_simplex({'a':.99,'b':.005,'c':.005},{'a':[.01,.95],'b':[.01,.95],'c':[.01,.95]},{'a':1,'b':1,'c':1})
    assert abs(sum(x.values())-1.)<1e-10 and all(.01-1e-12<=v<=.95+1e-12 for v in x.values())

def test_crp_and_official_sarg_selector(cfg_factory):
    cfg=cfg_factory(); crp=CRPRepository(cfg)
    c=crp.clusters[0]
    ctx={f:c['centroid'][i] for i,f in enumerate(crp.feature_order)}
    m=crp.match(ctx)
    assert m['selected'] and m['matched'] and m['crp_id']==0
    ref=SARGRepository(cfg).select(3,'cruise',m['crp_id'],reference_admissible=m['matched'])
    assert ref['selected'] and ref['key']==[3,3,m['crp_id']] and ref['route_reference']['available']

def test_crp_ood_gate_keeps_nearest_but_blocks_reference(cfg_factory):
    cfg=cfg_factory(); crp=CRPRepository(cfg)
    ctx={f:1e9 for f in crp.feature_order}
    m=crp.match(ctx)
    assert m['selected'] and not m['matched'] and m['out_of_distribution']
    ref=SARGRepository(cfg).select(3,'cruise',m['crp_id'],reference_admissible=m['matched'])
    assert ref['selected'] and not ref['reference_admissible']

def test_engine_fast_meso_slow_coc_and_routes(cfg_factory):
    cfg=cfg_factory(meso=2,slow=2); eng=Layer3Engine(cfg); last=None; meso=slow=0
    for i in range(4):
        last=eng.step(synthetic_packets(float(i)),reference_time_s=float(i),context={'local_relief_m':10.,'building_height_m':0.,'magnetic_anomaly_nt':0.,'magnetic_gradient_nt_per_m':0.},regime_id=3,stage='cruise')
        meso+=int(last.meso_event is not None); slow+=int(last.slow_event is not None)
    assert (meso,slow)==(2,1)
    c=last.snapshot.center_of_consistency; vg,va,w=c['ground_velocity_ned_m_s'],c['air_velocity_ned_m_s'],c['wind_ned_m_s']
    assert max(abs(vg[j]-va[j]-w[j]) for j in range(3))<1e-8
    assert last.snapshot.guidance['crp_match']['selected'] and last.snapshot.guidance['sarg_reference']['selected']
    assert last.snapshot.guidance['reference_admissible']==last.snapshot.guidance['crp_match']['matched']
    assert last.snapshot.decision_route['destination']=='Layer5' and not last.snapshot.decision_route['layer3_executes_actuator']
    assert last.snapshot.health['mode']=='NORMAL'

def test_meso_never_commits_topology_and_safe_commit_needs_authorization(cfg_factory):
    cfg=cfg_factory(meso=1,slow=2); eng=Layer3Engine(cfg)
    before={k:e.active for k,e in eng.topology.edges.items()}
    eng.step(synthetic_packets(),reference_time_s=0.,regime_id=3,stage='cruise')
    assert before=={k:e.active for k,e in eng.topology.edges.items()}
    key=next(iter(eng.topology.edges))
    r=eng.commit_layer7({'version':'v-safe','topology_changes':{key:True}},approved=True)
    assert not r['committed'] and r['reason']=='structural_authorization_required'
    r=eng.commit_layer7({'version':'v-safe','topology_changes':{key:True}},approved=True,authorization='validation-token')
    assert r['committed']

def test_dependency_registry_exact_versions(cfg_factory):
    eng=Layer3Engine(cfg_factory())
    s=eng.dependency_status
    assert s['all_ok'] and all(s[k]['basename_ok'] for k in ('twins','climate','tdoc','sarg','boulder'))

def test_python_runtime_preflight(cfg_factory):
    from layer3.dependency import DependencyRegistry
    cfg = cfg_factory()
    status = DependencyRegistry(cfg).validate_python_runtime(strict=False)
    assert status['all_ok'], status
    assert status['details']['shapely']['available'] is True

def test_sarg_consumes_authoritative_dataset_family(cfg_factory):
    repo=SARGRepository(cfg_factory())
    ref=repo.select(3,'cruise',0,reference_admissible=True)
    assert ref['selected']
    dc=ref['dataset_contract']
    assert dc['record_count']==1920 and dc['lookup_count']==1920
    assert dc['scenario_count']==10 and dc['fmi_trace_count']>0
    assert dc['stage_summary_count']>0 and dc['scenario_context_count']>0
    assert ref['scenario'] is not None and ref['region_context']
    assert isinstance(ref['fmi_execution_trace'],list)


def test_explicit_six_class_k_vector_and_time_scheduler(cfg_factory):
    eng=Layer3Engine(cfg_factory(meso=2,slow=2))
    r=eng.step(synthetic_packets(),reference_time_s=0.,regime_id=3,stage='cruise')
    for rec in r.fast_record['coupling_details']['edges'].values():
        if 'K_vector' in rec:
            assert rec['K_vector_order']==['Data','Model','State','Error','Objective','Control']
            assert len(rec['K_vector'])==6
    ts=eng.time_scheduler.snapshot()
    assert ts['fast_index']==1 and ts['meso_dt_s']==2.0 and ts['slow_dt_s']==4.0


def test_sarg_mission_phase_aliases_cover_tdoc_vtol_driver(cfg_factory):
    from layer3.knowledge import SARGRepository
    repo=SARGRepository(cfg_factory())
    expected={
        'on_ground':'takeoff_transition','vertical_takeoff':'takeoff_transition','hover':'takeoff_transition',
        'transition_forward':'takeoff_transition','climb':'climb','cruise':'cruise','turn':'turn',
        'approach':'approach','transition_back':'approach',
        'vertical_landing':'approach','touchdown':'approach','emergency_return':'emergency_return',
    }
    for requested,canonical in expected.items():
        assert repo.canonical_sarg_stage(requested)==canonical
        assert repo.resolve_phase(requested)==repo.resolve_phase(canonical)
