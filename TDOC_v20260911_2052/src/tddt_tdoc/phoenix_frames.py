"""Phoenix tailsitter attitude/frame conventions for TDOC simulation paths.

Canonical navigation frame: NED. Canonical body frame: FRD. The Phoenix/JSBSim
rotor thrust axis is +body-X. Therefore a rotor-supported tailsitter has +body-X
approximately aligned with -Down_NED, i.e. +90 deg canonical pitch.

This module supplies *fallback/initialization* semantics only. A complete live
physical quaternion remains authoritative and is never overwritten here.
"""
from __future__ import annotations
import math
from typing import Any

VERTICAL_PHASES=frozenset({"on_ground","vertical_takeoff","hover","vertical_landing","touchdown"})
TRANSITION_FROM_VERTICAL=frozenset({"transition_forward"})
TRANSITION_TO_VERTICAL=frozenset({"transition_back"})
WING_BORNE_PHASES=frozenset({"climb","cruise","turn","approach"})
PHOENIX_VERTICAL_PITCH_RAD=math.pi/2.0
PHOENIX_FORWARD_PITCH_RAD=0.0


def normalize_phase(value:Any)->str:
    return str(value or "").strip().lower()


def phase_from_request(request:dict[str,Any]|None)->str:
    request=request or {}; ctx=request.get("context") if isinstance(request,dict) else {}
    ctx=ctx if isinstance(ctx,dict) else {}
    rc=ctx.get("rtmo_run_context") or ctx.get("tdoc_run_context") or {}
    if not isinstance(rc,dict): rc={}
    return normalize_phase(rc.get("phase_id") or request.get("stage") or ctx.get("flight_phase"))


def euler_quaternion(roll:float,pitch:float,yaw:float)->tuple[float,float,float,float]:
    cr,sr=math.cos(roll/2.0),math.sin(roll/2.0)
    cp,sp=math.cos(pitch/2.0),math.sin(pitch/2.0)
    cy,sy=math.cos(yaw/2.0),math.sin(yaw/2.0)
    return (cr*cp*cy+sr*sp*sy,
            sr*cp*cy-cr*sp*sy,
            cr*sp*cy+sr*cp*sy,
            cr*cp*sy-sr*sp*cy)


def fallback_attitude(phase:Any, *, yaw_rad:float=0.0, kinematic_pitch_rad:float=0.0)->dict[str,Any]:
    """Return a canonical FRD->NED attitude for missing/non-authoritative attitude.

    Vertical/passive tailsitter phases use +90 deg pitch. Transition and wing-borne
    boundaries use the caller's realized kinematic pitch; a hover-entry quaternion
    must not remain frozen after forward-transition departure.
    """
    p=normalize_phase(phase)
    if p in VERTICAL_PHASES:
        pitch=PHOENIX_VERTICAL_PITCH_RAD
        basis="phoenix_tailsitter_vertical_body_x_to_negative_down"
    else:
        pitch=float(kinematic_pitch_rad)
        basis="kinematic_pitch_fallback"
    q=euler_quaternion(0.0,pitch,float(yaw_rad))
    return {"phase":p,"roll_rad":0.0,"pitch_rad":pitch,"yaw_rad":float(yaw_rad),
            "qw":q[0],"qx":q[1],"qy":q[2],"qz":q[3],
            "basis":basis,"body_frame":"FRD","navigation_frame":"NED",
            "rotor_thrust_axis":"+body-X","source_frame_note":"Phoenix SDF forward=[0,0,1], upward=[1,0,0] mapped once to canonical FRD"}


def body_x_ned(qw:float,qx:float,qy:float,qz:float)->tuple[float,float,float]:
    """Body +X expressed in NED for the quaternion convention used by WingMPET."""
    n=math.sqrt(qw*qw+qx*qx+qy*qy+qz*qz)
    if n<1e-12: return (1.0,0.0,0.0)
    qw,qx,qy,qz=(qw/n,qx/n,qy/n,qz/n)
    return (1-2*(qy*qy+qz*qz), 2*(qx*qy+qw*qz), 2*(qx*qz-qw*qy))


def vertical_alignment_ok(qw:float,qx:float,qy:float,qz:float,tol_deg:float=8.0)->bool:
    bx=body_x_ned(qw,qx,qy,qz)
    # Desired vertical tailsitter +X is NED [0,0,-1].
    dot=max(-1.0,min(1.0,-bx[2]))
    return math.degrees(math.acos(dot)) <= float(tol_deg)


def authoritative_or_fallback_attitude(state:dict[str,Any], phase:Any, *, yaw_rad:float=0.0, kinematic_pitch_rad:float=0.0)->dict[str,Any]:
    """Preserve a supplied FRD-to-NED quaternion; a partial/invalid one is an error.

    This is not an estimator and never rotates a realized state to satisfy a phase.
    Only a state with no attitude fields may use the documented initialization.
    """
    keys=("qw","qx","qy","qz")
    present=[k in state for k in keys]
    if not any(present):
        return fallback_attitude(phase,yaw_rad=yaw_rad,kinematic_pitch_rad=kinematic_pitch_rad)
    if not all(present):
        raise ValueError("partial_authoritative_quaternion")
    q=[float(state[k]) for k in keys]
    norm=math.sqrt(sum(v*v for v in q))
    if not all(math.isfinite(v) for v in q) or not 0.95 <= norm <= 1.05:
        raise ValueError("invalid_authoritative_quaternion")
    return {**dict(zip(keys,(v/norm for v in q))),"basis":"authoritative_physical_state_quaternion",
            "phase":normalize_phase(phase),"body_frame":"FRD","navigation_frame":"NED"}
