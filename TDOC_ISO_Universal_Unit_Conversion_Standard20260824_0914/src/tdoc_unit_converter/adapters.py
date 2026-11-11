import math
from .errors import ReferenceRequiredError, FrameMismatchError, OutOfDomainError, ConversionError
from .converter import convert

T_NED_ENU=((0.,1.,0.),(1.,0.,0.),(0.,0.,-1.))
T_FRD_FLU=((1.,0.,0.),(0.,-1.,0.),(0.,0.,-1.))

def matvec(M,v): return tuple(sum(M[i][j]*v[j] for j in range(3)) for i in range(3))
def transpose(M): return tuple(zip(*M))
def matmul(A,B): return tuple(tuple(sum(A[i][k]*B[k][j] for k in range(len(B))) for j in range(len(B[0]))) for i in range(len(A)))
def transform_covariance(P,T): return matmul(matmul(T,P),transpose(T))

def ned_to_enu(v): return matvec(T_NED_ENU,v)
def enu_to_ned(v): return matvec(T_NED_ENU,v)
def frd_to_flu(v): return matvec(T_FRD_FLU,v)
def flu_to_frd(v): return matvec(T_FRD_FLU,v)
def ned_enu_covariance(P): return transform_covariance(P,T_NED_ENU)
def frd_flu_covariance(P): return transform_covariance(P,T_FRD_FLU)
def ned_enu_tensor(G): return transform_covariance(G,T_NED_ENU)

def msl_to_agl(h_msl, terrain_msl): return h_msl-terrain_msl
def agl_to_msl(h_agl, terrain_msl): return h_agl+terrain_msl
def ned_down_to_altitude(down, origin_altitude): return origin_altitude-down
def altitude_to_ned_down(altitude, origin_altitude): return origin_altitude-altitude
def ellipsoidal_to_orthometric(h, geoid_undulation): return h-geoid_undulation
def orthometric_to_ellipsoidal(H, geoid_undulation): return H+geoid_undulation

def heading_nav_to_enu(angle_rad): return (math.pi/2-angle_rad)%(2*math.pi)
def heading_enu_to_nav(angle_rad): return (math.pi/2-angle_rad)%(2*math.pi)
def true_to_magnetic(true_rad, variation_rad): return (true_rad-variation_rad)%(2*math.pi)
def magnetic_to_true(mag_rad, variation_rad): return (mag_rad+variation_rad)%(2*math.pi)

def wind_from_to_vector(speed, met_from_rad, vertical=0.0):
    return (-speed*math.sin(met_from_rad), -speed*math.cos(met_from_rad), vertical)

def wind_triangle(air_velocity, wind_to): return tuple(a+w for a,w in zip(air_velocity,wind_to))
def vector_norm(v): return math.sqrt(sum(x*x for x in v))

def mach_to_tas(mach, *, speed_of_sound=None, temperature_k=None, gamma=1.4, gas_constant=287.05287):
    if speed_of_sound is None:
        if temperature_k is None: raise ReferenceRequiredError("Mach→TAS requires speed_of_sound or temperature_k")
        speed_of_sound=math.sqrt(gamma*gas_constant*temperature_k)
    return mach*speed_of_sound

def tas_to_mach(tas, **kw):
    a=mach_to_tas(1.0,**kw); return tas/a

def tas_to_eas(tas, rho, rho0=1.225): return tas*math.sqrt(rho/rho0)
def eas_to_tas(eas, rho, rho0=1.225): return eas*math.sqrt(rho0/rho)
def dynamic_pressure(rho,tas): return .5*rho*tas*tas

def pressure_altitude_lapse(p_pa, p0=101325.0, T0=288.15, L=0.0065, Rd=287.05287, g0=9.80665):
    if p_pa <= 0 or p_pa > p0*10: raise OutOfDomainError("pressure must be positive and physically plausible")
    return T0/L*(1-(p_pa/p0)**(Rd*L/g0))
def flight_level_from_pressure_altitude(h_m): return convert(h_m,"m","ft")/100.0

def wgs84_to_ecef(lat_rad, lon_rad, h_m):
    a=6378137.0; f=1/298.257223563; e2=f*(2-f)
    s=math.sin(lat_rad); c=math.cos(lat_rad); N=a/math.sqrt(1-e2*s*s)
    return ((N+h_m)*c*math.cos(lon_rad),(N+h_m)*c*math.sin(lon_rad),(N*(1-e2)+h_m)*s)
def ecef_delta_to_enu(delta_xyz, origin_lat_rad, origin_lon_rad):
    ph,la=origin_lat_rad,origin_lon_rad
    R=((-math.sin(la),math.cos(la),0),(-math.sin(ph)*math.cos(la),-math.sin(ph)*math.sin(la),math.cos(ph)),(math.cos(ph)*math.cos(la),math.cos(ph)*math.sin(la),math.sin(ph)))
    return matvec(R,delta_xyz)


def ecef_to_wgs84(x_m, y_m, z_m):
    """Pure-Python WGS84 ECEF -> geodetic (lat_rad, lon_rad, h_m).

    Uses a stable iterative ellipsoidal solution and requires no pyproj.
    """
    a=6378137.0; f=1/298.257223563; e2=f*(2-f)
    x=float(x_m); y=float(y_m); z=float(z_m)
    p=math.hypot(x,y)
    if p < 1e-12:
        if abs(z) < 1e-12: raise OutOfDomainError("ECEF origin has no geodetic latitude/longitude")
        b=a*(1-f); return (math.copysign(math.pi/2,z),0.0,abs(z)-b)
    lon=math.atan2(y,x)
    lat=math.atan2(z,p*(1-e2))
    h=0.0
    for _ in range(12):
        s=math.sin(lat); N=a/math.sqrt(1-e2*s*s)
        c=math.cos(lat)
        h=p/c-N if abs(c)>1e-14 else z/s-N*(1-e2)
        new_lat=math.atan2(z,p*(1-e2*N/(N+h)))
        if abs(new_lat-lat) < 1e-13:
            lat=new_lat; break
        lat=new_lat
    s=math.sin(lat); N=a/math.sqrt(1-e2*s*s); c=math.cos(lat)
    h=p/c-N if abs(c)>1e-14 else z/s-N*(1-e2)
    return (lat,lon,h)

