"""Advanced aviation examples for TDOC v2 semantic adapters."""
import math
from datetime import datetime, timezone
from tdoc_unit_converter import convert
from tdoc_unit_converter.adapters import *

print("=== Aviation units ===")
for value,src,dst in [(10000,"ft","m"),(120,"knot","m/s"),(1013.25,"hPa","Pa"),(29.92,"inHg","Pa")]:
    print(value,src,"->",convert(value,src,dst),dst)

print("\n=== Frames / vertical references ===")
print("NED -> ENU:", ned_to_enu((100.0,20.0,5.0)))
print("FRD -> FLU:", frd_to_flu((40.0,2.0,1.0)))
print("1500 m MSL -> AGL over 1200 m terrain:", msl_to_agl(1500,1200))

print("\n=== WGS84 / UTM (optional pyproj) ===")
try:
    e,n,epsg=wgs84_to_utm(40.015,-105.27)
    print("UTM:",e,n,"EPSG:",epsg,"back:",utm_to_wgs84(e,n,source_epsg=epsg))
except Exception as exc:
    print("UTM example unavailable:",exc)

print("\n=== Quaternion NED/FRD -> ENU/FLU ===")
src={"quaternion_order":"wxyz","quaternion_algebra":"Hamilton","rotation_direction":"body_to_nav","active_or_passive":"active","body_frame":"FRD","navigation_frame":"NED"}
dst={"quaternion_order":"xyzw","quaternion_algebra":"Hamilton","rotation_direction":"body_to_nav","active_or_passive":"active","body_frame":"FLU","navigation_frame":"ENU"}
print(convert_quaternion((1,0,0,0),source_metadata=src,target_metadata=dst))

print("\n=== UTC / GPS / TAI / mission elapsed ===")
t=datetime(2026,8,21,13,10,tzinfo=timezone.utc)
week,tow=utc_to_gps_week_tow(t)
print("UTC -> GPS:",week,tow,"-> UTC:",gps_week_tow_to_utc(week,tow))
print("UTC -> TAI seconds:",utc_to_tai_seconds(t),"table:",LEAP_SECOND_TABLE_VERSION)
print("mission elapsed:",common_time_to_elapsed(t,epoch=datetime(2026,8,21,13,0,tzinfo=timezone.utc)),"s")

print("\n=== QNH / QFE / QNE ===")
qnh=101325.0; elevation=1600.0
qfe=qnh_to_qfe(qnh,elevation_m=elevation)
print("QNH -> QFE:",qfe,"Pa; round-trip:",qfe_to_qnh(qfe,elevation_m=elevation))
print("QNH -> QNE:",pressure_reference(qnh,source="QNH",target="QNE",elevation_m=elevation),"Pa")

print("\n=== IAS / CAS / EAS / TAS / pressures ===")
cal={"scale":1.01,"offset":-0.4}; ias=50.0; ps=90000.0; T=280.0
cas=ias_to_cas(ias,calibration=cal); eas=cas_to_eas(cas,static_pressure_pa=ps); tas=cas_to_tas(cas,static_pressure_pa=ps,temperature_k=T)
print("IAS,CAS,EAS,TAS:",ias,cas,eas,tas,"m/s")
qc=impact_pressure_from_cas(cas); print("impact pressure:",qc,"Pa; total:",total_pressure(ps,qc),"Pa; dynamic:",dynamic_pressure(ps/(287.05287*T),tas),"Pa")

print("\n=== Heading / wind / log pressure ===")
true=math.radians(90); conv=math.radians(2)
print("True -> Grid -> True:",true_to_grid(true,grid_convergence_rad=conv),grid_to_true(true_to_grid(true,grid_convergence_rad=conv),grid_convergence_rad=conv))
wind=wind_from_to_vector(12,math.radians(270)); print("wind TO ENU:",wind,"components:",wind_components_along_track(wind,math.radians(0)))
lp=pressure_to_log_ratio(90000,p_ref=101325); print("ln(p/p_ref):",lp,"unit=1; inverse:",log_ratio_to_pressure(lp,p_ref=101325),"Pa")


print("\n=== Explicit WGS84 <-> ECEF <-> local tangent APIs ===")
lat=math.radians(40.015); lon=math.radians(-105.27); h=1650.0
xyz=wgs84_to_ecef(lat,lon,h); print("WGS84->ECEF:",xyz,"back:",ecef_to_wgs84(*xyz))
enu=(100.0,20.0,5.0); xyz2=local_enu_to_ecef(enu,origin_lat_rad=lat,origin_lon_rad=lon,origin_h_m=h)
print("local ENU->ECEF->local ENU:",ecef_to_local_enu(xyz2,origin_lat_rad=lat,origin_lon_rad=lon,origin_h_m=h))

print("\n=== Explicit Body <-> Navigation ===")
q=euler_to_quaternion((math.radians(30),math.radians(5),math.radians(-2)),sequence="ZYX")
vb=(20.0,1.0,-0.5); vn=body_to_navigation_vector(vb,quaternion=q)
print("body->nav:",vn,"nav->body:",navigation_to_body_vector(vn,quaternion=q))

print("\n=== Euler sequence + body rates <-> Euler rates ===")
angles=(math.radians(30),math.radians(5),math.radians(-2)) # ZYX = yaw,pitch,roll
rates=(math.radians(1),math.radians(.5),math.radians(-.2))
pqr=euler_rates_to_body_rates(rates,angles,sequence="ZYX")
print("Euler rates->pqr:",pqr,"back:",body_rates_to_euler_rates(pqr,angles,sequence="ZYX"))

print("\n=== DataSemanticAdapter ===")
print(adapt_data_semantics({"x":1.0},source_role="current_state"))
print(adapt_data_semantics({"x":1.0},source_role="truth",target_role="reference",allow_relabel=True,provenance="explicit validation reference"))
