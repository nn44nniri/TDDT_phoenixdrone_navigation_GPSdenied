"""Stage 5 authority boundary: model-only FF never authorizes native pilot control.

Only the ArduPilot-controlled path can establish pilot->mixer->JSBSim evidence.
Layer 5 has no native predictor installed in this release. The interface is
read-only, deterministic, and NOT an authorization provider. A caller-supplied
boolean, certificate ID, mode, or PWM-derived RPM is never sufficient.
"""
from __future__ import annotations
from collections.abc import Mapping
from typing import Any

SCHEMA = "TDDT-L5-native-pilot-FF-boundary-v1"
REQUIRED = (
    "candidate_and_snapshot_identity", "native_controller_model_and_parameters",
    "flight_mode_and_spool_initial_state", "pilot_domain_command_and_commit_duration",
    "fcu_post_mixer_actuator_observation", "jsbsim_plant_time_alignment",
    "prediction_observation_residual_with_uncertainty", "independent_motor_speed_if_rpm_claimed",
)


def audit(layer4: Mapping[str, Any], *, learning_mode: str) -> dict[str, Any]:
    mode = str(learning_mode).upper()
    ctx = dict(layer4.get("context") or {}) if isinstance(layer4.get("context"), Mapping) else {}
    prov = dict(layer4.get("provenance") or {}) if isinstance(layer4.get("provenance"), Mapping) else {}
    active = (mode == "ONLINE_JSBSIM"
              or any(key in ctx or key in prov for key in (
                  "online_episode_id", "rtmo_run_context", "pilot_execution_binding",
                  "native_controller_evidence", "execution_transaction_id"))
              or str(ctx.get("command_domain") or "").lower() in (
                  "native_pilot", "pilot_reference_native", "ardupilot_pilot")
              or str(ctx.get("flight_mode") or "").upper() in (
                  "QHOVER", "QSTABILIZE", "QLOITER", "QLAND", "QRTL", "FBWA", "FBWB", "GUIDED_NATIVE"))
    options = layer4.get("candidates") or []
    verified = layer4.get("verified_candidates") or []
    # Do not collapse empty verification to a vacuously true conformance gate.
    selected_ids = [str(x.get("candidate_id")) for x in verified
                    if isinstance(x, Mapping) and x.get("candidate_id")]
    source_scopes = sorted(set(str((v.get("verification") or {}).get("verification_scope") or "UNSPECIFIED")
                                for v in options if isinstance(v, Mapping)))
    return {
        "schema": SCHEMA,
        "online_native_pilot_required": active,
        "current_native_predictor_implemented": False,
        "native_execution_mapping_certified": False,
        "pilot_to_actuator_replay_executed": False,
        "native_controller_certified": False,
        "flight_authorized": False,
        "full_fidelity_authority_for_online": False,
        "model_only_prior_allowed_for_offline": mode == "OFFLINE_SE",
        "raw_verified_candidate_count": len(selected_ids),
        "raw_verified_candidate_ids": selected_ids,
        "layer4_verification_scopes": source_scopes,
        "snapshot_hash_declared": bool(layer4.get("snapshot_hash")),
        "required_evidence": list(REQUIRED) if active else [],
        "status": ("BLOCKED_NATIVE_PILOT_CONTROLLER" if active else "MODEL_ONLY_OFFLINE_SCOPE"),
        "reason": ("native_pilot_controller_prediction_unavailable" if active
                   else "offline_model_only_no_execution_authority"),
        "contract_version": "v1",
    }