def _ecef_to_enu_rotation(origin_lat_rad, origin_lon_rad):
    ph,la=float(origin_lat_rad),float(origin_lon_rad)
    return ((-math.sin(la),math.cos(la),0.0),
            (-math.sin(ph)*math.cos(la),-math.sin(ph)*math.sin(la),math.cos(ph)),
            (math.cos(ph)*math.cos(la),math.cos(ph)*math.sin(la),math.sin(ph)))

def enu_delta_to_ecef(delta_enu, origin_lat_rad, origin_lon_rad):
    """Inverse of ecef_delta_to_enu for a local tangent vector."""
    R=_ecef_to_enu_rotation(origin_lat_rad,origin_lon_rad)
    return matvec(transpose(R),delta_enu)


def ecef_enu_covariance(P, origin_lat_rad, origin_lon_rad, *, direction="ecef_to_enu"):
    R=_ecef_to_enu_rotation(origin_lat_rad,origin_lon_rad)
    if direction=="ecef_to_enu": return transform_covariance(P,R)
    if direction=="enu_to_ecef": return transform_covariance(P,transpose(R))
    raise FrameMismatchError("direction must be ecef_to_enu or enu_to_ecef")

def ecef_to_local_enu(ecef_xyz, *, origin_lat_rad=None, origin_lon_rad=None, origin_h_m=None, origin_ecef=None):
    """Absolute ECEF -> local ENU using an explicit WGS84 origin."""
    if origin_ecef is None:
        if origin_lat_rad is None or origin_lon_rad is None or origin_h_m is None:
            raise ReferenceRequiredError("ECEF→local ENU requires origin_ecef or origin lat/lon/height")
        origin_ecef=wgs84_to_ecef(origin_lat_rad,origin_lon_rad,origin_h_m)
    elif origin_lat_rad is None or origin_lon_rad is None:
        origin_lat_rad,origin_lon_rad,_=ecef_to_wgs84(*origin_ecef)
    delta=tuple(float(a)-float(b) for a,b in zip(ecef_xyz,origin_ecef))
    return ecef_delta_to_enu(delta,origin_lat_rad,origin_lon_rad)

def local_enu_to_ecef(enu_xyz, *, origin_lat_rad=None, origin_lon_rad=None, origin_h_m=None, origin_ecef=None):
    """Local ENU -> absolute ECEF using an explicit WGS84 origin."""
    if origin_ecef is None:
        if origin_lat_rad is None or origin_lon_rad is None or origin_h_m is None:
            raise ReferenceRequiredError("local ENU→ECEF requires origin_ecef or origin lat/lon/height")
        origin_ecef=wgs84_to_ecef(origin_lat_rad,origin_lon_rad,origin_h_m)
    elif origin_lat_rad is None or origin_lon_rad is None:
        origin_lat_rad,origin_lon_rad,_=ecef_to_wgs84(*origin_ecef)
    delta=enu_delta_to_ecef(enu_xyz,origin_lat_rad,origin_lon_rad)
    return tuple(float(a)+float(b) for a,b in zip(origin_ecef,delta))

def air_angles_frd(u,v,w):
    Va=math.sqrt(u*u+v*v+w*w)
    if Va == 0: raise OutOfDomainError("airspeed vector magnitude is zero")
    return {"airspeed":Va,"alpha":math.atan2(w,u),"beta":math.asin(max(-1,min(1,v/Va)))}
def flight_path_angle_enu(ve,vn,vu): return math.atan2(vu, math.hypot(ve,vn))
def flight_path_angle_ned(vn,ve,vd): return math.atan2(-vd, math.hypot(vn,ve))
def slope_ratio_to_angle(s): return math.atan(s)
def slope_percent_to_angle(pct): return math.atan(pct/100.0)
def fuel_volume_to_mass(volume_m3,density_kg_m3): return volume_m3*density_kg_m3

# ---------------------------------------------------------------------------
# Aviation semantic adapters added to complete the TDOC v2 Adapter Registry.
# These are intentionally dependency-light. pyproj is imported lazily only
# when a general CRS/datum transformation is requested.
# ---------------------------------------------------------------------------
from datetime import datetime, timezone, timedelta
from .errors import (
    CRSMismatchError, DatumMismatchError, QuaternionConventionError,
    TimeReferenceError, AirDataCalibrationError, PressureTypeMismatchError,
    GridConvergenceError, LogReferenceError, EulerSequenceError, DataSemanticError,
)

# ---- Geodesy / CRS ---------------------------------------------------------
def _pyproj_transformer(source_crs, target_crs):
    try:
        from pyproj import Transformer
    except ImportError as exc:
        raise CRSMismatchError(
            "CRS transformation requires optional dependency 'pyproj'; "
            "install package extra [geodesy]"
        ) from exc
    if source_crs is None or target_crs is None:
        raise ReferenceRequiredError("source_crs and target_crs (preferably EPSG codes) are required")
    return Transformer.from_crs(source_crs, target_crs, always_xy=True)


def transform_crs(x, y, z=None, *, source_crs=None, target_crs=None):
    """Transform coordinates with explicit CRS definitions/EPSG identifiers."""
    tr = _pyproj_transformer(source_crs, target_crs)
    return tr.transform(x, y) if z is None else tr.transform(x, y, z)


