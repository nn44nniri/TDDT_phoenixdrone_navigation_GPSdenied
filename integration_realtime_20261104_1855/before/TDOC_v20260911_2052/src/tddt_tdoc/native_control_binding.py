"""Native phase/mode mapping and FCU PWM feedback conversion for TDOC."""
from __future__ import annotations
import math

PHASE_MODES = {"vertical_takeoff":"QHOVER", "hover":"QHOVER",
               "transition_forward":"FBWA", "climb":"FBWA", "cruise":"FBWA",
               "turn":"FBWA", "approach":"FBWA", "transition_back":"QHOVER",
               "vertical_landing":"QHOVER"}


def actuator_feedback(status, parameters, elevon_limit):
    """FCU output estimates from the exact Phoenix PWM contract, not measured RPM."""
    raw = status.get("servo_output_raw") or {}
    out = {}
    for channel, target in ((1,"left_elevon_rad"),(2,"right_elevon_rad"),
                            (3,"left_motor_norm"),(4,"right_motor_norm")):
        value = float(raw[f"servo{channel}_raw"])
        prefix = f"SERVO{channel}"
        lo, trim, hi = (float(parameters[prefix+"_"+x]) for x in ("MIN","TRIM","MAX"))
        if not all(math.isfinite(x) for x in (value,lo,trim,hi)) or not lo<=value<=hi or hi<=lo:
            raise ValueError("invalid/out-of-range FCU actuator feedback")
        if channel<=2:
            if not lo<trim<hi: raise ValueError("invalid elevon trim")
            norm=(value-trim)/(hi-trim if value>=trim else trim-lo)
            out[target]=norm*float(elevon_limit)
        else:
            out[target]=(value-lo)/(hi-lo)
    return out



def ground_contact(status):
    """Native demo _ground_safe semantics with finite near-ground fallback evidence.

    MANUAL may omit EXTENDED_SYS_STATE. Explicit airborne reports always veto
    the fallback. These FCU references validate lifecycle only, never MESA state.
    """
    if not status or status.get('mavlink_connected') is not True:
        return False
    landed=str(status.get('landed_state') or '').upper()
    if landed=='ON_GROUND':
        return True
    if landed in {'IN_AIR','TAKEOFF','LANDING'}:
        return False
    try:
        values=[float(status[k]) for k in ('groundspeed_m_s','relative_altitude_m','vertical_speed_m_s')]
        return (all(math.isfinite(v) for v in values)
                and abs(values[0])<=.8 and abs(values[1])<=.8 and abs(values[2])<=.6)
    except (KeyError,TypeError,ValueError):
        return False


def fcu_estimator_ready(status):
    """Flight-controller readiness only; never a TDDT navigation observation."""
    try:
        if 'schema' in status or 'telemetry_metadata' in status:
            from layer6.status import validate_observation_status
            if not validate_observation_status(status,required=('SYS_STATUS','EKF_STATUS_REPORT'))['ok']:return False
        ages=status["telemetry_age_s"]
        if not all(0 <= float(ages[key]) <= .5 for key in ("SYS_STATUS","EKF_STATUS_REPORT")):
            return False
        flags=int(status["ekf_status"]["flags"])
        # ArduPilot EKF_ATTITUDE, VELOCITY_HORIZ/VERT, POS_HORIZ_ABS/VERT_ABS.
        required=1|2|4|16|32
        if flags & required != required or flags & 1024:  # EKF_UNINITIALIZED
            return False
        health=status["fcu_health"]
        present=int(health["onboard_control_sensors_present"])
        healthy=int(health["onboard_control_sensors_health"])
        prearm=268435456  # MAV_SYS_STATUS_PREARM_CHECK
        return not (present & prearm) or bool(healthy & prearm)
    except (KeyError,ValueError,TypeError):
        return False
