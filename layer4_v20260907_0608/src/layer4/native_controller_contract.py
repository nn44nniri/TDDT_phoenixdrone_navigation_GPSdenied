"""Stage-4 native pilot/controller FF eligibility contract.

This module identifies requests whose physical meaning requires ArduPilot's native
pilot->controller->mixer->JSBSim path. The current Layer-4 FF adapter only replays
Layer-3 FMUs with hypothetical actions: it cannot certify that path. Never infer
controller state, spool or RPM from PWM, and never promote an offline certificate.
"""
from __future__ import annotations
from dataclasses import dataclass
import math
from typing import Any
from .native_provider_contract import CONTRACT_SCHEMA, REVISION as PROVIDER_REVISION

SCHEMA = "TDDT-L4-native-controller-readiness-v1"
ONLINE_MARKERS = frozenset((
    "online_episode_id", "online_cycle_in_episode", "rtmo_run_context",
    "rtmo_data_authority", "online_state_ingress", "rtmo_pre_release_verify",
    "online_execution_state", "online_physical_state", "execution_transaction_id",
    "layer6_transaction_id", "pilot_command", "pilot_command_domain",
    "pilot_execution_binding", "native_controller_evidence", "native_pilot_control",
))
NATIVE_MODES = frozenset((
    "qstabilize", "qhover", "qloiter", "qland", "qrtl", "qautotune",
    "fbwa", "fbwb", "cruise_native", "guided_native", "auto_native",
))

MODEL_PREDICTION_SCOPE = "TRANSDOMAIN_MODEL_NATIVE_EXECUTION_DIAGNOSTIC"


@dataclass(frozen=True)
class ModelPredictionScope:
    """Composition-time capability for model prediction, never FCU certification.

    TDOC installs this Python object only for the explicitly selected SITL
    prototype. Snapshot fields, YAML booleans and native readiness flags cannot
    install it. Every existing FF feasibility and identity gate still applies.
    """


def model_prediction_scope(config: Any) -> dict:
    if type(getattr(config, "model_prediction_scope", None)) is not ModelPredictionScope:
        return {}
    return {"verification_scope": MODEL_PREDICTION_SCOPE,
            "model_prediction_only": True, "native_controller_certified": False,
            "flight_authorized": False,
            "prediction_role": "trans-domain model prediction; measured Native execution compared separately in Layer7"}


def model_ff_allowed(snapshot: Any, config: Any) -> bool:
    return not needs_native_controller(snapshot) or bool(model_prediction_scope(config))


def _context(snapshot: Any) -> dict:
    return dict(getattr(snapshot, "context", {}) or {})


def needs_native_controller(snapshot: Any) -> bool:
    c = _context(snapshot)
    p = dict(getattr(snapshot, "provenance", {}) or {})
    explicit = any(key in c or key in p for key in ONLINE_MARKERS)
    domain = str(c.get("command_domain") or c.get("control_domain") or "").lower().strip()
    mode = str(c.get("flight_mode") or c.get("flight_phase") or "").lower().strip()
    return bool(explicit or domain in {"pilot", "pilot_reference", "native_pilot", "layer6_pilot"}
                or mode in NATIVE_MODES)


def readiness(snapshot: Any, candidate: Any = None, config: Any = None) -> dict:
    c = _context(snapshot)
    need = needs_native_controller(snapshot)
    result = {
        "schema": SCHEMA,
        "native_pilot_execution_requested": need,
        "native_controller_certified": False,
        "flight_authorized": False,
        "provider_kind": "UNAVAILABLE_NOT_IMPLEMENTED_IN_LAYER4",
        "provider_contract_schema": CONTRACT_SCHEMA,
        "provider_contract_revision": PROVIDER_REVISION,
        "provider_contract_is_qualification": False,
        "pilot_to_actuator_replay_executed": False,
        "reason": "native_pilot_controller_prediction_unavailable" if need else "model_only_fmu_scope",
        "required_evidence": [
            "same_candidate_and_snapshot_hash", "same_mode_and_controller_parameter_hash",
            "observed_controller_and_spool_initial_state", "actual_post_mixer_actuator_trace",
            "pilot_command_domain_and_duration", "plant_time_alignment_and_knowledge_hash",
            "prediction_observation_residual_and_uncertainty", "measured_or_native_speed_if_omega_claimed",
        ] if need else [],
    }
    if need:
        # Diagnostics are not authority. No caller-supplied flags can manufacture
        # the missing native predictor/replay implementation.
        h = None
        dt = None
        try:
            if candidate is not None:
                actions = getattr(candidate, "actions", [])
                h = len(actions)
                if not actions or any(not isinstance(row,(list,tuple)) or not row or
                    any(not math.isfinite(float(x)) for x in row) for row in actions):
                    result["reason"] = "invalid_pilot_action_horizon"
            if config is not None:
                dt = float(config.dt)
                if not math.isfinite(dt) or dt <= 0:
                    result["reason"] = "invalid_prediction_step_s"
            commit = c.get("execution_segment_s")
            if commit is not None and h is not None and dt is not None:
                commit = float(commit)
                if not math.isfinite(commit) or commit <= 0 or commit > h * dt + 1e-12:
                    result["reason"] = "invalid_commit_forecast_horizon"
                result["forecast_horizon_s"] = h * dt
                result["execution_segment_s"] = commit
        except (ValueError, TypeError, OverflowError):
            result["reason"] = "invalid_pilot_timing_or_action"
    return result
