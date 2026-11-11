import math
from datetime import datetime, timezone
import pytest
from tdoc_unit_converter.adapters import *
from tdoc_unit_converter.errors import (
    ReferenceRequiredError, QuaternionConventionError, AirDataCalibrationError,
    GridConvergenceError, LogReferenceError,
)


def test_existing_ned_enu_preserved_roundtrip_covariance():
    v=(12.0,-3.0,5.0)
    assert enu_to_ned(ned_to_enu(v)) == pytest.approx(v)
    P=((4.,.2,.1),(.2,9.,.3),(.1,.3,16.))
    P2=ned_enu_covariance(ned_enu_covariance(P))
    for r2,r in zip(P2,P): assert r2 == pytest.approx(r)


def test_utm_roundtrip_and_explicit_epsg():
    pytest.importorskip("pyproj")
    lat,lon=40.015,-105.27
    e,n,epsg=wgs84_to_utm(lat,lon)
    assert epsg == 32613
    lat2,lon2=utm_to_wgs84(e,n,source_epsg=epsg)
    assert lat2 == pytest.approx(lat,abs=1e-8)
    assert lon2 == pytest.approx(lon,abs=1e-8)
    with pytest.raises(ReferenceRequiredError): utm_to_wgs84(e,n)


def test_crs_covariance_numeric():
    pytest.importorskip("pyproj")
    f=lambda ll: transform_crs(ll[0],ll[1],source_crs="EPSG:4326",target_crs="EPSG:32613")
    P=((1e-10,0.),(0.,1e-10))
    out=propagate_covariance_numeric(f,(-105.27,40.015),P,eps=1e-5)
    assert len(out)==2 and out[0][0] > 0 and out[1][1] > 0


def test_time_scales_roundtrip_elapsed_and_reject():
    t=datetime(2026,8,21,13,10,tzinfo=timezone.utc)
    w,tow=utc_to_gps_week_tow(t)
    assert gps_week_tow_to_utc(w,tow) == t
    tai=utc_to_tai_seconds(t)
    assert tai_seconds_to_utc(tai) == t
    epoch=datetime(2026,8,21,13,0,tzinfo=timezone.utc)
    assert common_time_to_elapsed(t,epoch=epoch)==600
    assert elapsed_to_common_time(600,epoch=epoch)==t
    with pytest.raises(ReferenceRequiredError): common_time_to_elapsed(t)


def test_quaternion_frame_order_roundtrip_covariance_and_reject():
    src={"quaternion_order":"wxyz","quaternion_algebra":"Hamilton","rotation_direction":"body_to_nav","active_or_passive":"active","body_frame":"FRD","navigation_frame":"NED"}
    dst={"quaternion_order":"xyzw","quaternion_algebra":"Hamilton","rotation_direction":"body_to_nav","active_or_passive":"active","body_frame":"FLU","navigation_frame":"ENU"}
    q=(1.,0.,0.,0.)
    q2=convert_quaternion(q,source_metadata=src,target_metadata=dst)
    q3=convert_quaternion(q2,source_metadata=dst,target_metadata=src)
    assert q3 == pytest.approx(q,abs=1e-7)
    P=tuple(tuple(1e-6 if i==j else 0. for j in range(4)) for i in range(4))
    P2=quaternion_covariance(P,q,source_metadata=src,target_metadata=dst)
    assert len(P2)==4 and all(len(r)==4 for r in P2)
    with pytest.raises(QuaternionConventionError): convert_quaternion(q,source_metadata={})


def test_qnh_qfe_qne_roundtrip_and_context_reject():
    qnh=101325.0
    qfe=qnh_to_qfe(qnh,elevation_m=1600.0)
    assert qfe_to_qnh(qfe,elevation_m=1600.0) == pytest.approx(qnh,rel=1e-12)
    assert pressure_reference(qnh,source="QNH",target="QNE",elevation_m=1600.0)==101325.0
    with pytest.raises(ReferenceRequiredError): qnh_to_qfe(qnh)
    u=propagate_uncertainty_numeric(lambda p:qnh_to_qfe(p,elevation_m=1600),qnh,30.0)
    assert u > 0


def test_airdata_ias_cas_eas_tas_roundtrip_and_pressure_types():
    cal={"scale":1.01,"offset":-0.4}
    ias=50.0
    cas=ias_to_cas(ias,calibration=cal)
    assert cas_to_ias(cas,calibration=cal)==pytest.approx(ias)
    ps=90000.0
    eas=cas_to_eas(cas,static_pressure_pa=ps)
    assert eas_to_cas(eas,static_pressure_pa=ps)==pytest.approx(cas,rel=1e-10)
    tas=cas_to_tas(cas,static_pressure_pa=ps,temperature_k=280.0)
    assert tas_to_cas(tas,static_pressure_pa=ps,temperature_k=280.0)==pytest.approx(cas,rel=1e-10)
    qc=impact_pressure_from_cas(cas)
    assert cas_from_impact_pressure(qc)==pytest.approx(cas)
    assert impact_pressure(total_pressure(ps,qc),ps)==pytest.approx(qc)
    with pytest.raises(AirDataCalibrationError): ias_to_cas(ias)


def test_heading_true_grid_roundtrip_reject():
    a=math.radians(123); g=math.radians(2.5)
    assert grid_to_true(true_to_grid(a,grid_convergence_rad=g),grid_convergence_rad=g)==pytest.approx(a)
    with pytest.raises(GridConvergenceError): true_to_grid(a)


def test_wind_head_cross_tail():
    c=wind_components_along_track((5.,-10.,0.),0.0)
    assert c["tailwind"] == pytest.approx(-10)
    assert c["headwind"] == pytest.approx(10)
    assert c["crosswind"] == pytest.approx(5)