def utm_epsg_from_wgs84(lat_deg, lon_deg):
    if not (-80.0 <= lat_deg <= 84.0):
        raise OutOfDomainError("standard UTM is defined here only for latitude -80..84 deg")
    if not (-180.0 <= lon_deg <= 180.0):
        raise OutOfDomainError("longitude must be in -180..180 deg")
    zone = min(60, max(1, int((lon_deg + 180.0) // 6.0) + 1))
    return (32600 if lat_deg >= 0 else 32700) + zone


def wgs84_to_utm(lat_deg, lon_deg, h_m=None, *, target_epsg=None):
    epsg = int(target_epsg or utm_epsg_from_wgs84(lat_deg, lon_deg))
    if not (32601 <= epsg <= 32660 or 32701 <= epsg <= 32760):
        raise CRSMismatchError("target_epsg must be an explicit WGS84 UTM EPSG (326xx/327xx)")
    out = transform_crs(lon_deg, lat_deg, h_m, source_crs="EPSG:4979" if h_m is not None else "EPSG:4326", target_crs=f"EPSG:{epsg}")
    return (*out, epsg) if h_m is not None else (out[0], out[1], epsg)


def utm_to_wgs84(easting_m, northing_m, *, source_epsg=None, h_m=None):
    if source_epsg is None:
        raise ReferenceRequiredError("source_epsg is required for UTM→WGS84")
    epsg = int(source_epsg)
    out = transform_crs(easting_m, northing_m, h_m, source_crs=f"EPSG:{epsg}", target_crs="EPSG:4979" if h_m is not None else "EPSG:4326")
    return (out[1], out[0], out[2]) if h_m is not None else (out[1], out[0])


def transform_datum(x, y, z=None, *, source_crs=None, target_crs=None):
    """Datum/CRS transformation delegated to PROJ through pyproj."""
    if source_crs is None or target_crs is None:
        raise ReferenceRequiredError("datum transformation requires explicit source_crs and target_crs")
    return transform_crs(x, y, z, source_crs=source_crs, target_crs=target_crs)

# ---- Heading ---------------------------------------------------------------
def true_to_grid(true_rad, *, grid_convergence_rad=None):
    if grid_convergence_rad is None:
        raise GridConvergenceError("True→Grid requires grid_convergence_rad")
    return (true_rad-grid_convergence_rad)%(2*math.pi)


def grid_to_true(grid_rad, *, grid_convergence_rad=None):
    if grid_convergence_rad is None:
        raise GridConvergenceError("Grid→True requires grid_convergence_rad")
    return (grid_rad+grid_convergence_rad)%(2*math.pi)

# ---- Wind ------------------------------------------------------------------
def wind_components_along_track(wind_to_enu, track_rad):
    """Return signed tail/head/cross wind for track clockwise from North.

    Positive crosswind is to the right of track. Input wind is a TO vector ENU.
    """
    if len(wind_to_enu) < 2:
        raise FrameMismatchError("wind_to_enu must contain at least East and North components")
    e, n = wind_to_enu[0], wind_to_enu[1]
    t = (math.sin(track_rad), math.cos(track_rad))
    right = (math.cos(track_rad), -math.sin(track_rad))
    tail = e*t[0] + n*t[1]
    cross = e*right[0] + n*right[1]
    return {"tailwind":tail, "headwind":-tail, "crosswind":cross}

# ---- Log quantity ----------------------------------------------------------
def pressure_to_log_ratio(p, *, p_ref=None):
    if p_ref is None:
        raise LogReferenceError("pressure log-ratio requires p_ref")
    if p <= 0 or p_ref <= 0:
        raise OutOfDomainError("p and p_ref must be positive")
    return math.log(p/p_ref)


def log_ratio_to_pressure(log_ratio, *, p_ref=None):
    if p_ref is None:
        raise LogReferenceError("inverse pressure log-ratio requires p_ref")
    if p_ref <= 0:
        raise OutOfDomainError("p_ref must be positive")
    return p_ref*math.exp(log_ratio)


def pressure_log_uncertainty(p, u_p):
    if p <= 0 or u_p < 0:
        raise OutOfDomainError("p must be positive and uncertainty non-negative")
    return abs(u_p/p)


def log_pressure_uncertainty(log_ratio, u_log, *, p_ref=None):
    p = log_ratio_to_pressure(log_ratio, p_ref=p_ref)
    if u_log < 0:
        raise OutOfDomainError("uncertainty must be non-negative")
    return abs(p*u_log)

# ---- Time scales -----------------------------------------------------------
LEAP_SECOND_TABLE_VERSION = "IERS-compatible-through-2017-01-01"
# Effective UTC instants and TAI-UTC after the leap. 1972+ official step table.
_LEAPS = (
    ("1972-01-01T00:00:00Z",10),("1972-07-01T00:00:00Z",11),("1973-01-01T00:00:00Z",12),
    ("1974-01-01T00:00:00Z",13),("1975-01-01T00:00:00Z",14),("1976-01-01T00:00:00Z",15),
    ("1977-01-01T00:00:00Z",16),("1978-01-01T00:00:00Z",17),("1979-01-01T00:00:00Z",18),
    ("1980-01-01T00:00:00Z",19),("1981-07-01T00:00:00Z",20),("1982-07-01T00:00:00Z",21),
    ("1983-07-01T00:00:00Z",22),("1985-07-01T00:00:00Z",23),("1988-01-01T00:00:00Z",24),
    ("1990-01-01T00:00:00Z",25),("1991-01-01T00:00:00Z",26),("1992-07-01T00:00:00Z",27),
    ("1993-07-01T00:00:00Z",28),("1994-07-01T00:00:00Z",29),("1996-01-01T00:00:00Z",30),
    ("1997-07-01T00:00:00Z",31),("1999-01-01T00:00:00Z",32),("2006-01-01T00:00:00Z",33),
    ("2009-01-01T00:00:00Z",34),("2012-07-01T00:00:00Z",35),("2015-07-01T00:00:00Z",36),
    ("2017-01-01T00:00:00Z",37),
)

def _as_utc_datetime(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise TimeReferenceError("datetime must be timezone-aware UTC/common-time")
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        v=value.strip().replace("Z","+00:00")
        dt=datetime.fromisoformat(v)
        if dt.tzinfo is None:
            raise TimeReferenceError("timestamp string must include UTC offset/Z")
        return dt.astimezone(timezone.utc)
    raise TimeReferenceError("time value must be timezone-aware datetime or ISO-8601 string")

_LEAPS_DT = tuple((_as_utc_datetime(s), off) for s,off in _LEAPS)
_GPS_EPOCH = datetime(1980,1,6,tzinfo=timezone.utc)
_UNIX_EPOCH = datetime(1970,1,1,tzinfo=timezone.utc)

def tai_minus_utc(utc_value):
    dt=_as_utc_datetime(utc_value)
    off=None
    for start, value in _LEAPS_DT:
        if dt >= start: off=value
        else: break
    if off is None:
        raise OutOfDomainError("built-in leap-second table supports UTC from 1972-01-01")
    return off


def utc_to_tai_seconds(utc_value):
    dt=_as_utc_datetime(utc_value)
    return (dt-_UNIX_EPOCH).total_seconds()+tai_minus_utc(dt)


def tai_seconds_to_utc(tai_seconds):
    # fixed-point because offset is piecewise constant in UTC
    dt=_UNIX_EPOCH+timedelta(seconds=float(tai_seconds)-37.0)
    for _ in range(4):
        off=tai_minus_utc(dt)
        dt=_UNIX_EPOCH+timedelta(seconds=float(tai_seconds)-off)
    return dt


def utc_to_gps_seconds(utc_value):
    dt=_as_utc_datetime(utc_value)
    if dt < _GPS_EPOCH:
        raise OutOfDomainError("GPS time is defined here from GPS epoch 1980-01-06")
    # GPS-UTC = (TAI-UTC)-19 s
    return (dt-_GPS_EPOCH).total_seconds() + (tai_minus_utc(dt)-19)


def utc_to_gps_week_tow(utc_value):
    sec=utc_to_gps_seconds(utc_value)
    week=int(sec//604800)
    return week, sec-week*604800.0


def gps_week_tow_to_utc(week, tow_s):
    if int(week) < 0 or not (0 <= float(tow_s) < 604800):
        raise OutOfDomainError("GPS week must be non-negative and TOW in [0,604800)")
    gps_seconds=int(week)*604800+float(tow_s)
    dt=_GPS_EPOCH+timedelta(seconds=gps_seconds-18.0)
    for _ in range(4):
        gps_utc=tai_minus_utc(dt)-19
        dt=_GPS_EPOCH+timedelta(seconds=gps_seconds-gps_utc)
    return dt


def common_time_to_elapsed(common_time, *, epoch=None):
    if epoch is None:
        raise ReferenceRequiredError("mission/FMI elapsed conversion requires epoch")
    return (_as_utc_datetime(common_time)-_as_utc_datetime(epoch)).total_seconds()


def elapsed_to_common_time(elapsed_s, *, epoch=None):
    if epoch is None:
        raise ReferenceRequiredError("elapsed→common-time conversion requires epoch")
    return _as_utc_datetime(epoch)+timedelta(seconds=float(elapsed_s))

# ---- Attitude / quaternion -------------------------------------------------
def _mtranspose(M): return tuple(tuple(M[j][i] for j in range(len(M))) for i in range(len(M[0])))

def _q_wxyz(q, order):
    if order == "wxyz": return tuple(float(x) for x in q)
    if order == "xyzw": return (float(q[3]),float(q[0]),float(q[1]),float(q[2]))
    raise QuaternionConventionError("quaternion_order must be 'wxyz' or 'xyzw'")

def _q_order(q, order):
    return q if order == "wxyz" else (q[1],q[2],q[3],q[0])

def quaternion_to_matrix(q, *, order="wxyz", algebra="Hamilton"):
    if algebra != "Hamilton":
        raise QuaternionConventionError("only Hamilton quaternion algebra is implemented")
    w,x,y,z=_q_wxyz(q,order)
    n=math.sqrt(w*w+x*x+y*y+z*z)
    if n == 0: raise OutOfDomainError("zero quaternion is invalid")
    w,x,y,z=w/n,x/n,y/n,z/n
    return (
      (1-2*(y*y+z*z), 2*(x*y-z*w),   2*(x*z+y*w)),
      (2*(x*y+z*w),   1-2*(x*x+z*z), 2*(y*z-x*w)),
      (2*(x*z-y*w),   2*(y*z+x*w),   1-2*(x*x+y*y)),
    )

def matrix_to_quaternion(R, *, order="wxyz", algebra="Hamilton"):
    if algebra != "Hamilton": raise QuaternionConventionError("only Hamilton quaternion algebra is implemented")
    tr=R[0][0]+R[1][1]+R[2][2]
    if tr > 0:
        s=math.sqrt(tr+1.0)*2; w=.25*s; x=(R[2][1]-R[1][2])/s; y=(R[0][2]-R[2][0])/s; z=(R[1][0]-R[0][1])/s
    elif R[0][0] > R[1][1] and R[0][0] > R[2][2]:
        s=math.sqrt(1+R[0][0]-R[1][1]-R[2][2])*2; w=(R[2][1]-R[1][2])/s; x=.25*s; y=(R[0][1]+R[1][0])/s; z=(R[0][2]+R[2][0])/s
    elif R[1][1] > R[2][2]:
        s=math.sqrt(1+R[1][1]-R[0][0]-R[2][2])*2; w=(R[0][2]-R[2][0])/s; x=(R[0][1]+R[1][0])/s; y=.25*s; z=(R[1][2]+R[2][1])/s
    else:
        s=math.sqrt(1+R[2][2]-R[0][0]-R[1][1])*2; w=(R[1][0]-R[0][1])/s; x=(R[0][2]+R[2][0])/s; y=(R[1][2]+R[2][1])/s; z=.25*s
    q=(w,x,y,z)
    n=math.sqrt(sum(v*v for v in q)); q=tuple(v/n for v in q)
    if q[0] < 0: q=tuple(-v for v in q)  # deterministic hemisphere
    return _q_order(q,order)

def _frame_matrix(dst, src, kind):
    if dst == src: return ((1.,0.,0.),(0.,1.,0.),(0.,0.,1.))
    if kind == "nav" and {src,dst} == {"NED","ENU"}: return T_NED_ENU
    if kind == "body" and {src,dst} == {"FRD","FLU"}: return T_FRD_FLU
    raise FrameMismatchError(f"unsupported {kind} frame conversion {src}->{dst}")

def convert_quaternion(q, *, source_metadata=None, target_metadata=None):
    required=("quaternion_order","quaternion_algebra","rotation_direction","active_or_passive","body_frame","navigation_frame")
    sm=dict(source_metadata or {}); tm=dict(target_metadata or {})
    missing=[k for k in required if k not in sm]
    if missing: raise QuaternionConventionError("missing quaternion metadata: "+", ".join(missing))
    for k in required:
        tm.setdefault(k,sm[k])
    if sm["quaternion_algebra"] != "Hamilton" or tm["quaternion_algebra"] != "Hamilton":
        raise QuaternionConventionError("only Hamilton quaternion algebra is implemented")
    if sm["rotation_direction"] not in ("body_to_nav","nav_to_body") or tm["rotation_direction"] not in ("body_to_nav","nav_to_body"):
        raise QuaternionConventionError("rotation_direction must be body_to_nav or nav_to_body")
    if sm["active_or_passive"] not in ("active","passive") or tm["active_or_passive"] not in ("active","passive"):
        raise QuaternionConventionError("active_or_passive must be active or passive")
    R=quaternion_to_matrix(q,order=sm["quaternion_order"],algebra=sm["quaternion_algebra"])
    if sm["active_or_passive"] == "passive": R=_mtranspose(R)
    if sm["rotation_direction"] == "nav_to_body": R=_mtranspose(R)
    Tn=_frame_matrix(tm["navigation_frame"],sm["navigation_frame"],"nav")
    Tb=_frame_matrix(tm["body_frame"],sm["body_frame"],"body")
    R=matmul(matmul(Tn,R),_mtranspose(Tb))
    if tm["rotation_direction"] == "nav_to_body": R=_mtranspose(R)
    if tm["active_or_passive"] == "passive": R=_mtranspose(R)
    return matrix_to_quaternion(R,order=tm["quaternion_order"],algebra=tm["quaternion_algebra"])

def quaternion_covariance(P, q, *, source_metadata=None, target_metadata=None, eps=1e-7):
    """First-order covariance propagation using a numeric 4x4 Jacobian."""
    if len(P)!=4 or any(len(r)!=4 for r in P): raise ConversionError("quaternion covariance must be 4x4")
    q0=tuple(float(v) for v in q)
    y0=convert_quaternion(q0,source_metadata=source_metadata,target_metadata=target_metadata)
    J=[]
    for i in range(4):
        qp=list(q0); qp[i]+=eps
        yp=convert_quaternion(qp,source_metadata=source_metadata,target_metadata=target_metadata)
        # align quaternion sign before differencing
        if sum(a*b for a,b in zip(yp,y0)) < 0: yp=tuple(-v for v in yp)
        J.append(tuple((yp[j]-y0[j])/eps for j in range(4)))
    J=tuple(tuple(J[col][row] for col in range(4)) for row in range(4))
    return matmul(matmul(J,P),_mtranspose(J))

# ---- Explicit Body <-> Navigation + Euler conventions ----------------------
_EULER_NEXT_AXIS=(1,2,0,1)
_EULER_AXES={
    "XYZ":(2,1,0,1), "XZY":(1,0,0,1), "YXZ":(2,0,0,1),
    "YZX":(0,1,0,1), "ZXY":(1,1,0,1), "ZYX":(0,0,0,1),
}

def _euler_tuple(sequence):
    seq=str(sequence).upper()
    if seq not in _EULER_AXES:
        raise EulerSequenceError("supported intrinsic Tait-Bryan sequences: XYZ, XZY, YXZ, YZX, ZXY, ZYX")
    return _EULER_AXES[seq]

def euler_to_matrix(angles_rad, *, sequence="ZYX"):
    """Intrinsic active Euler/Tait-Bryan angles -> rotation matrix.

    Angles are supplied in sequence order. Example ZYX = (yaw, pitch, roll).
    """
    ai,aj,ak=(float(v) for v in angles_rad)
    firstaxis,parity,repetition,frame=_euler_tuple(sequence)
    i=firstaxis; j=_EULER_NEXT_AXIS[i+parity]; k=_EULER_NEXT_AXIS[i-parity+1]
    if repetition: raise EulerSequenceError("repeated-axis Euler sequences are not part of this aviation profile")
    if frame: ai,ak=ak,ai
    if parity: ai,aj,ak=-ai,-aj,-ak
    si,sj,sk=math.sin(ai),math.sin(aj),math.sin(ak)
    ci,cj,ck=math.cos(ai),math.cos(aj),math.cos(ak)
    cc,cs=ci*ck,ci*sk; sc,ss=si*ck,si*sk
    M=[[0.0]*3 for _ in range(3)]
    M[i][i]=cj*ck; M[i][j]=sj*sc-cs; M[i][k]=sj*cc+ss
    M[j][i]=cj*sk; M[j][j]=sj*ss+cc; M[j][k]=sj*cs-sc
    M[k][i]=-sj; M[k][j]=cj*si; M[k][k]=cj*ci
    return tuple(tuple(r) for r in M)

def matrix_to_euler(R, *, sequence="ZYX"):
    """Rotation matrix -> intrinsic Tait-Bryan angles in sequence order."""
    firstaxis,parity,repetition,frame=_euler_tuple(sequence)
    i=firstaxis; j=_EULER_NEXT_AXIS[i+parity]; k=_EULER_NEXT_AXIS[i-parity+1]
    if repetition: raise EulerSequenceError("repeated-axis Euler sequences are not part of this aviation profile")
    cy=math.sqrt(float(R[i][i])**2+float(R[j][i])**2)
    if cy > 1e-12:
        ax=math.atan2(R[k][j],R[k][k]); ay=math.atan2(-R[k][i],cy); az=math.atan2(R[j][i],R[i][i])
    else:
        ax=math.atan2(-R[j][k],R[j][j]); ay=math.atan2(-R[k][i],cy); az=0.0
    if parity: ax,ay,az=-ax,-ay,-az
    if frame: ax,az=az,ax
    return (ax,ay,az)

def euler_to_quaternion(angles_rad, *, sequence="ZYX", order="wxyz", algebra="Hamilton"):
    return matrix_to_quaternion(euler_to_matrix(angles_rad,sequence=sequence),order=order,algebra=algebra)

def quaternion_to_euler(q, *, sequence="ZYX", order="wxyz", algebra="Hamilton"):
    return matrix_to_euler(quaternion_to_matrix(q,order=order,algebra=algebra),sequence=sequence)

def body_to_navigation_vector(vector_body, *, rotation_matrix=None, quaternion=None, quaternion_order="wxyz", algebra="Hamilton"):
    if rotation_matrix is None:
        if quaternion is None: raise ReferenceRequiredError("Body→Navigation requires rotation_matrix or quaternion")
        rotation_matrix=quaternion_to_matrix(quaternion,order=quaternion_order,algebra=algebra)
    return matvec(rotation_matrix,vector_body)

def navigation_to_body_vector(vector_nav, *, rotation_matrix=None, quaternion=None, quaternion_order="wxyz", algebra="Hamilton"):
    if rotation_matrix is None:
        if quaternion is None: raise ReferenceRequiredError("Navigation→Body requires rotation_matrix or quaternion")
        rotation_matrix=quaternion_to_matrix(quaternion,order=quaternion_order,algebra=algebra)
    return matvec(transpose(rotation_matrix),vector_nav)

def body_navigation_covariance(P, *, rotation_matrix=None, quaternion=None, quaternion_order="wxyz", algebra="Hamilton", direction="body_to_nav"):
    if rotation_matrix is None:
        if quaternion is None: raise ReferenceRequiredError("covariance transform requires rotation_matrix or quaternion")
        rotation_matrix=quaternion_to_matrix(quaternion,order=quaternion_order,algebra=algebra)
    if direction=="body_to_nav": T=rotation_matrix
    elif direction=="nav_to_body": T=transpose(rotation_matrix)
    else: raise FrameMismatchError("direction must be body_to_nav or nav_to_body")
    return transform_covariance(P,T)

def _vee_skew(S):
    return (0.5*(S[2][1]-S[1][2]),0.5*(S[0][2]-S[2][0]),0.5*(S[1][0]-S[0][1]))

def _euler_rate_jacobian(angles_rad, *, sequence="ZYX", eps=1e-7):
    a=tuple(float(v) for v in angles_rad); R=euler_to_matrix(a,sequence=sequence); RT=transpose(R)
    cols=[]
    for n in range(3):
        ap=list(a); am=list(a); ap[n]+=eps; am[n]-=eps
        Rp=euler_to_matrix(ap,sequence=sequence); Rm=euler_to_matrix(am,sequence=sequence)
        Rdot=tuple(tuple((Rp[i][j]-Rm[i][j])/(2*eps) for j in range(3)) for i in range(3))
        omega_skew=matmul(RT,Rdot)
        cols.append(_vee_skew(omega_skew))
    return tuple(tuple(cols[c][r] for c in range(3)) for r in range(3))

def _solve3(A,b):
    M=[list(map(float,row))+[float(b[i])] for i,row in enumerate(A)]
    for col in range(3):
        pivot=max(range(col,3),key=lambda r:abs(M[r][col]))
        if abs(M[pivot][col]) < 1e-10:
            raise EulerSequenceError("Euler-rate mapping is singular/near gimbal lock for this attitude")
        M[col],M[pivot]=M[pivot],M[col]
        d=M[col][col]; M[col]=[v/d for v in M[col]]
        for r in range(3):
            if r==col: continue
            f=M[r][col]; M[r]=[M[r][j]-f*M[col][j] for j in range(4)]
    return tuple(M[i][3] for i in range(3))

def euler_rates_to_body_rates(euler_rates_rad_s, angles_rad, *, sequence="ZYX"):
    """Euler angle rates (sequence order) -> body angular rates p,q,r."""
    J=_euler_rate_jacobian(angles_rad,sequence=sequence)
    return matvec(J,euler_rates_rad_s)

def body_rates_to_euler_rates(body_rates_rad_s, angles_rad, *, sequence="ZYX"):
    """Body angular rates p,q,r -> Euler angle rates (sequence order)."""
    J=_euler_rate_jacobian(angles_rad,sequence=sequence)
    return _solve3(J,body_rates_rad_s)

def euler_body_rate_covariance(P, angles_rad, *, sequence="ZYX", direction="euler_to_body"):
    J=_euler_rate_jacobian(angles_rad,sequence=sequence)
    if direction=="euler_to_body": return transform_covariance(P,J)
    if direction=="body_to_euler":
        inv=tuple(_solve3(J,tuple(1.0 if i==j else 0.0 for i in range(3))) for j in range(3))
        inv=transpose(inv)
        return transform_covariance(P,inv)
    raise ConversionError("direction must be euler_to_body or body_to_euler",code="EULER_SEQUENCE_MISSING")

# ---- Barometric altitude / references -------------------------------------
def isa_pressure_at_altitude(h_m, *, p0=101325.0, T0=288.15, L=0.0065, Rd=287.05287, g0=9.80665):
    if h_m >= T0/L: raise OutOfDomainError("altitude outside constant-lapse ISA layer")
    return p0*(1-L*h_m/T0)**(g0/(Rd*L))

def qnh_to_qfe(qnh_pa, *, elevation_m=None, atmospheric_model="ISA"):
    if elevation_m is None: raise ReferenceRequiredError("QNH→QFE requires aerodrome/reference elevation")
    if atmospheric_model != "ISA": raise ReferenceRequiredError("only explicit atmospheric_model='ISA' is implemented")
    return isa_pressure_at_altitude(elevation_m,p0=qnh_pa)

def qfe_to_qnh(qfe_pa, *, elevation_m=None, atmospheric_model="ISA"):
    if elevation_m is None: raise ReferenceRequiredError("QFE→QNH requires aerodrome/reference elevation")
    if atmospheric_model != "ISA": raise ReferenceRequiredError("only explicit atmospheric_model='ISA' is implemented")
    if qfe_pa <= 0: raise OutOfDomainError("QFE must be positive")
    T0=288.15; L=.0065; Rd=287.05287; g0=9.80665
    factor=(1-L*elevation_m/T0)**(g0/(Rd*L))
    return qfe_pa/factor

def pressure_reference(value_pa, *, source=None, target=None, elevation_m=None, atmospheric_model="ISA"):
    """Convert altimeter reference pressure settings with explicit context.

    QNE is the standard setting 101325 Pa and is not inferred from QNH/QFE without
    declaring that the caller intentionally requests the standard reference.
    """
    if source not in ("QNH","QFE","QNE") or target not in ("QNH","QFE","QNE"):
        raise ReferenceRequiredError("source/target must explicitly be QNH, QFE or QNE")
    if source==target: return float(value_pa)
    if source=="QNH" and target=="QFE": return qnh_to_qfe(value_pa,elevation_m=elevation_m,atmospheric_model=atmospheric_model)
    if source=="QFE" and target=="QNH": return qfe_to_qnh(value_pa,elevation_m=elevation_m,atmospheric_model=atmospheric_model)
    if target=="QNE":
        if elevation_m is None: raise ReferenceRequiredError("QNH/QFE→QNE requires elevation/reference context")
        return 101325.0
    if source=="QNE":
        if elevation_m is None: raise ReferenceRequiredError("QNE→QNH/QFE requires elevation/reference context")
        # Under declared ISA, QNE corresponds to standard sea-level pressure.
        return 101325.0 if target=="QNH" else qnh_to_qfe(101325.0,elevation_m=elevation_m,atmospheric_model=atmospheric_model)
    raise ConversionError("unsupported altimeter-reference conversion",code="QNH_QFE_QNE_MISMATCH")

def density_altitude_isa(rho_actual, *, rho0=1.225, T0=288.15, L=0.0065, Rd=287.05287, g0=9.80665):
    if rho_actual <= 0: raise OutOfDomainError("density must be positive")
    # rho/rho0=(1-Lh/T0)^(g/(R L)-1), inverse in troposphere.
    expo=g0/(Rd*L)-1.0
    return T0/L*(1-(rho_actual/rho0)**(1/expo))

# ---- Air data --------------------------------------------------------------
def impact_pressure(total_pressure_pa, static_pressure_pa):
    if total_pressure_pa < static_pressure_pa or static_pressure_pa <= 0:
        raise PressureTypeMismatchError("total pressure must be >= positive static pressure")
    return total_pressure_pa-static_pressure_pa

def total_pressure(static_pressure_pa, impact_pressure_pa):
    if static_pressure_pa <= 0 or impact_pressure_pa < 0:
        raise OutOfDomainError("pressures outside valid domain")
    return static_pressure_pa+impact_pressure_pa

def mach_from_impact_pressure(impact_pressure_pa, static_pressure_pa, gamma=1.4):
    if impact_pressure_pa < 0 or static_pressure_pa <= 0:
        raise OutOfDomainError("impact pressure must be >=0 and static pressure >0")
    return math.sqrt(2/(gamma-1)*((1+impact_pressure_pa/static_pressure_pa)**((gamma-1)/gamma)-1))

def impact_pressure_from_mach(mach, static_pressure_pa, gamma=1.4):
    if not (0 <= mach < 1): raise OutOfDomainError("implemented pitot relation is subsonic only (0<=Mach<1)")
    if static_pressure_pa <= 0: raise OutOfDomainError("static pressure must be positive")
    return static_pressure_pa*((1+(gamma-1)/2*mach*mach)**(gamma/(gamma-1))-1)

def cas_from_impact_pressure(impact_pressure_pa, *, p0=101325.0, a0=340.294, gamma=1.4):
    if impact_pressure_pa < 0 or p0 <= 0: raise OutOfDomainError("invalid pressure")
    return a0*math.sqrt(2/(gamma-1)*((1+impact_pressure_pa/p0)**((gamma-1)/gamma)-1))

def impact_pressure_from_cas(cas, *, p0=101325.0, a0=340.294, gamma=1.4):
    if cas < 0 or cas >= a0: raise OutOfDomainError("implemented CAS relation is subsonic only")
    m=cas/a0
    return p0*((1+(gamma-1)/2*m*m)**(gamma/(gamma-1))-1)

def ias_to_cas(ias, *, calibration=None):
    if calibration is None:
        raise AirDataCalibrationError("IAS→CAS requires instrument/position calibration")
    if callable(calibration): return float(calibration(float(ias)))
    if isinstance(calibration, dict):
        scale=float(calibration.get("scale",1.0)); offset=float(calibration.get("offset",0.0))
        return scale*float(ias)+offset
    raise AirDataCalibrationError("calibration must be callable or {'scale','offset'} mapping")

def cas_to_ias(cas, *, calibration=None):
    if calibration is None:
        raise AirDataCalibrationError("CAS→IAS requires invertible calibration")
    if isinstance(calibration, dict):
        scale=float(calibration.get("scale",1.0)); offset=float(calibration.get("offset",0.0))
        if scale == 0: raise AirDataCalibrationError("calibration scale cannot be zero")
        return (float(cas)-offset)/scale
    raise AirDataCalibrationError("inverse CAS→IAS supports affine {'scale','offset'} calibration")

def cas_to_eas(cas, *, static_pressure_pa=None, gamma=1.4, p0=101325.0, a0=340.294, rho0=1.225):
    if static_pressure_pa is None: raise ReferenceRequiredError("CAS→EAS requires static_pressure_pa")
    qc=impact_pressure_from_cas(cas,p0=p0,a0=a0,gamma=gamma)
    m=mach_from_impact_pressure(qc,static_pressure_pa,gamma=gamma)
    # q_dynamic = gamma/2 * p * M^2 for ideal gas; EAS=sqrt(2q/rho0)
    qdyn=.5*gamma*static_pressure_pa*m*m
    return math.sqrt(2*qdyn/rho0)

def eas_to_cas(eas, *, static_pressure_pa=None, gamma=1.4, p0=101325.0, a0=340.294, rho0=1.225):
    if static_pressure_pa is None: raise ReferenceRequiredError("EAS→CAS requires static_pressure_pa")
    if eas < 0: raise OutOfDomainError("EAS must be non-negative")
    qdyn=.5*rho0*eas*eas
    m=math.sqrt(2*qdyn/(gamma*static_pressure_pa))
    qc=impact_pressure_from_mach(m,static_pressure_pa,gamma=gamma)
    return cas_from_impact_pressure(qc,p0=p0,a0=a0,gamma=gamma)

def cas_to_tas(cas, *, static_pressure_pa=None, temperature_k=None, density=None, gamma=1.4, gas_constant=287.05287):
    eas=cas_to_eas(cas,static_pressure_pa=static_pressure_pa,gamma=gamma)
    if density is None:
        if temperature_k is None: raise ReferenceRequiredError("CAS→TAS requires density or temperature_k")
        density=static_pressure_pa/(gas_constant*temperature_k)
    return eas_to_tas(eas,density)

def tas_to_cas(tas, *, static_pressure_pa=None, temperature_k=None, density=None, gamma=1.4, gas_constant=287.05287):
    if density is None:
        if static_pressure_pa is None or temperature_k is None: raise ReferenceRequiredError("TAS→CAS requires density or static_pressure_pa+temperature_k")
        density=static_pressure_pa/(gas_constant*temperature_k)
    eas=tas_to_eas(tas,density)
    return eas_to_cas(eas,static_pressure_pa=static_pressure_pa,gamma=gamma)

# ---- Data semantic role adapter ---------------------------------------------
_DATA_SEMANTIC_ROLES={"current_state","prior","reference","truth"}
_DATA_SEMANTIC_ALIASES={
    "current":"current_state","state":"current_state","current state":"current_state",
    "prior_state":"prior","ref":"reference","ground_truth":"truth","ground truth":"truth",
}

def normalize_data_semantic_role(role):
    if role is None: raise DataSemanticError("semantic role is required")
    key=str(role).strip().lower().replace("-","_")
    key=_DATA_SEMANTIC_ALIASES.get(key,key)
    if key not in _DATA_SEMANTIC_ROLES:
        raise DataSemanticError("role must be one of current_state, prior, reference, truth")
    return key

def validate_data_semantics(*, source_role=None, target_role=None, allow_relabel=False, provenance=None):
    src=normalize_data_semantic_role(source_role); dst=normalize_data_semantic_role(target_role if target_role is not None else source_role)
    if src != dst:
        if not allow_relabel:
            raise DataSemanticError("cross-role semantic relabel requires allow_relabel=True and provenance")
        if not provenance:
            raise ReferenceRequiredError("semantic relabel requires provenance/reason metadata")
    return {"source_role":src,"target_role":dst,"relabelled":src!=dst,"provenance":provenance}

def adapt_data_semantics(value, *, source_role=None, target_role=None, allow_relabel=False, provenance=None, metadata=None):
    """Attach/validate current_state/prior/reference/truth semantics without altering value."""
    audit=validate_data_semantics(source_role=source_role,target_role=target_role,allow_relabel=allow_relabel,provenance=provenance)
    out_meta=dict(metadata or {}); out_meta.update(audit); out_meta["implementation_status"]="complete"
    return {"value":value,"semantics":audit["target_role"],"metadata":out_meta}

# A compact machine-readable implementation registry for edge/runtime discovery.
AVIATION_ADAPTER_REGISTRY = {
 "AviationUnitAdapter":{"implementation_status":"complete","api":["convert"]},
 "NavigationFrameAdapter":{"implementation_status":"complete","api":["ned_to_enu","enu_to_ned","frd_to_flu","flu_to_frd","ecef_delta_to_enu","enu_delta_to_ecef","ecef_enu_covariance","ecef_to_local_enu","local_enu_to_ecef","body_to_navigation_vector","navigation_to_body_vector","body_navigation_covariance"]},
 "GeodesyAdapter":{"implementation_status":"complete_optional_pyproj","api":["wgs84_to_ecef","ecef_to_wgs84","wgs84_to_utm","utm_to_wgs84","transform_crs","transform_datum"]},
 "VerticalReferenceAdapter":{"implementation_status":"complete","api":["ellipsoidal_to_orthometric","orthometric_to_ellipsoidal","msl_to_agl","agl_to_msl","ned_down_to_altitude","altitude_to_ned_down"]},
 "BaroAltitudeAdapter":{"implementation_status":"complete_ISA_lower_layer","api":["pressure_reference","qnh_to_qfe","qfe_to_qnh","pressure_altitude_lapse","flight_level_from_pressure_altitude","density_altitude_isa"]},
 "HeadingConventionAdapter":{"implementation_status":"complete","api":["heading_nav_to_enu","heading_enu_to_nav","true_to_magnetic","magnetic_to_true","true_to_grid","grid_to_true"]},
 "WindConventionAdapter":{"implementation_status":"complete","api":["wind_from_to_vector","wind_components_along_track","wind_triangle"]},
 "AirDataAdapter":{"implementation_status":"complete_subsonic","api":["ias_to_cas","cas_to_ias","cas_to_eas","eas_to_cas","cas_to_tas","tas_to_cas","mach_to_tas","tas_to_mach","impact_pressure","total_pressure","dynamic_pressure"]},
 "AttitudeAdapter":{"implementation_status":"complete_tait_bryan","api":["convert_quaternion","quaternion_covariance","quaternion_to_matrix","matrix_to_quaternion","euler_to_matrix","matrix_to_euler","euler_to_quaternion","quaternion_to_euler","euler_rates_to_body_rates","body_rates_to_euler_rates","euler_body_rate_covariance"]},
 "TimeScaleAdapter":{"implementation_status":"complete_table_through_2017","api":["utc_to_tai_seconds","tai_seconds_to_utc","utc_to_gps_week_tow","gps_week_tow_to_utc","common_time_to_elapsed","elapsed_to_common_time"]},
 "LogQuantityAdapter":{"implementation_status":"complete","api":["pressure_to_log_ratio","log_ratio_to_pressure","pressure_log_uncertainty","log_pressure_uncertainty"]},
 "DataSemanticAdapter":{"implementation_status":"complete","api":["normalize_data_semantic_role","validate_data_semantics","adapt_data_semantics"]},
}



# ---- Generic first-order uncertainty helpers -------------------------------
def propagate_uncertainty_numeric(func, x, u_x, *, eps=None):
    """First-order scalar uncertainty propagation u_y=|df/dx|u_x."""
    x=float(x); u_x=float(u_x)
    if u_x < 0: raise ConversionError("uncertainty must be non-negative",code="UNCERTAINTY_INVALID")
    h=eps if eps is not None else max(1e-8,abs(x)*1e-7)
    dy=(float(func(x+h))-float(func(x-h)))/(2*h)
    return abs(dy)*u_x


def propagate_covariance_numeric(func, x, P, *, eps=1e-6):
    """First-order covariance propagation P_y=J P_x J^T for small vectors."""
    x=tuple(float(v) for v in x); n=len(x)
    if len(P)!=n or any(len(r)!=n for r in P):
        raise ConversionError("input covariance shape does not match input vector",code="UNCERTAINTY_INVALID")
    y0=tuple(float(v) for v in func(x)); m=len(y0)
    cols=[]
    for i in range(n):
        xp=list(x); xm=list(x); xp[i]+=eps; xm[i]-=eps
        yp=tuple(float(v) for v in func(tuple(xp))); ym=tuple(float(v) for v in func(tuple(xm)))
        if len(yp)!=m or len(ym)!=m: raise ConversionError("transform output size changed")
        cols.append(tuple((yp[j]-ym[j])/(2*eps) for j in range(m)))
    J=tuple(tuple(cols[i][j] for i in range(n)) for j in range(m))
    return matmul(matmul(J,P),transpose(J))
