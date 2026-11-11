from __future__ import annotations

"""Reverse-path reconstruction and Layer-7 evidence builders.

The GUI owns the MAVLink connection and keeps a bounded asynchronous telemetry
buffer.  This module converts the GUI's reconstructed status snapshot into the
canonical current-state / execution-evidence representation required by the
Layer-6 formalism.  Raw history is intentionally not forwarded to Layer-7.
"""

import time
from typing import Any, Mapping


def _dict(v: Any) -> dict:
    return dict(v) if isinstance(v, Mapping) else {}


def _vector_from_packet(packet: Mapping[str, Any]) -> dict:
    p = packet.get("layer6_package", packet)
    u = p.get("u_cmd") or p.get("first_move") or [None, None, None, None]
    if len(u) != 4:
        u = [None, None, None, None]
    return {
        "aileron": u[0], "elevator": u[1], "throttle": u[2], "rudder": u[3]
    }


def current_state_from_gui(status: Mapping[str, Any] | None, *, transaction_id: str | None = None) -> dict:
    st = _dict(status)
    att = _dict(st.get("attitude"))
    battery = _dict(st.get("battery"))
    return {
        "timestamp": time.time(),
        "state_id": f"l6-{transaction_id or 'snapshot'}",
        "attitude": {
            "roll_rad": att.get("roll"),
            "pitch_rad": att.get("pitch"),
            "yaw_rad": att.get("yaw"),
        },
        "body_rates": {
            "roll_rate_rad_s": att.get("roll_rate"),
            "pitch_rate_rad_s": att.get("pitch_rate"),
            "yaw_rate_rad_s": att.get("yaw_rate"),
        },
        "speed": {
            "airspeed_m_s": st.get("airspeed_m_s"),
            "groundspeed_m_s": st.get("groundspeed_m_s"),
            "vertical_speed_m_s": st.get("vertical_speed_m_s"),
            "heading_deg": st.get("heading_deg"),
        },
        "velocity": {key: st.get(key) for key in
                     ("velocity_n_m_s", "velocity_e_m_s", "velocity_d_m_s")},
        "plant_time_s": st.get("plant_time_s"),
        "session_id": st.get("session_id"),
        "position": {
            "latitude_deg": st.get("latitude_deg"),
            "longitude_deg": st.get("longitude_deg"),
            "altitude_msl_m": st.get("altitude_msl_m"),
            "relative_altitude_m": st.get("relative_altitude_m"),
        },
        "battery": {
            "voltage_v": battery.get("voltage_v"),
            "current_a": battery.get("current_a"),
            "remaining_percent": battery.get("remaining_percent"),
        },
        "mode": st.get("mode"),
        "armed": st.get("armed"),
        "landed_state": st.get("landed_state"),
        "estimator_health": None,
        "sensor_health": None,
        "state_quality": {
            "mavlink_connected": bool(st.get("mavlink_connected")),
            "telemetry_buffer_samples": int(st.get("telemetry_buffer_samples") or 0),
            "telemetry_latest_types": list(st.get("telemetry_latest_types") or []),
        },
        "active_decision_id": transaction_id,
        "active_pilot_command": _dict(st.get("applied_pilot_command")),
    }