def test_log_pressure_roundtrip_uncertainty_reject():
    p=90000.; pref=101325.
    l=pressure_to_log_ratio(p,p_ref=pref)
    assert log_ratio_to_pressure(l,p_ref=pref)==pytest.approx(p)
    assert pressure_log_uncertainty(p,90)==pytest.approx(.001)
    assert log_pressure_uncertainty(l,.001,p_ref=pref)==pytest.approx(90)
    with pytest.raises(LogReferenceError): pressure_to_log_ratio(p)


def test_registry_every_entry_has_status_and_callable_api():
    import tdoc_unit_converter.adapters as a
    for name,entry in AVIATION_ADAPTER_REGISTRY.items():
        assert entry["implementation_status"]
        assert entry["api"]
        for api in entry["api"]:
            assert callable(getattr(a,api)), (name,api)



def test_wgs84_ecef_explicit_bidirectional_roundtrip():
    lat,lon,h=math.radians(40.015),math.radians(-105.27),1650.0
    xyz=wgs84_to_ecef(lat,lon,h)
    lat2,lon2,h2=ecef_to_wgs84(*xyz)
    assert lat2 == pytest.approx(lat,abs=1e-11)
    assert lon2 == pytest.approx(lon,abs=1e-11)
    assert h2 == pytest.approx(h,abs=1e-5)


def test_ecef_local_tangent_explicit_bidirectional_roundtrip_covariance():
    lat,lon,h=math.radians(40.015),math.radians(-105.27),1650.0
    origin=wgs84_to_ecef(lat,lon,h)
    enu=(120.0,-35.0,8.0)
    xyz=local_enu_to_ecef(enu,origin_lat_rad=lat,origin_lon_rad=lon,origin_h_m=h)
    assert ecef_to_local_enu(xyz,origin_lat_rad=lat,origin_lon_rad=lon,origin_h_m=h) == pytest.approx(enu,abs=1e-8)
    P=((4.,.2,.1),(.2,9.,.3),(.1,.3,16.))
    Penu=ecef_enu_covariance(P,lat,lon,direction="ecef_to_enu")
    Pxyz=ecef_enu_covariance(Penu,lat,lon,direction="enu_to_ecef")
    for a,b in zip(Pxyz,P): assert a == pytest.approx(b,abs=1e-10)


def test_body_navigation_explicit_bidirectional_roundtrip_covariance():
    q=euler_to_quaternion((math.radians(30),math.radians(8),math.radians(-4)),sequence="ZYX")
    vb=(30.0,2.0,-1.0)
    vn=body_to_navigation_vector(vb,quaternion=q)
    assert navigation_to_body_vector(vn,quaternion=q) == pytest.approx(vb,abs=1e-9)
    P=((1.,.1,0.),(.1,2.,.2),(0.,.2,3.))
    Pn=body_navigation_covariance(P,quaternion=q,direction="body_to_nav")
    Pb=body_navigation_covariance(Pn,quaternion=q,direction="nav_to_body")
    for a,b in zip(Pb,P): assert a == pytest.approx(b,abs=1e-9)
    with pytest.raises(ReferenceRequiredError): body_to_navigation_vector(vb)


@pytest.mark.parametrize("sequence",["XYZ","XZY","YXZ","YZX","ZXY","ZYX"])
def test_euler_sequence_matrix_roundtrip(sequence):
    a=(0.3,-0.2,0.1)
    R=euler_to_matrix(a,sequence=sequence)
    a2=matrix_to_euler(R,sequence=sequence)
    R2=euler_to_matrix(a2,sequence=sequence)
    for r2,r in zip(R2,R): assert r2 == pytest.approx(r,abs=1e-10)
    q=euler_to_quaternion(a,sequence=sequence)
    a3=quaternion_to_euler(q,sequence=sequence)
    R3=euler_to_matrix(a3,sequence=sequence)
    for r3,r in zip(R3,R): assert r3 == pytest.approx(r,abs=1e-10)


def test_body_rates_euler_rates_roundtrip_covariance_and_singularity():
    angles=(math.radians(20),math.radians(10),math.radians(-5)) # ZYX yaw,pitch,roll
    er=(0.03,-0.02,0.01)
    body=euler_rates_to_body_rates(er,angles,sequence="ZYX")
    assert body_rates_to_euler_rates(body,angles,sequence="ZYX") == pytest.approx(er,rel=1e-8,abs=1e-9)
    P=((1e-4,0.,0.),(0.,2e-4,0.),(0.,0.,3e-4))
    Pb=euler_body_rate_covariance(P,angles,sequence="ZYX",direction="euler_to_body")
    Pe=euler_body_rate_covariance(Pb,angles,sequence="ZYX",direction="body_to_euler")
    for a,b in zip(Pe,P): assert a == pytest.approx(b,rel=1e-7,abs=1e-10)
    with pytest.raises(EulerSequenceError): euler_to_matrix((0,0,0),sequence="ZXZ")


def test_data_semantic_adapter_roles_relabel_reject_and_provenance():
    x=adapt_data_semantics(42.0,source_role="current state")
    assert x["semantics"] == "current_state" and x["value"] == 42.0
    for role in ("current_state","prior","reference","truth"):
        assert normalize_data_semantic_role(role)==role
    with pytest.raises(DataSemanticError): adapt_data_semantics(42,source_role="truth",target_role="current_state")
    with pytest.raises(ReferenceRequiredError): adapt_data_semantics(42,source_role="truth",target_role="reference",allow_relabel=True)
    y=adapt_data_semantics(42,source_role="truth",target_role="reference",allow_relabel=True,provenance="frozen validation truth used as explicit reference")
    assert y["metadata"]["relabelled"] is True
