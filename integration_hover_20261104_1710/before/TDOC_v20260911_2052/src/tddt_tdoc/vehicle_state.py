from __future__ import annotations

import math
from typing import Any, Mapping


def _finite(v: Any, default: float | None = None) -> float | None:
    try:
        x=float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo,min(hi,float(x)))


def _group(snapshot: Mapping[str,Any] | None, name: str) -> tuple[dict[str,Any],dict[str,Any]]:
    snap=snapshot if isinstance(snapshot,Mapping) else {}
    g=((snap.get('groups') or {}).get(name) or {}) if isinstance(snap.get('groups'),Mapping) else {}
    p=g.get('payload') if isinstance(g,Mapping) else {}
    return (dict(g) if isinstance(g,Mapping) else {}, dict(p) if isinstance(p,Mapping) else {})


def _l5_result(obj: Mapping[str,Any] | None) -> dict[str,Any]:
    if not isinstance(obj,Mapping): return {}
    if isinstance(obj.get('result'),Mapping): return dict(obj['result'])
    return dict(obj)


def quaternion_to_euler(qw:Any,qx:Any,qy:Any,qz:Any) -> dict[str,float|None]:
    w,x,y,z=(_finite(qw),_finite(qx),_finite(qy),_finite(qz))
    if None in (w,x,y,z):
        return {'roll_rad':None,'pitch_rad':None,'yaw_rad':None,'roll_deg':None,'pitch_deg':None,'yaw_deg':None}
    n=math.sqrt(w*w+x*x+y*y+z*z)
    if n<=1e-12:
        return {'roll_rad':None,'pitch_rad':None,'yaw_rad':None,'roll_deg':None,'pitch_deg':None,'yaw_deg':None}
    w,x,y,z=w/n,x/n,y/n,z/n
    sinr=2*(w*x+y*z); cosr=1-2*(x*x+y*y); roll=math.atan2(sinr,cosr)
    sinp=2*(w*y-z*x); pitch=math.copysign(math.pi/2,sinp) if abs(sinp)>=1 else math.asin(sinp)
    siny=2*(w*z+x*y); cosy=1-2*(y*y+z*z); yaw=math.atan2(siny,cosy)
    return {'roll_rad':roll,'pitch_rad':pitch,'yaw_rad':yaw,
            'roll_deg':math.degrees(roll),'pitch_deg':math.degrees(pitch),'yaw_deg':math.degrees(yaw)}


def _anchor(l5: Mapping[str,Any], servo: str, key: str, fallback: float) -> float:
    try:
        v=((l5.get('phoenix') or {}).get('pwm_anchors') or {}).get(servo,{}).get(key)
        return float(v) if v is not None else fallback
    except Exception:
        return fallback


def _surface_norm(pwm: float | None, mn:float, trim:float, mx:float) -> float | None:
    if pwm is None:return None
    span=(mx-trim) if pwm>=trim else (trim-mn)
    if span<=0:return None
    return _clamp((pwm-trim)/span,-1.0,1.0)


def _motor_norm(pwm: float | None, mn:float, mx:float) -> float | None:
    if pwm is None or mx<=mn:return None
    return _clamp((pwm-mn)/(mx-mn),0.0,1.0)


