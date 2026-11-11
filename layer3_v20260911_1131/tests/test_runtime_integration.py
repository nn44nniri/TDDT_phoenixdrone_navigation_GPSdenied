from __future__ import annotations
from pathlib import Path
from layer3 import Layer3FMURuntime, RuntimeRequest
from conftest import strict_climate, strict_context, strict_anchor

ROOT=Path(__file__).resolve().parents[1]
LAT,LON=40.017498782173554,-105.28999780562658

def request(**kw):
    d=dict(reference_time_s=.001,regime_id=3,stage='cruise',climate=strict_climate(LAT,LON),context=strict_context(),mesa_anchor=strict_anchor())
    d.update(kw); return RuntimeRequest(**d)

def test_real_three_fmus_tdoc_boulder_and_runtime_couplings(cfg_factory):
    cfg=cfg_factory(meso=3,slow=2)
    with Layer3FMURuntime(cfg) as rt: r=rt.step(request())
    assert set(r.snapshot.std['representations'])=={'MESA_NAV','WingMPET','MicroMetTwin'}
    assert r.snapshot.std['representations']['MicroMetTwin']['canonical_state']['wind_ned_m_s']
    assert abs(sum(r.snapshot.weights.values())-1.)<1e-9
    ctrl=r.snapshot.std['control_status']; ops=ctrl['runtime_couplings']['operations']
    assert {'Climate->WingMPET:op5','Climate+MESA->WingMPET:op6','MESA+Climate->WingMPET:op7','WingMPET->MESA_NAV:model_error','MESA_NAV->MicroMetTwin:query'}<=set(ops)
    assert not ops['WingMPET->MESA_NAV:model_error']['state_overwrite']
    assert not ops['MESA_NAV->MicroMetTwin:query']['geodetic_origin_inferred']
    assert ctrl['environmental_anchor_candidates']['wind']['accepted_by_mesa']
    assert ctrl['environmental_anchor_candidates']['pressure']['accepted_by_mesa']
    assert not ctrl['environmental_anchor_candidates']['wind']['state_injected_by_layer3']
    assert ctrl['runtime_couplings']['boulder']['api']['sample']=='map_mosaic_query.sample_file'
    assert 'climate_sample' in ctrl['runtime_couplings']['boulder']
    assert 'local_relief_m' in ctrl['runtime_couplings']['boulder']['context']
    assert r.snapshot.tdoc_health['conversions']>0 and r.snapshot.tdoc_health['failures']==0
    assert r.snapshot.health['mode']=='NORMAL'

def test_contract_gateway_converts_ft_to_m_before_wing(cfg_factory):
    cfg=cfg_factory()
    wing_inputs={
      'state_x':{'value':0.,'unit':'ft','frame':'ENU','semantic':'state_x'},
      'state_y':{'value':0.,'unit':'ft','frame':'ENU','semantic':'state_y'},
      'state_h':{'value':5905.511811,'unit':'ft','frame':'ENU','semantic':'state_h'},
      'air_velocity_x':{'value':13.,'unit':'m/s','frame':'ENU','semantic':'air_velocity_x'},
      'air_velocity_y':{'value':.5,'unit':'m/s','frame':'ENU','semantic':'air_velocity_y'},
      'air_velocity_h':{'value':0.,'unit':'m/s','frame':'ENU','semantic':'air_velocity_h'},
    }
    with Layer3FMURuntime(cfg) as rt:
        vals, rec=rt._normalize_inputs('WingMPET',rt.wing,4,wing_inputs,{})
        assert abs(vals['state_h']-1800.)<1e-4
        assert any('state_x' in x.get('group',[]) for x in rec['records'])

def test_tdoc_vector_frame_and_covariance(cfg_factory):
    cfg=cfg_factory()
    with Layer3FMURuntime(cfg) as rt:
        out,cov,rec=rt.tdoc.transform_vector([1.,2.,3.],'ENU','NED',covariance=[[1.,0.,0.],[0.,4.,0.],[0.,0.,9.]])
    assert out==[2.,1.,-3.] and cov==[[4.0,0.0,0.0],[0.0,1.0,0.0],[0.0,0.0,9.0]] and rec['ok']

def test_six_tick_end_to_end(cfg_factory):
    cfg=cfg_factory(meso=3,slow=2); meso=slow=0; last=None
    with Layer3FMURuntime(cfg) as rt:
        for i in range(6):
            req=request(reference_time_s=.001+i)
            last=rt.step(req); meso+=int(last.meso_event is not None); slow+=int(last.slow_event is not None)
    assert (meso,slow)==(2,1)
    assert last.snapshot.version_state['active_version']=='L3-policy-slow-00001'
    assert last.snapshot.health['mode']=='NORMAL'

def test_boulder_consumes_assignment_and_cluster_raster(cfg_factory):
    cfg=cfg_factory()
    with Layer3FMURuntime(cfg) as rt:
        b=rt.boulder.sample(LON,LAT,hour=12)
    assert b['crp_assignment'] is not None
    assert b['pixel']=={'row':171,'col':171}
    assert b['crp_spatial_crosscheck']['agree'] is True
    assert int(b['crp_spatial_crosscheck']['raster_cluster_id'])==int(b['crp_spatial_crosscheck']['assignment_cluster_id'])
    assert b['api']['crp_assignment'].startswith('crp_cell_assignments.csv.gz')

def test_rejected_climate_wind_anchor_never_injects_mesa_state(cfg_factory):
    cfg=cfg_factory()
    req=request(mesa_anchor=strict_anchor(residual=1e9,jac_n=.1,jac_e=.1,nis_gate=1e-6,pressure_enabled=False))
    with Layer3FMURuntime(cfg) as rt:
        r=rt.step(req)
    a=r.snapshot.std['control_status']['environmental_anchor_candidates']['wind']
    assert a['offered'] and not a['accepted_by_mesa']
    assert not a['state_injected_by_layer3']
    assert a['packet']['state']['terrain_flow_update_valid']==0.0
