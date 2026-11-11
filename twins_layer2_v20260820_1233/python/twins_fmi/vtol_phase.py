from __future__ import annotations

import math
from typing import Any, Mapping

PHASE_FAMILY_MAP = {
    "vertical_takeoff": "vertical_up",
    "hover": "hover",
    "transition_forward": "transition",
    "transition_back": "transition",
    "vertical_landing": "vertical_down",
    "climb": "wing_borne",
    "cruise": "wing_borne",
    "turn": "wing_borne",
    "approach": "wing_borne",
}

# Absolute mission coordinates are not epistemic operating-regime dimensions.
# Keeping them in the TEP metric made ordinary route progress look like novelty.
TEP_NOVELTY_EXCLUDED_FEATURES = {
    "state_x", "state_y", "state_h", "x", "y", "h",
    "pn", "pe", "pd", "altitude", "msl_altitude", "agl_altitude_input",
    "target_x", "target_y", "target_h", "goal_x", "goal_y", "goal_h",
    "mission_time", "path_length", "path_length_prev", "time_prev",
    "energy_prev", "energy_reference", "initial_goal_distance",
}

WING_PHASE_OVERRIDES = {
    "vertical_up": {
        "airspeed": 2.0, "groundspeed": 2.0,
        "air_velocity_x": 0.0, "air_velocity_y": 0.0, "air_velocity_h": 2.0,
        "ground_velocity_x": 0.0, "ground_velocity_y": 0.0, "ground_velocity_h": 2.0,
        "vn": 0.0, "ve": 0.0, "vd": -2.0,
        "flightpath_angle": math.pi / 2.0,
        "bank_angle": 0.0, "bank_cmd": 0.0, "sideslip": 0.0,
        "left_motor_cmd": 0.72, "right_motor_cmd": 0.72, "motor_collective": 0.72,
    },
    "hover": {
        "airspeed": 0.2, "groundspeed": 0.0,
        "air_velocity_x": 0.2, "air_velocity_y": 0.0, "air_velocity_h": 0.0,
        "ground_velocity_x": 0.0, "ground_velocity_y": 0.0, "ground_velocity_h": 0.0,
        "vn": 0.0, "ve": 0.0, "vd": 0.0,
        "flightpath_angle": 0.0,
        "bank_angle": 0.0, "bank_cmd": 0.0, "sideslip": 0.0,
        "left_motor_cmd": 0.62, "right_motor_cmd": 0.62, "motor_collective": 0.62,
    },
    "transition": {
        "airspeed": 7.0, "groundspeed": 7.0,
        "air_velocity_x": 7.0, "air_velocity_y": 0.0, "air_velocity_h": 0.0,
        "ground_velocity_x": 7.0, "ground_velocity_y": 0.0, "ground_velocity_h": 0.0,
        "vn": 7.0, "ve": 0.0, "vd": 0.0,
        "flightpath_angle": 0.0,
        "bank_angle": 0.0, "bank_cmd": 0.0, "sideslip": 0.0,
        "left_motor_cmd": 0.62, "right_motor_cmd": 0.62, "motor_collective": 0.62,
    },
    "wing_borne": {},
    "vertical_down": {
        "airspeed": 2.0, "groundspeed": 2.0,
        "air_velocity_x": 0.0, "air_velocity_y": 0.0, "air_velocity_h": -2.0,
        "ground_velocity_x": 0.0, "ground_velocity_y": 0.0, "ground_velocity_h": -2.0,
        "vn": 0.0, "ve": 0.0, "vd": 2.0,
        "flightpath_angle": -math.pi / 2.0,
        "bank_angle": 0.0, "bank_cmd": 0.0, "sideslip": 0.0,
        "left_motor_cmd": 0.55, "right_motor_cmd": 0.55, "motor_collective": 0.55,
    },
}

WING_TEP_PHASE_FAMILIES = tuple(WING_PHASE_OVERRIDES)


def phase_family(stage: Any) -> str | None:
    if stage is None:
        return None
    key = str(stage).strip().lower()
    if key in WING_PHASE_OVERRIDES:
        return key
    return PHASE_FAMILY_MAP.get(key)


def apply_wing_phase_overrides(inputs: Mapping[str, Any], family: str) -> dict[str, Any]:
    out = dict(inputs)
    for name, value in WING_PHASE_OVERRIDES.get(family, {}).items():
        if name in out:
            out[name] = value
    return out

# Minimum half-widths for phase-conditioned TEP cells. These are local operating
# regime scales, not expanded validity limits; novelty_radius remains unchanged.
WING_TEP_SCALE_FLOORS = {
    "airspeed": 3.0, "Va": 3.0, "groundspeed": 3.0,
    "air_velocity_x": 3.0, "air_velocity_y": 3.0, "air_velocity_h": 3.0,
    "ground_velocity_x": 3.0, "ground_velocity_y": 3.0, "ground_velocity_h": 3.0,
    "vn": 3.0, "ve": 3.0, "vd": 3.0,
    "wind_x": 3.0, "wind_y": 3.0, "wind_h": 3.0,
    "flightpath_angle": 0.35, "bank_angle": 0.20, "bank_cmd": 0.20, "sideslip": 0.15,
    "p_rate": 0.5, "q_rate": 0.5, "r_rate": 0.5,
    "left_motor_cmd": 0.10, "right_motor_cmd": 0.10, "motor_collective": 0.10,
    "left_elevon_rad": 0.10, "right_elevon_rad": 0.10, "elevon_collective": 0.10,
    "soc": 0.05, "air_density": 0.10, "rho": 0.10,
}
