from __future__ import annotations
import math
import pytest
from layer3 import Layer3FMURuntime
from layer3.tdoc_adapter import TDOCAdapter
from conftest import sig, strict_climate, strict_context, strict_anchor


def test_strict_external_bare_and_incomplete_envelopes_are_rejected(cfg_factory):
    cfg=cfg_factory()
    with Layer3FMURuntime(cfg) as rt:
        with pytest.raises(ValueError, match='bare external'):
            rt._normalize_inputs('MESA_NAV',rt.mesa,5,{'dt':0.01},{})
        with pytest.raises(ValueError):
            rt._normalize_inputs('MESA_NAV',rt.mesa,5,{'dt':{'value':0.01,'unit':'s'}},{})


def test_strict_semantic_mismatch_requires_explicit_mapping(cfg_factory):
    t=TDOCAdapter(cfg_factory())
    with pytest.raises(ValueError, match='semantic mismatch'):
        t.normalize_envelope(sig(10.0,'m','SCALAR','wrong_signal'),target_unit='m',target_frame='SCALAR',target_semantic='right_signal',external=True)
    value,env=t.normalize_envelope(sig(10.0,'m','SCALAR','source_signal'),target_unit='m',target_frame='SCALAR',target_semantic='target_signal',external=True,
        allow_semantic_relabel=True,semantic_provenance={'adapter':'test_Phi','reason':'explicit coupling mapping'})
    assert value==10.0 and env['provenance']['tdoc']['semantic_valid']


def test_context_dependent_frame_needs_resolver(cfg_factory):
    t=TDOCAdapter(cfg_factory())
    with pytest.raises(ValueError, match='requires an explicit resolver'):
        t.transform_vector([1,2,3],'CONTEXT_DEPENDENT','ENU')
    out,_,rec=t.transform_vector([1,2,3],'CONTEXT_DEPENDENT','ENU',context={'source_frame_resolved':'NED'})
    assert out==[2.0,1.0,-3.0] and rec['ok']


def test_wgs84_ecef_covariance_is_propagated(cfg_factory):
    t=TDOCAdapter(cfg_factory())
    P=[[1e-10,0,0],[0,1e-10,0],[0,0,4.0]]
    ecef,cov,_=t.transform_vector([40.0,-105.0,1700.0],'WGS84','ECEF',covariance=P)
    assert len(ecef)==3 and cov is not None and len(cov)==3 and all(len(r)==3 for r in cov)
    wgs,cov2,_=t.transform_vector(ecef,'ECEF','WGS84',covariance=cov)
    assert abs(wgs[0]-40.0)<1e-5 and abs(wgs[1]+105.0)<1e-5 and cov2 is not None


def test_heading_wind_airdata_quaternion_vertical_time_and_crs_adapters(cfg_factory):
    t=TDOCAdapter(cfg_factory())
    h,hr=t.transform_heading(math.radians(90),source_convention='TRUE_NAV',target_convention='TRUE_ENU')
    assert abs(h-0.0)<1e-12 and hr['ok']
    w,_,wr=t.wind_from_to_vector(10.0,0.0,target_frame='NED')
    assert pytest.approx(w[0],abs=1e-9)==-10.0 and wr['ok']
    tas,ar=t.transform_airdata(0.5,source_type='MACH',target_type='TAS',context={'temperature_k':288.15})
    assert tas>100 and ar['ok']
    src={'quaternion_order':'wxyz','quaternion_algebra':'Hamilton','rotation_direction':'body_to_nav','active_or_passive':'active','body_frame':'FRD','navigation_frame':'NED'}
    dst={'quaternion_order':'xyzw','quaternion_algebra':'Hamilton','rotation_direction':'body_to_nav','active_or_passive':'active','body_frame':'FLU','navigation_frame':'ENU'}
    q,cov,qr=t.transform_quaternion([1,0,0,0],source_metadata=src,target_metadata=dst,covariance=[[1e-6 if i==j else 0 for j in range(4)] for i in range(4)])
    assert len(q)==4 and cov is not None and qr['ok']
    agl,vr=t.transform_vertical(1800.0,'MSL','AGL',context={'terrain_msl_m':1700.0})
    assert agl==100.0 and vr['ok']
    ortho,_=t.transform_vertical(1800.0,'ELLIPSOIDAL','ORTHOMETRIC',context={'geoid_undulation_m':20.0})
    assert ortho==1780.0
    gps,tr=t.transform_time('2026-08-24T11:00:00+00:00',source_reference='UTC',target_reference='GPS_SECONDS')
    back,_=t.transform_time(gps,source_reference='GPS_SECONDS',target_reference='UTC')
    assert '2026-08-24T11:00:00' in back and tr['ok']
    utm,covc,cr=t.transform_crs([-105.0,40.0,1700.0],source_crs='EPSG:4326',target_crs='EPSG:32613',covariance=[[1e-10,0,0],[0,1e-10,0],[0,0,1.]])
    assert 400000 < utm[0] < 600000 and covc is not None and cr['ok']


