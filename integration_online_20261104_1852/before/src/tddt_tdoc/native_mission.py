"""Bind current MPC decisions to the explicitly selected closed-loop SITL profile.

This checks the execution contract, not a model of ArduPilot's controller. The
Layer2/Layer4 prediction remains model guidance and is compared with measured
execution in Layer7. No command, FINAL gate, ARM intent or prediction is invented
here. The normal Layer5 MPC, PSF, RTA and governance path must already have run.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import time

from .native_control_binding import PHASE_MODES, fcu_estimator_ready, ground_contact
from .utils import sha256_obj


PROFILE = "closed_loop_sitl"
SCOPE = "SITL_CLOSED_LOOP"
SCHEMA = "TDOC-native-mission-binding-v1"
IDENTITY_KEYS = ("binding_id", "binding_hash", "context_sha256", "model_id", "handoff_hash",
                 "forcing_file_sha256", "effective_climate_epoch", "plant_epoch_s")
ALIASES = (("u_cmd", "command"), ("A_target", "actuator_targets"),
           ("C_exec", "execution_constraints"), ("S_safe", "safety_policy"),
           ("tau_apply", "apply_time"), ("tau_deadline", "deadline_time"),
           ("rho_decision", "decision_confidence"), ("pi_decision", "provenance"),
           ("nu", "version"))


def _require(ok, reason):
    if not ok:
        raise ValueError(reason)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _validate_status(status):
    from layer6.status import CORE_CHANNELS, validate_observation_status
    result = validate_observation_status(status, required=(*CORE_CHANNELS, "SERVO_OUTPUT_RAW", "SYS_STATUS", "EKF_STATUS_REPORT"))
    _require(result["ok"], "fresh Native FCU observations required: " + result["reason"])
    _require(fcu_estimator_ready(status), "FCU estimator/pre-arm health is not ready")


class NativeMissionBinding:
    """Process-local registration and independent recheck before Layer6 submit."""

    def __init__(self, config, *, status_source, prior_validator, environment_validator=None):
        self.config = config
        self.status_source = status_source
        self.prior_validator = prior_validator
        self.environment_validator = environment_validator
        self._decisions = {}

    def _current(self, status=None):
        _require(self.config.get("layers.layer6.online.execution_profile") == PROFILE,
                 "explicit closed_loop_sitl execution profile required")
        _require(self.config.get("layers.layer6.online.transport") == "Layer6Client_GUI_MAVLink",
                 "Native GUI/MAVLink transport required")
        prior = self.prior_validator()
        _require(isinstance(prior, dict) and prior.get("ok") is True,
                 "accepted Offline model prior required")
        handoff = prior.get("handoff_hash")
        _require(isinstance(handoff, str) and len(handoff) == 64, "verified handoff identity required")
        status = self.status_source() if status is None else status
        _validate_status(status)
        _require(type(status.get("armed")) is bool and status.get("mode") not in (None, "UNKNOWN", "INITIALISING"),
                 "current FCU ARM/MODE state required")
        env = status.get("environment_contract") or {}
        if self.environment_validator is not None:
            _require(self.environment_validator(status) is True, "Native forcing/environment binding rejected")
        else:
            from layer6.binding import context_guard, consumption_identity, validate_wind_application, forcing_time
            from layer6.timebase import validate_clock_mapping
            path = self.config.path_value("TDDT_LAYER6_ROOT", required=True) / "runtime/tdoc_environment_binding.json"
            binding = context_guard(path).check()
            _require(all(env.get(k) == v for k, v in consumption_identity(binding).items()),
                     "Native forcing/environment identity mismatch")
            validate_clock_mapping(binding, env)
            validate_wind_application(env)
            _require(forcing_time(binding, env["plant_time_s"])[0] == env["simulation_time_dataset"],
                     "Native forcing time mismatch")
        _require(env.get("ok") is True and all(env.get(k) is not None for k in IDENTITY_KEYS),
                 "complete live environment identity required")
        _require(env["handoff_hash"] == handoff, "Native/Offline handoff identity mismatch")
        # Validate the acquired observation before copying it. Its provider may
        # retain a process-local witness for this exact snapshot's input checks;
        # copying first would force repeated file hashing after acquisition and
        # consume the live telemetry freshness budget.
        status = deepcopy(status)
        env = status["environment_contract"]
        native_id = (status.get("status_quality") or {}).get("native_session_id")
        _require(bool(native_id), "Native process session identity required")
        _require(_number(status.get("plant_time_s")) and status["plant_time_s"] >= 0,
                 "current FCU plant clock required")
        identity = {k: env[k] for k in IDENTITY_KEYS}
        identity.update(session_id=status["session_id"], monotonic_domain_id=status["monotonic_domain_id"],
                        native_session_id=native_id, target_system=status["target_system"],
                        target_component=status["target_component"])
        return status, identity

    def operational_readiness(self):
        try:
            _, identity = self._current()
            return {"ok": True, "scope": SCOPE, "execution_profile": PROFILE,
                    "identity": identity, "flight_certified": False, "native_controller_certified": False,
                    "ARM_authorized": False, "per_decision_verification_required": True}
        except Exception as exc:
            return {"ok": False, "scope": SCOPE, "reason": str(exc), "ARM_authorized": False}

    @staticmethod
    def _intent(package, request, status):
        phase = str(request.get("stage") or "").lower()
        expected = {"on_ground": "QHOVER", "touchdown": "QHOVER", **PHASE_MODES}
        _require(phase in expected, "unsupported current mission phase")
        intent = package.get("execution_intent")
        _require(isinstance(intent, dict) and intent.get("requested_pilot_mode") == expected[phase],
                 "current Layer5 phase/pilot mode intent mismatch")
        discrete = intent.get("discrete")
        _require(isinstance(discrete, list) and len(discrete) == len(set(discrete))
                 and all(x in {"ARM", "DISARM", "THROTTLE_ZERO", "NEUTRAL_ATTITUDE"} for x in discrete),
                 "explicit valid Layer5 discrete intent required")
        _require(not ("ARM" in discrete and "DISARM" in discrete), "conflicting ARM/DISARM intent")
        command = package["u_cmd"]
        if "ARM" in discrete:
            _require(phase in ("on_ground", "vertical_takeoff") and status["armed"] is False
                     and ground_contact(status) and status.get("throttle") == 0.0,
                     "ARM requires current grounded, disarmed, zero-throttle FCU state")
        elif phase not in ("on_ground", "touchdown"):
            _require(status["armed"] is True, "airborne mission command requires armed FCU")
        if "DISARM" in discrete:
            _require(phase == "touchdown" and ground_contact(status) and command[2] == 0.0,
                     "DISARM requires current grounded touchdown decision")
        if phase in ("on_ground", "touchdown"):
            _require(ground_contact(status) and command[2] == 0.0, "ground boundary requires zero throttle and ground contact")
        if "THROTTLE_ZERO" in discrete:
            _require(command[2] == 0.0, "THROTTLE_ZERO conflicts with MPC command")
        if "NEUTRAL_ATTITUDE" in discrete:
            _require(all(command[i] == 0.0 for i in (0, 1, 3)), "NEUTRAL_ATTITUDE conflicts with MPC command")
        return phase

    @staticmethod
    def _decision(result, layer4):
        _require(result.get("ok") is True and result.get("learning_mode") == "ONLINE_JSBSIM",
                 "current Online Layer5 decision required")
        gates = result.get("gates") or {}
        _require(gates.get("FINAL") is True, "current Layer5 FINAL required")
        cid = (result.get("decision") or {}).get("candidate_id")
        _require(bool(cid) and bool((result.get("decision") or {}).get("decision_id")), "current Layer5 decision identity required")
        for name in ("tdoc_boundary", "lifecycle", "budget"):
            _require((gates.get(name) or {}).get("ok") is True, "Layer5 gate rejected: " + name)
        for name in ("integrity", "admissibility", "policy_gate", "phoenix_binding"):
            _require(((gates.get(name) or {}).get(cid) or {}).get("ok") is True, "selected Layer5 gate rejected: " + name)
        _require((gates.get("psf") or {}).get("gate") is True
                 and (gates.get("rta") or {}).get("source") == "advanced"
                 and (gates.get("governance") or {}).get("gate") is True,
                 "current Layer5 PSF/RTA/governance required")
        _require((result.get("provenance") or {}).get("layer4_input_digest") == sha256_obj(layer4),
                 "Layer5 result does not match current Layer4 input")
        _require((result.get("provenance") or {}).get("only_first_mpc_move") is True,
                 "receding-horizon first move evidence required")
        sh, sv = layer4.get("snapshot_hash"), layer4.get("snapshot_version")
        _require(bool(sh) and bool(sv) and result.get("snapshot_hash") == sh
                 and result.get("snapshot_version") == sv, "Layer4/Layer5 snapshot identity mismatch")
        candidates = [c for c in layer4.get("candidates", []) if c.get("candidate_id") == cid]
        verified = [c for c in layer4.get("verified_candidates", []) if c.get("candidate_id") == cid]
        _require(len(candidates) == 1 and len(verified) == 1, "selected verified Layer4 candidate missing or ambiguous")
        candidate = candidates[0]
        if verified[0] == {"candidate_id": cid}:
            # RTMO's production execution projection retains a compact verified
            # ID index. Admission still uses the full candidate and its FF proof;
            # the projection digest must bind both before accepting that index.
            projected = deepcopy(layer4)
            provenance = projected.get("provenance") or {}
            proof = provenance.pop("rtmo_execution_projection", None)
            provenance.pop("rtmo_l4_artifact_reuse", None)  # Added only by acquire().
            encoded = json.dumps(projected, sort_keys=True, separators=(",", ":"),
                                 default=str, allow_nan=False).encode("utf-8")
            _require(isinstance(proof, dict) and proof.get("schema") == "TDOC-RTMO-L4-execution-projection-v1"
                     and proof.get("sha256") == hashlib.sha256(encoded).hexdigest(),
                     "Layer4 execution projection integrity mismatch")
        else:
            _require(candidate == verified[0], "selected Layer4 verified candidate differs from candidate registry")
        _require(candidate.get("snapshot_hash", sh) == sh and candidate.get("snapshot_version", sv) == sv,
                 "selected candidate snapshot mismatch")
        _require(candidate.get("feasible") is True and
                 (candidate.get("candidate_verified") is True or candidate.get("verified") is True),
                 "selected Layer4 candidate is not feasible/verified")
        from layer5.contract import Layer4ContractAdapter
        _require(Layer4ContractAdapter._full_fidelity_authority(candidate.get("verification") or {}),
                 "current authoritative Layer4 FMU verification required")
        _require(not (candidate.get("provenance") or {}).get("test_fixture")
                 and not (layer4.get("provenance") or {}).get("test_fixture"), "fixture candidate cannot authorize Native execution")
        solver = ((gates.get("solver") or {}).get(cid) or {})
        _require(solver.get("ok") is True and (solver.get("vp") or {}).get("ok") is True,
                 "verification-preserving MPC solution required")
        moves = solver.get("corrected_actions") or []
        _require(bool(moves), "MPC first move unavailable")
        command = moves[0]
        _require(isinstance(command, list) and len(command) == 4
                 and all(_number(v) and lo <= v <= hi for v, lo, hi in zip(command, (-1., -1., 0., -1.), (1., 1., 1., 1.))),
                 "MPC command outside native pilot domain")
        package = result.get("layer6_package") or {}
        _require(package.get("u_cmd") == command and (result.get("control_action") or {}).get("first_move") == command,
                 "Layer5 package/control differs from MPC first move")
        return cid, candidate

    def bind_result(self, result, *, layer4_result, request, status=None):
        cid, candidate = self._decision(result, layer4_result)
        status, identity = self._current(status)
        value = deepcopy(result)
        package = value["layer6_package"]
        phase = self._intent(package, request, status)
        tx = request.get("transaction_id")
        _require(isinstance(tx, str) and bool(tx), "current TDOC transaction required")
        for key, expected in (("transaction_id", tx), ("candidate_id", cid),
                              ("snapshot_hash", result["snapshot_hash"]), ("snapshot_version", result["snapshot_version"])):
            _require(package.get(key, expected) == expected, "Layer5 package identity mismatch: " + key)
            package[key] = expected
        segment = float(self.config.get("tdoc.rtmo.l4_artifacts.commit_segment_s", .1))
        rate = float(self.config.get("layers.layer6.online.execution_rate_hz", 30.))
        _require(math.isfinite(segment) and 0 < segment <= 1 and math.isfinite(rate) and rate > 0,
                 "bounded Native execution policy required")
        from layer6.segments import DEFAULT_CLOSE_GUARD_S
        policy = {"schema": "Layer6-first-move-segment-v1", "max_duration_s": segment,
                  "close_guard_s": min(DEFAULT_CLOSE_GUARD_S, segment / 2)}
        _require(package.get("execution_segment", policy) == policy, "Layer5 execution segment differs from transport policy")
        _require(package.get("execution_rate_hz", rate) == rate, "Layer5 cadence differs from transport policy")
        package.update(execution_segment=policy, execution_rate_hz=rate, phase=phase)
        for canonical, alias in ALIASES:
            _require(canonical in package and (alias not in package or package[alias] == package[canonical]),
                     "missing/conflicting Layer5 contract alias: " + canonical)
            package[alias] = deepcopy(package[canonical])
        package["formal_contract"] = {k: deepcopy(package[k]) for k, _ in ALIASES}
        _require(package["C_exec"].get("direct_pwm") is False
                 and all(package["S_safe"].get(k) is True for k in ("psf", "rta")), "Layer5 execution safety/domain rejected")
        _require(all(_number(package[k]) for k in ("tau_apply", "tau_deadline", "rho_decision"))
                 and package["tau_apply"] <= time.time() < package["tau_deadline"] and 0 <= package["rho_decision"] <= 1,
                 "Layer5 decision expired or invalid")
        evidence = {"schema": SCHEMA, "scope": SCOPE, "execution_profile": PROFILE,
                    "transaction_id": tx, "candidate_id": cid, "snapshot_hash": result["snapshot_hash"],
                    "snapshot_version": result["snapshot_version"], "phase": phase,
                    "layer4_input_sha256": sha256_obj(layer4_result), "candidate_sha256": sha256_obj(candidate),
                    "package_sha256": sha256_obj(package), "identity": identity,
                    "source_plant_time_s": status["plant_time_s"], "flight_certified": False,
                    "native_controller_certified": False, "prediction_role": "Layer2/Layer4 model guidance; compare measured execution in Layer7"}
        value["native_mission_binding"] = evidence
        pin = sha256_obj(value)
        old = self._decisions.get(tx)
        _require(old is None or old["result_sha256"] == pin, "transaction already bound to a different Native decision")
        self._decisions[tx] = {"result_sha256": pin, "source": deepcopy(layer4_result),
                               "request": deepcopy(request), "identity": identity, "source_plant_time_s": status["plant_time_s"]}
        while len(self._decisions) > 64:
            self._decisions.pop(next(iter(self._decisions)))
        return value

    def verify_result(self, result, *, transaction_id):
        try:
            row = self._decisions.get(transaction_id)
            _require(row is not None and sha256_obj(result) == row["result_sha256"],
                     "unregistered or mutated current Native decision")
            self._decision(result, row["source"])
            status, identity = self._current()
            _require(identity == row["identity"], "Native session/model/forcing identity changed after decision")
            _require(status["plant_time_s"] >= row["source_plant_time_s"], "Native FCU plant time rewound")
            package = result["layer6_package"]
            _require(package["transaction_id"] == transaction_id, "Native transaction mismatch")
            self._intent(package, row["request"], status)
            _require(package["tau_apply"] <= time.time() < package["tau_deadline"], "Native decision expired")
            return {"ok": True, "execution_contract_verified": True, "scope": SCOPE,
                    "package_sha256": sha256_obj(package), "flight_certified": False,
                    "native_controller_certified": False, "execution_confirmed": False,
                    "prediction_role": "model guidance; Native execution residual remains measured"}
        except Exception as exc:
            return {"ok": False, "execution_contract_verified": False, "scope": SCOPE,
                    "reason": str(exc), "flight_certified": False, "execution_confirmed": False}
