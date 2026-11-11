"""Stage-2 explicit actuator-domain WingMPET replay.

This intentionally does NOT implement ArduPilot QHOVER/FBWA control loops and
never issues an Online model or flight certificate.  It requires actuator
outputs *after* the flight controller and an independently sourced motor state.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
from typing import Any, Mapping

from .formula_fmi import FormulaFMU

# Exactly op27 inputs, no implicit pilot-command-to-actuator conversion.
FIELDS = (
    "dt", "pn", "pe", "pd", "vn", "ve", "vd", "qw", "qx", "qy", "qz",
    "p_rate", "q_rate", "r_rate", "omega_left", "omega_right", "soc",
    "left_motor_cmd", "right_motor_cmd", "left_elevon_rad", "right_elevon_rad",
    "wind_n", "wind_e", "wind_d", "air_density",
)
MAX_OMEGA_RAD_S = 800.0  # Checked against compiled Phoenix profile in tests.
MAX_ELEVON_RAD = 0.53
VALID_MOTOR_SOURCE = {"jsbsim_property", "motor_rpm_telemetry", "dynamic_motor_estimator"}
VALID_ACTUATOR_SOURCE = {"fcu_servo_output_with_verified_mapping", "jsbsim_actuator_property"}


class NativeReplayRejected(ValueError):
    """Invalid or untrusted input. Always fail closed, never fill missing values."""


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NativeReplayRejected(f"{name}: non-empty identifier required")
    return value.strip()


def _finite(value: Any, name: str) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise NativeReplayRejected(f"{name}: numerical value required") from exc
    if not math.isfinite(v):
        raise NativeReplayRejected(f"{name}: nonfinite value")
    return v


@dataclass(frozen=True)
class ActuatorReplaySample:
    values: Mapping[str, Any]
    transaction_id: str
    snapshot_id: str
    controller_identity: str
    model_identity: str
    forcing_identity: str
    mode: str
    actuator_source: str
    motor_state_source: str
    plant_time_s: float
    actuator_timestamp_s: float
    motor_state_timestamp_s: float
    motor_state_uncertainty_rad_s: float = 0.0
    ground_supported: bool = False
    ground_contact_evidence: str | None = None

    @classmethod
    def from_mapping(cls, obj: Mapping[str, Any]) -> "ActuatorReplaySample":
        try:
            return cls(**dict(obj))
        except (TypeError, ValueError) as exc:
            raise NativeReplayRejected(f"sample schema: {exc}") from exc

    def validated(self, *, max_age_s: float = 0.1, max_dt_s: float = 0.1) -> dict[str, float]:
        for name in ("transaction_id", "snapshot_id", "controller_identity", "model_identity", "forcing_identity"):
            _text(getattr(self, name), name)
        if _text(self.mode, "mode") not in {"QHOVER", "FBWA", "QLOITER", "QSTABILIZE", "MANUAL", "TRANSITION"}:
            raise NativeReplayRejected("mode: unsupported explicit native mode")
        if self.actuator_source not in VALID_ACTUATOR_SOURCE:
            raise NativeReplayRejected("actuator_source: must be native actuator output, not pilot stick/PWM assumption")
        if self.motor_state_source not in VALID_MOTOR_SOURCE:
            raise NativeReplayRejected("motor_state_source: PWM and pilot throttle are not measured RPM")
        if set(self.values) != set(FIELDS):
            raise NativeReplayRejected(f"op27 exact inputs required: missing={sorted(set(FIELDS)-set(self.values))}; extra={sorted(set(self.values)-set(FIELDS))}")
        u = {k: _finite(v, k) for k, v in self.values.items()}
        if not 0.0 < u["dt"] <= max_dt_s:
            raise NativeReplayRejected("dt: outside certified replay segment bound")
        for key in ("left_motor_cmd", "right_motor_cmd", "soc"):
            if not 0 <= u[key] <= 1:
                raise NativeReplayRejected(f"{key}: outside physical domain")
        for key in ("omega_left", "omega_right"):
            if not 0 <= u[key] <= MAX_OMEGA_RAD_S:
                raise NativeReplayRejected(f"{key}: outside Phoenix profile rad/s domain")
        for key in ("left_elevon_rad", "right_elevon_rad"):
            if abs(u[key]) > MAX_ELEVON_RAD:
                raise NativeReplayRejected(f"{key}: outside Phoenix profile radian domain")
        if not 0.0 <= u["air_density"] or u["air_density"] <= .05:
            raise NativeReplayRejected("air_density: explicit physically positive density required")
        if sum(u[k] ** 2 for k in ("qw", "qx", "qy", "qz")) <= 1e-12:
            raise NativeReplayRejected("attitude quaternion: zero norm")
        plant = _finite(self.plant_time_s, "plant_time_s")
        act = _finite(self.actuator_timestamp_s, "actuator_timestamp_s")
        mot = _finite(self.motor_state_timestamp_s, "motor_state_timestamp_s")
        if not (0 <= plant - act <= max_age_s and 0 <= plant - mot <= max_age_s):
            raise NativeReplayRejected("actuator/motor state: stale, future or mixed time")
        unc = _finite(self.motor_state_uncertainty_rad_s, "motor_state_uncertainty_rad_s")
        if unc < 0 or (self.motor_state_source == "dynamic_motor_estimator" and unc <= 0):
            raise NativeReplayRejected("estimated motor omega requires explicit positive uncertainty")
        if type(self.ground_supported) is not bool:
            raise NativeReplayRejected("ground_supported: must be boolean")
        if self.ground_supported:
            _text(self.ground_contact_evidence, "ground_contact_evidence")
            # Ground contact is a source assertion, not an output of this model.
        return u


def replay_native_actuator(fmu: FormulaFMU, sample: ActuatorReplaySample) -> dict[str, Any]:
    """Return model-only prediction; readiness/certificate always false.

    The FMU is op27 when no independent contact assertion exists; op35 only if
    explicitly supplied.  No direct PWM→omega or pilot throttle→motor mapping.
    """
    u = sample.validated()
    op = 35 if sample.ground_supported else 27
    if op == 35:
        u["ground_supported"] = 1.0
    out = fmu.evaluate_operation(op, use_config=False, require_all_inputs=True,
                                 reset_before=True, inputs=u)
    if not all(math.isfinite(float(v)) for v in out.values()):
        raise NativeReplayRejected("WingMPET output contains nonfinite values")
    return {
        "prediction": out,
        "operation_code": op,
        "model_fmu_sha256": hashlib.sha256(Path(fmu.path).read_bytes()).hexdigest(),
        "transaction_id": sample.transaction_id,
        "snapshot_id": sample.snapshot_id,
        "controller_identity": sample.controller_identity,
        "forcing_identity": sample.forcing_identity,
        "actuator_source": sample.actuator_source,
        "motor_state_source": sample.motor_state_source,
        "model_domain": "NATIVE_ACTUATOR_OUTPUT_SURROGATE",
        "motor_estimate_is_measured": sample.motor_state_source != "dynamic_motor_estimator",
        "native_controller_prediction_certified": False,
        "flight_authorized": False,
        "evidence_scope": "MODEL_ONLY_NOT_NATIVE_DYNAMICS_VERIFICATION",
    }