def build_execution_evidence(
    layer5_packet: Mapping[str, Any],
    status: Mapping[str, Any] | None,
    *,
    transaction_id: str | None = None,
    intake_ack: Mapping[str, Any] | None = None,
) -> dict:
    st = _dict(status)
    if 'schema' in st or 'telemetry_metadata' in st:
        from .status import CORE_CHANNELS,validate_observation_status
        check=validate_observation_status(st,required=(*CORE_CHANNELS,'SERVO_OUTPUT_RAW'))
        if not check['ok']:raise ValueError('invalid native ExecutionEvidence observations: '+check['reason'])
    tx = transaction_id or str(
        layer5_packet.get("transaction_id")
        or _dict(layer5_packet.get("layer6_package")).get("transaction_id")
        or "unknown"
    )
    current = current_state_from_gui(st, transaction_id=tx)
    requested = _vector_from_packet(layer5_packet)
    applied = _dict(st.get("applied_pilot_command"))
    actuator_observed = _dict(st.get("servo_output_raw"))

    y_real = {
        "airspeed_m_s": current["speed"]["airspeed_m_s"],
        "altitude_msl_m": current["position"]["altitude_msl_m"],
        "roll_rad": current["attitude"]["roll_rad"],
        "pitch_rad": current["attitude"]["pitch_rad"],
        "yaw_rad": current["attitude"]["yaw_rad"],
    }
    x_obs = {
        **current["body_rates"],
        **current["velocity"],
        "groundspeed_m_s": current["speed"]["groundspeed_m_s"],
        "vertical_speed_m_s": current["speed"]["vertical_speed_m_s"],
        "heading_deg": current["speed"]["heading_deg"],
        "latitude_deg": current["position"]["latitude_deg"],
        "longitude_deg": current["position"]["longitude_deg"],
        "relative_altitude_m": current["position"]["relative_altitude_m"],
        "mode": current["mode"],
        "armed": current["armed"],
        "landed_state": current["landed_state"],
    }
    return {
        "y_real": y_real,
        "u_cmd": requested,
        "u_applied": applied,
        "u_cmd_pilot": requested,
        "u_applied_pilot": applied,
        "x_obs": x_obs,
        "d_real": {
            "wind": None,
            "availability": "not_provided_by_current_MAVLink_feedback",
        },
        "actuator_observed": actuator_observed,
        "actuator_status": {
            "execution_path": "Layer6Client/intake -> DigitalPilotExecutor -> GUI -> MAVLink",
            "mavlink_connected": bool(st.get("mavlink_connected")),
            "measurement_semantics": "SERVO_OUTPUT_RAW is flight-controller output telemetry, not direct physical actuator measurement",
            "status": "AVAILABLE" if st.get("mavlink_connected") else "UNAVAILABLE",
        },
        "timestamp": time.time(),
        "planned_timestamp": _dict(layer5_packet.get("layer6_package", layer5_packet)).get("tau_apply"),
        "provenance": {
            "source": "Layer6",
            "flight_controller": "ArduPilot SITL",
            "simulator": "JSBSim",
            "transaction_id": tx,
            "session_id": st.get("session_id"),
            "native_session_id": _dict(st.get("status_quality")).get("native_session_id"),
            "plant_time_s": st.get("plant_time_s"),
            "candidate_id": _dict(layer5_packet.get("layer6_package", layer5_packet)).get("candidate_id"),
            "snapshot_hash": _dict(layer5_packet.get("layer6_package", layer5_packet)).get("snapshot_hash"),
            "command_correlation": _dict(st.get("command_correlation")),
            "intake_ack": _dict(intake_ack),
        },
        "version": "layer6-2026.10.07",
        "evidence_type": "simulation",
        "correlation_ids": {"transaction_id": tx},
        "output_semantics": {
            "airspeed_m_s": {"quantity": "airspeed", "unit": "m/s", "frame": "BODY"},
            "altitude_msl_m": {"quantity": "altitude", "unit": "m", "frame": "MSL"},
            "attitude": {"unit": "rad", "frame": "vehicle attitude"},
            "pilot_domain": {"aileron": "[-1,1]", "elevator": "[-1,1]", "throttle": "[0,1]", "rudder": "[-1,1]"},
        },
        "current_state": current,
    }


def build_layer7_input(
    layer5_packet: Mapping[str, Any],
    status: Mapping[str, Any] | None,
    *,
    transaction_id: str | None = None,
    intake_ack: Mapping[str, Any] | None = None,
    prediction_evidence: Any = None,
    network_snapshot: Any = None,
) -> dict:
    tx = transaction_id or str(
        layer5_packet.get("transaction_id")
        or _dict(layer5_packet.get("layer6_package")).get("transaction_id")
        or "unknown"
    )
    return {
        "transaction_id": tx,
        "version": "layer7-input-v1",
        "ExecutionEvidence": build_execution_evidence(
            layer5_packet, status, transaction_id=tx, intake_ack=intake_ack
        ),
        # These originate in L4/L3 and are not invented by this Layer-6 harness.
        "PredictionEvidence": prediction_evidence,
        "NetworkSnapshot": network_snapshot,
        "DecisionTrace": layer5_packet,
    }