def phoenix_state_from_ptlib_snapshot(snapshot: Mapping[str,Any] | None, layer5: Mapping[str,Any] | None = None,
                                      execution: Mapping[str,Any] | None = None) -> dict[str,Any]:
    """Normalize PT_LIB I/O evidence into a Phoenix decision-state record.

    SERVO_OUTPUT_RAW is actuator feedback from ArduPilot.  The current PT_LIB docs do
    not provide a measured rotor-RPM sensor or measured elevon angle, therefore those
    two physical quantities are reported as command-based estimates when enough
    Phoenix limits are available, never as measured values.
    """
    l5=_l5_result(layer5)
    ag,att=_group(snapshot,'ATTITUDE'); sg,air=_group(snapshot,'AIRSPEED'); pg,pos=_group(snapshot,'POSITION'); vg,vel=_group(snapshot,'VELOCITY'); xg,act=_group(snapshot,'ACTUATOR')
    roll=_finite(att.get('roll')); pitch=_finite(att.get('pitch')); yaw=_finite(att.get('yaw'))
    vx=_finite(vel.get('vx')); vy=_finite(vel.get('vy')); vz=_finite(vel.get('vz'))
    # GLOBAL_POSITION_INT velocities are cm/s.
    vn=(vx/100.0 if vx is not None else None); ve=(vy/100.0 if vy is not None else None); vd=(vz/100.0 if vz is not None else None)
    groundspeed=_finite(air.get('groundspeed'))
    if groundspeed is None and vn is not None and ve is not None: groundspeed=math.hypot(vn,ve)
    servo={i:_finite(act.get(f'servo{i}_raw')) for i in range(1,5)}
    a1=(_anchor(l5,'SERVO1','min',1000),_anchor(l5,'SERVO1','trim',1500),_anchor(l5,'SERVO1','max',2000))
    a2=(_anchor(l5,'SERVO2','min',1000),_anchor(l5,'SERVO2','trim',1500),_anchor(l5,'SERVO2','max',2000))
    a3=(_anchor(l5,'SERVO3','min',1000),_anchor(l5,'SERVO3','trim',1000),_anchor(l5,'SERVO3','max',2000))
    a4=(_anchor(l5,'SERVO4','min',1000),_anchor(l5,'SERVO4','trim',1000),_anchor(l5,'SERVO4','max',2000))
    left_surface=_surface_norm(servo[1],*a1); right_surface=_surface_norm(servo[2],*a2)
    left_thr=_motor_norm(servo[3],a3[0],a3[2]); right_thr=_motor_norm(servo[4],a4[0],a4[2])
    try:elevon_limit=float(((l5.get('phoenix') or {}).get('limits') or {}).get('elevon_rad'))
    except Exception:elevon_limit=None
    try:motor_max=float(((l5.get('phoenix') or {}).get('limits') or {}).get('motor_speed_rad_s'))
    except Exception:motor_max=None
    le=(left_surface*elevon_limit if left_surface is not None and elevon_limit is not None else None)
    re=(right_surface*elevon_limit if right_surface is not None and elevon_limit is not None else None)
    wl=(left_thr*motor_max if left_thr is not None and motor_max is not None else None)
    wr=(right_thr*motor_max if right_thr is not None and motor_max is not None else None)
    surface_output_active=bool(any(v is not None and abs(v)>0.02 for v in (left_surface,right_surface)))
    propulsion_output_active=bool(any(v is not None and v>0.02 for v in (left_thr,right_thr)))
    raw_output_active=bool(surface_output_active or propulsion_output_active)
    ex=dict(execution) if isinstance(execution,Mapping) else {}
    authorized_actuation=bool(ex.get('actuation_executed') is True and str(ex.get('status') or '').upper()=='EXECUTED')
    surface_active=bool(authorized_actuation and surface_output_active)
    propulsion_active=bool(authorized_actuation and propulsion_output_active)
    act_active=bool(authorized_actuation and raw_output_active)
    moving=bool(groundspeed is not None and abs(groundspeed)>0.25)
    if moving:
        motion_state='MOVING'
    elif act_active:
        motion_state='ACTUATED_NO_TRANSLATION'
    elif raw_output_active:
        motion_state='TRIM_ONLY_HELD'
    else:
        motion_state='NO_ACTUATION_STATIONARY'
    return {
      'schema':'TDOC-Phoenix-decision-vehicle-state-v1','source':'PT_LIB/io_snapshot_session','source_semantics':'live SITL sensor/actuator boundary; ArduPilot owns physical mixer',
      'attitude':{'roll_rad':roll,'pitch_rad':pitch,'yaw_rad':yaw,'roll_deg':math.degrees(roll) if roll is not None else None,'pitch_deg':math.degrees(pitch) if pitch is not None else None,'yaw_deg':math.degrees(yaw) if yaw is not None else None,
                  'roll_rate_rad_s':_finite(att.get('rollspeed')),'pitch_rate_rad_s':_finite(att.get('pitchspeed')),'yaw_rate_rad_s':_finite(att.get('yawspeed')),'age_s':_finite(ag.get('age_s')),'source_message':ag.get('source_message')},
      'speed':{'airspeed_m_s':_finite(air.get('airspeed')),'groundspeed_m_s':groundspeed,'climb_m_s':_finite(air.get('climb')),'heading_deg':_finite(air.get('heading')),
               'velocity_n_m_s':vn,'velocity_e_m_s':ve,'velocity_d_m_s':vd,'age_s':_finite(sg.get('age_s')),'source_message':sg.get('source_message')},
      'position':{'lat_deg':(_finite(pos.get('lat'))/1e7 if _finite(pos.get('lat')) is not None else None),'lon_deg':(_finite(pos.get('lon'))/1e7 if _finite(pos.get('lon')) is not None else None),
                  'alt_msl_m':(_finite(pos.get('alt'))/1000.0 if _finite(pos.get('alt')) is not None else None),'relative_alt_m':(_finite(pos.get('relative_alt'))/1000.0 if _finite(pos.get('relative_alt')) is not None else None),'age_s':_finite(pg.get('age_s'))},
      'actuators':{
        'feedback_source':'ArduPilot SERVO_OUTPUT_RAW','feedback_age_s':_finite(xg.get('age_s')),
        'left_elevon':{'servo':'SERVO1','pwm_us':servo[1],'normalized_output':left_surface,'estimated_angle_rad':le,'estimated_angle_deg':math.degrees(le) if le is not None else None,'angle_measured':False},
        'right_elevon':{'servo':'SERVO2','pwm_us':servo[2],'normalized_output':right_surface,'estimated_angle_rad':re,'estimated_angle_deg':math.degrees(re) if re is not None else None,'angle_measured':False},
        'left_motor':{'servo':'SERVO3','pwm_us':servo[3],'normalized_throttle_output':left_thr,'estimated_speed_rad_s':wl,'estimated_speed_rpm':(wl*60/(2*math.pi) if wl is not None else None),'speed_measured':False},
        'right_motor':{'servo':'SERVO4','pwm_us':servo[4],'normalized_throttle_output':right_thr,'estimated_speed_rad_s':wr,'estimated_speed_rpm':(wr*60/(2*math.pi) if wr is not None else None),'speed_measured':False},
        'physical_mapping':{'SERVO1':'left_elevon','SERVO2':'right_elevon','SERVO3':'left_motor','SERVO4':'right_motor'},
        'estimate_note':'Elevon angle and motor speed are command-based estimates only when Layer5 Phoenix limits are available; PT_LIB documents SERVO_OUTPUT_RAW feedback but no direct elevon-angle/RPM sensor.'},
      'motion':{'moving':moving,'translation_active':moving,'surface_actuation_active':surface_active,'propulsion_actuation_active':propulsion_active,
                'actuator_output_active':act_active,'authorized_actuation':authorized_actuation,
                'raw_surface_output_active':surface_output_active,'raw_propulsion_output_active':propulsion_output_active,'raw_output_active':raw_output_active,
                'state':motion_state,
                'propulsion_state':'PROPULSION_ACTIVE' if propulsion_active else ('TRIM_OUTPUT_ONLY' if propulsion_output_active else 'PROPULSION_IDLE'),
                'semantic_note':'SERVO/RPM trim or held outputs are not counted as Layer5-authorized actuation unless Layer6 status=EXECUTED and actuation_executed=true'},
    }