def test_specialized_registry_propagates_heading_and_airdata_uncertainty(cfg_factory):
    t=TDOCAdapter(cfg_factory())
    hv,henv=t.normalize_specialized({**sig(90.0,'deg','SCALAR','heading',uncertainty=1.0),'adapter_kind':'heading','heading_convention':'TRUE_NAV'},
        target_metadata={'adapter_kind':'heading','heading_convention':'TRUE_ENU','unit':'rad','frame':'SCALAR','semantic':'heading'},context={},external=True)
    assert abs(hv)<1e-12 and henv['uncertainty'] is not None and henv['uncertainty']>0
    av,aenv=t.normalize_specialized({**sig(0.5,'1','SCALAR','airspeed',uncertainty=0.01),'adapter_kind':'air_data','air_data_type':'MACH'},
        target_metadata={'adapter_kind':'air_data','air_data_type':'TAS','unit':'m/s','frame':'SCALAR','semantic':'airspeed'},context={'temperature_k':288.15},external=True)
    assert av>100 and aenv['uncertainty'] is not None and aenv['uncertainty']>0


def test_contract_overrides_correct_known_upstream_physical_metadata(cfg_factory):
    cfg=cfg_factory()
    with Layer3FMURuntime(cfg) as rt:
        assert rt.contracts._meta(rt.mesa,10,'log_pressure_prior')['unit']=='1'
        assert rt.contracts._meta(rt.mesa,10,'log_scale_height')['unit']=='1'
        assert rt.contracts._meta(rt.mesa,10,'pressure_time_scale')['unit']=='s'
        assert rt.contracts._meta(rt.wing,6,'wind_time_derivative_x')['unit']=='m/s2'
        assert rt.contracts._meta(rt.wing,6,'wind_jacobian_11')['unit']=='1/s'


def test_pressure_anchor_uses_ln_pa_and_log_scale_height_contract(cfg_factory):
    cfg=cfg_factory()
    from layer3 import RuntimeRequest
    req=RuntimeRequest(reference_time_s=.001,regime_id=3,stage='cruise',climate=strict_climate(),context=strict_context(),mesa_anchor=strict_anchor())
    with Layer3FMURuntime(cfg) as rt:
        r=rt.step(req)
    p=r.snapshot.std['control_status']['environmental_anchor_candidates']['pressure']
    vals=p['normalization']['envelopes']
    assert p['semantic_adapter'].endswith('p_ref=1Pa)')
    assert abs(vals['log_pressure_prior']['value']-math.log(vals['absolute_pressure']['value']))<1e-6
    assert abs(vals['log_scale_height']['value']-math.log(8434.5))<1e-9
    assert vals['pressure_time_scale']['unit']=='s' and vals['pressure_time_scale']['value']==30.0


def test_all_runtime_cross_domain_fmu_inputs_have_complete_canonical_envelopes(cfg_factory):
    from layer3 import RuntimeRequest
    cfg=cfg_factory()
    req=RuntimeRequest(reference_time_s=.001,regime_id=3,stage='cruise',climate=strict_climate(),context=strict_context(),mesa_anchor=strict_anchor())
    with Layer3FMURuntime(cfg) as rt:
        r=rt.step(req)
    ops=r.snapshot.std['control_status']['runtime_couplings']['operations']
    for opname in ('Climate->WingMPET:op5','Climate+MESA->WingMPET:op6','MESA+Climate->WingMPET:op7'):
        norm=ops[opname]['normalization']
        assert norm['envelopes']
        for name,env in norm['envelopes'].items():
            assert all(k in env for k in ('value','unit','frame','semantic','provenance')), (opname,name,env)
            assert env['provenance'].get('tdoc') or env.get('source')=='layer3_trusted_internal'
