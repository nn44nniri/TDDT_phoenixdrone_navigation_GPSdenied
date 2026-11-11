from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping


class FMIExecutionError(RuntimeError):
    """Raised when strict FMI-only generation cannot continue."""


def _finite_dict(values: Mapping[str, Any], *, model: str, operation: int) -> dict[str, float]:
    result: dict[str, float] = {}
    for key, value in values.items():
        number = float(value)
        if not math.isfinite(number):
            raise FMIExecutionError(f"{model} operation {operation} returned non-finite {key}={value!r}")
        result[key] = number
    return result


@dataclass
class FMITrace:
    wing_calls: int = 0
    mesa_calls: int = 0

    @property
    def total(self) -> int:
        return self.wing_calls + self.mesa_calls


class StrictFMIEngine:
    """Dispatch SARG flight/navigation calculations to the Layer-2 FMUs.

    The adapter understands both the legacy fixed-wing operation set and the
    Phoenix-FA v20260820 operation set.  The current Phoenix profile intentionally
    removed WingMPET operations 10/26 and MESA-NAV operations 8/20.  SARG must
    therefore bind by *semantic capability* rather than hard-coding those retired
    operation numbers.

    Python remains orchestration-only: dataset/map/CRP I/O, policy scheduling,
    coordinate/unit adaptation, acceptance/retry and serialization.  No removed
    FMI operation is emulated with a substitute physical/navigation equation.
    """

    WING_COMMON = {1, 2, 5, 6, 7, 9, 13, 14, 15, 16}
    MESA_COMMON = {1, 7, 10, 14, 15, 19, 21}

    # Current Phoenix dual-rotor tailsitter contract (20260820_1141 / library 1232).
    WING_PHOENIX = {30, 34}      # mode-aware VTOL envelope + completeness
    MESA_PHOENIX = {11, 24, 27}  # terrain-flow anchor + mode-aware constraints + completeness

    # Backward compatibility for older SARG/twins packages.
    WING_LEGACY = {10}
    MESA_LEGACY = {8, 20}

    def __init__(self, manager: Any, config: Any):
        self.manager = manager
        self.wing = manager.wingmpet
        self.mesa = manager.mesa
        self.config = config
        self.trace = FMITrace()
        self.contract_profile = "unknown"
        self.contract_details: dict[str, Any] = {}
        self._validate_contract()

    def _validate_contract(self) -> None:
        wing_ops = set(int(x) for x in self.wing.list_operations())
        mesa_ops = set(int(x) for x in self.mesa.list_operations())

        missing_common_w = sorted(self.WING_COMMON - wing_ops)
        missing_common_m = sorted(self.MESA_COMMON - mesa_ops)
        if missing_common_w or missing_common_m:
            raise FMIExecutionError(
                f"Required common FMI operations are missing: WingMPET={missing_common_w}, "
                f"MESA_NAV={missing_common_m}"
            )

        phoenix_ok = self.WING_PHOENIX.issubset(wing_ops) and self.MESA_PHOENIX.issubset(mesa_ops)
        legacy_ok = self.WING_LEGACY.issubset(wing_ops) and self.MESA_LEGACY.issubset(mesa_ops)
        if phoenix_ok:
            self.contract_profile = "PHOENIX_FA_20260820"
        elif legacy_ok:
            self.contract_profile = "LEGACY_FIXED_WING"
        else:
            raise FMIExecutionError(
                "Unsupported Layer-2 FMI operation contract. "
                f"Phoenix alternatives require WingMPET={sorted(self.WING_PHOENIX)} and "
                f"MESA_NAV={sorted(self.MESA_PHOENIX)}; legacy alternatives require "
                f"WingMPET={sorted(self.WING_LEGACY)} and MESA_NAV={sorted(self.MESA_LEGACY)}. "
                f"Available WingMPET={sorted(wing_ops)}, MESA_NAV={sorted(mesa_ops)}"
            )

        wing_id = self.wing_call(1)
        mesa_id = self.mesa_call(1)
        if self.contract_profile == "PHOENIX_FA_20260820":
            # The updated library reports Wing formalism 3 and MESA formalism 4.
            if wing_id.get("formalism_version", 0) < 3 or mesa_id.get("formalism_version", 0) < 4:
                raise FMIExecutionError(
                    f"Phoenix-FA operation set is present but formalism versions are inconsistent: "
                    f"WingMPET={wing_id}, MESA_NAV={mesa_id}"
                )
            wing_complete = self.wing_call(34)
            mesa_complete = self.mesa_call(27)
            if wing_complete.get("formalism_complete_flag", 0.0) < 0.5:
                raise FMIExecutionError(f"WingMPET Phoenix-FA completeness check failed: {wing_complete}")
            if mesa_complete.get("formalism_complete_flag", 0.0) < 0.5:
                raise FMIExecutionError(f"MESA-NAV Phoenix-FA completeness check failed: {mesa_complete}")
            if wing_complete.get("vtol_full_envelope_flag", 0.0) < 0.5:
                raise FMIExecutionError(f"WingMPET does not advertise the required VTOL full-envelope contract: {wing_complete}")
            if mesa_complete.get("mode_aware_constraints_flag", 0.0) < 0.5:
                raise FMIExecutionError(f"MESA-NAV does not advertise mode-aware VTOL constraints: {mesa_complete}")
            self.contract_details = {
                "retired_operations": {
                    "WingMPET": [10, 26],
                    "MESA_NAV": [8, 20],
                },
                "replacement_operations": {
                    "WingMPET.flight_envelope": 30,
                    "WingMPET.completeness": 34,
                    "MESA_NAV.wind_context_anchor": 11,
                    "MESA_NAV.mechanical_constraints": 24,
                    "MESA_NAV.completeness": 27,
                    "MESA_NAV.physical_ultrasonic_wind_optional": 23,
                },
                "wind_semantics": (
                    "CRP wind is environmental/map context. It is not synthesized as a physical "
                    "ULTRASONIC_WIND observation; operation 23 is reserved for a real/emulated-realistic sensor sample."
                ),
            }
        else:
            if wing_id.get("formalism_version", 0) < 2 or mesa_id.get("formalism_version", 0) < 3:
                raise FMIExecutionError(f"Unsupported legacy twin formalism versions: WingMPET={wing_id}, MESA_NAV={mesa_id}")
            self.contract_details = {
                "legacy_operations": {
                    "WingMPET.flight_envelope": 10,
                    "MESA_NAV.wind_anchor": 8,
                    "MESA_NAV.mechanical_constraints": 20,
                }
            }

    def wing_call(self, operation: int, **inputs: Any) -> dict[str, float]:
        try:
            raw = self.wing.evaluate_operation(operation, inputs=inputs, use_config=True, require_all_inputs=False)
        except Exception as exc:
            raise FMIExecutionError(f"WingMPET operation {operation} failed: {exc}") from exc
        self.trace.wing_calls += 1
        return _finite_dict(raw, model="WingMPET", operation=operation)

    def mesa_call(self, operation: int, **inputs: Any) -> dict[str, float]:
        try:
            raw = self.mesa.evaluate_operation(operation, inputs=inputs, use_config=True, require_all_inputs=False)
        except Exception as exc:
            raise FMIExecutionError(f"MESA_NAV operation {operation} failed: {exc}") from exc
        self.trace.mesa_calls += 1
        return _finite_dict(raw, model="MESA_NAV", operation=operation)

    def _wind_context(self, *, wind: Mapping[str, float], dt: float) -> dict[str, float]:
        """Evaluate the wind-related MESA reference without violating Phoenix sensor semantics."""
        nis_gate = float(self.config.get("navigation.nis_gate", 6.0))
        if self.contract_profile == "PHOENIX_FA_20260820":
            # SARG's CRP field is a map/environment reference, not a physical sensor.
            # The nominal offline reference residual is zero unless an upstream CRP
            # product explicitly supplies a model-consistency residual.  Operation 11
            # performs the robust MESA terrain-flow anchor update.  We intentionally
            # do NOT call operation 23 from CRP data because that operation is reserved
            # for a physical ULTRASONIC_WIND sample.
            residual = float(wind.get("terrain_flow_residual_ms", 0.0))
            jac_n = float(wind.get("terrain_flow_jacobian_n", 0.0))
            jac_e = float(wind.get("terrain_flow_jacobian_e", 0.0))
            raw = self.mesa_call(
                11,
                terrain_flow_anchor_residual=residual,
                jacobian_n=jac_n,
                jacobian_e=jac_e,
                measurement_variance=max(1e-9, float(wind["variance"])),
                nis_gate=nis_gate,
            )
            return {
                **raw,
                # Compatibility aliases consumed by the SARG summary layer.
                "wind_residual_norm": abs(raw["terrain_flow_update_residual"]),
                "flow_anchor_valid": raw["terrain_flow_update_valid"],
                "flow_anchor_weight": raw["terrain_flow_update_weight"],
                "spatial_information_valid": 1.0 if (abs(jac_n) + abs(jac_e)) > 0.0 else 0.0,
                "physical_ultrasonic_used": 0.0,
            }

        # Legacy fixed-wing MESA used an airflow-relative pseudo-sensor (operation 8).
        # Kept only to preserve backward compatibility with older twins packages.
        return self.mesa_call(
            8,
            flow_bx=float(wind.get("legacy_flow_bx", wind["x"])),
            flow_by=float(wind.get("legacy_flow_by", wind["y"])),
            flow_bz=float(wind.get("legacy_flow_bz", wind["h"])),
            flow_filter_tau=float(self.config.get("navigation.wind_filter_tau_s", 10.0)),
            flow_dt=dt,
            wind_map_n=wind["x"], wind_map_e=wind["y"], wind_map_d=-wind["h"],
            wind_residual_variance=wind["variance"], nis_gate=nis_gate,
        )

    @staticmethod
    def acceptance_flags(result, safety=None):
        """Accept only the FMU envelope, mechanical and spatially aligned safety verdicts."""
        safety=result["safety"] if safety is None else safety
        return bool(result["envelope"]["flight_envelope_valid"] >= .5
                    and safety["safety_valid"] >= .5
                    and result["mechanical"]["mechanical_constraints_valid"] >= .5)

    def step(self, *, dt: float, state: Mapping[str, float], target: Mapping[str, float],
             airspeed_command: float, climb_rate_command: float,
             wind: Mapping[str, float], atmosphere: Mapping[str, float],
             terrain_height: float, obstacle_distance: float,
             clearance_required: float, position_sigma: float,
             elapsed_path: float, elapsed_time: float, elapsed_energy_j: float,
             policy_weights: Mapping[str, float]) -> dict[str, Any]:
        # WingMPET computes goal geometry. Python adapts the polar command to the
        # Cartesian input contract of operation 7; propagation remains in the FMU.
        goal = self.wing_call(15, x=state["x"], y=state["y"], h=state["h"],
                              goal_x=target["x"], goal_y=target["y"], goal_h=target["h"],
                              initial_goal_distance=target["initial_distance"], goal_tolerance=target["tolerance"])
        heading = goal["goal_bearing"]
        horizontal = max(0.0, airspeed_command * airspeed_command - climb_rate_command * climb_rate_command) ** 0.5

        # Wind-triangle command adaptation. WingMPET operation 7 still computes
        # the propagated state; this only maps a policy command to its FMI inputs.
        track_x = math.cos(heading)
        track_y = math.sin(heading)
        normal_x = -track_y
        normal_y = track_x
        wind_cross = wind["x"] * normal_x + wind["y"] * normal_y
        wind_along = wind["x"] * track_x + wind["y"] * track_y
        if abs(wind_cross) >= horizontal:
            raise FMIExecutionError(
                f"Crosswind {abs(wind_cross):.3f} m/s exceeds available horizontal airspeed "
                f"{horizontal:.3f} m/s; destination track cannot be maintained"
            )
        air_along = math.sqrt(max(0.0, horizontal * horizontal - wind_cross * wind_cross))
        if air_along + wind_along <= 0.0:
            raise FMIExecutionError("Headwind prevents positive progress toward Destination at the commanded airspeed")
        vax = air_along * track_x - wind_cross * normal_x
        vay = air_along * track_y - wind_cross * normal_y
        vah = climb_rate_command

        wing_wind = self.wing_call(
            6, wind_x=wind["x"], wind_y=wind["y"], wind_h=wind["h"],
            ground_velocity_x=0.0, ground_velocity_y=0.0, ground_velocity_h=0.0,
            wind_time_derivative_x=0.0, wind_time_derivative_y=0.0, wind_time_derivative_h=0.0,
            wind_jacobian_11=0.0, wind_jacobian_12=0.0, wind_jacobian_13=0.0,
            wind_jacobian_21=0.0, wind_jacobian_22=0.0, wind_jacobian_23=0.0,
            wind_jacobian_31=0.0, wind_jacobian_32=0.0, wind_jacobian_33=0.0,
        )
        wind_est = self._wind_context(wind={**wind, "legacy_flow_bx": vax + wind["x"],
                                            "legacy_flow_by": vay + wind["y"],
                                            "legacy_flow_bz": vah + wind["h"]}, dt=dt)
        motion = self.wing_call(
            7, dt=dt, x=state["x"], y=state["y"], h=state["h"],
            air_velocity_x=vax, air_velocity_y=vay, air_velocity_h=vah,
            wind_x=wing_wind["wind_x_echo"], wind_y=wing_wind["wind_y_echo"], wind_h=wing_wind["wind_h_echo"],
            target_x=target["x"], target_y=target["y"], target_h=target["h"],
        )

        air = self.wing_call(5, altitude=motion["next_h"], external_temperature=atmosphere["temperature"],
                             external_pressure=atmosphere["pressure"], external_density=atmosphere["density"],
                             relative_humidity=atmosphere["humidity"], airspeed=airspeed_command)
        mass = float(self.config.get("mass.empty_or_flight_mass_kg",
                                     self.config.get("derived.mission_mass_kg", 0.65)))
        gravity = float(self.config.get("model.gravity_m_s2", 9.80665))
        bank = policy_weights["bank_angle"]
        lift_command = mass * gravity / max(0.1, math.cos(bank))
        aero = self.wing_call(
            9, air_density=air["air_density"], airspeed=airspeed_command, mass=mass,
            wing_area=float(self.config.get("geometry.wing_area_m2", 0.12)),
            cd0=float(self.config.get("aerodynamics.cd0", 0.045)),
            induced_drag_factor=float(self.config.get("derived.induced_drag_factor", 0.13)),
            cl_max=float(self.config.get("aerodynamics.cl_max", 1.05)), bank_angle=bank,
            flightpath_angle=motion["ground_flightpath"], lift_command=lift_command,
        )

        if self.contract_profile == "PHOENIX_FA_20260820":
            raw_envelope = self.wing_call(
                30,
                airspeed=airspeed_command,
                sideslip=0.0,
                bank_angle=bank,
                flightpath_angle=motion["ground_flightpath"],
                p_rate=0.0, q_rate=0.0, r_rate=0.0,
                load_factor=aero["load_factor"],
                # SARG route generation is a wing-borne reference path.  We do not
                # invent motor telemetry; operation 30 gates hover thrust by mode.
                total_thrust=0.0,
                mass=mass,
                left_motor_cmd=0.0, right_motor_cmd=0.0,
                left_elevon_rad=0.0, right_elevon_rad=0.0,
            )
            envelope = {**raw_envelope, "flight_envelope_valid": raw_envelope["vtol_envelope_valid"]}
        else:
            envelope = self.wing_call(
                10, airspeed=airspeed_command, bank_angle=bank,
                flightpath_angle=motion["ground_flightpath"], cl=aero["cl"],
                load_factor=aero["load_factor"],
                min_airspeed=float(self.config.get("speed.minimum_command_speed_m_s", 0.0)),
                max_airspeed=float(self.config.get("speed.maximum_speed_m_s", 1e6)),
                max_bank=math.radians(float(self.config.get("control_limits.max_bank_angle_deg", 45.0))),
                min_flightpath=-math.radians(float(self.config.get("control_limits.max_flightpath_angle_deg", 20.0))),
                max_flightpath=math.radians(float(self.config.get("control_limits.max_flightpath_angle_deg", 20.0))),
                max_cl=float(self.config.get("aerodynamics.cl_max", 1.05)),
                max_load_factor=float(self.config.get("control_limits.max_load_factor", 2.5)),
            )

        safety = self.wing_call(
            13, altitude=motion["next_h"], terrain_height=terrain_height,
            clearance=clearance_required, elevated_obstacle_distance=obstacle_distance,
            ground_obstacle_distance=obstacle_distance, base_safe_radius=clearance_required,
            position_sigma=position_sigma, wind_sigma=wind["sigma"], inflation_factor=policy_weights["inflation_factor"],
        )
        accumulation = self.wing_call(
            14, dt=dt, groundspeed=motion["groundspeed"], power=aero["aerodynamic_power"],
            path_length_prev=elapsed_path, time_prev=elapsed_time, energy_prev=elapsed_energy_j,
        )

        pitot = self.mesa_call(
            7, air_density=air["air_density"], pitot_pressure=air["dynamic_pressure"],
            predicted_airspeed=airspeed_command,
            airspeed_variance=float(self.config.get("navigation.default_sensor_variance", 1.0)),
        )
        pressure = self.mesa_call(
            10, absolute_pressure=air["air_pressure"],
            log_pressure_prior=math.log(max(1.0, air["air_pressure"])),
            log_scale_height=math.log(float(self.config.get("navigation.pressure_scale_height_m", 8400.0))),
            pressure_grad_n=0.0, pressure_grad_e=0.0,
            pressure_variance=float(self.config.get("navigation.default_sensor_variance", 1.0)),
            nis_gate=float(self.config.get("navigation.nis_gate", 6.0)),
            pressure_time_scale=float(self.config.get("navigation.pressure_time_scale_s", 30.0)),
        )
        robust = self.mesa_call(
            14, residual=pitot["pitot_residual"], innovation_variance=max(1e-9, pitot["pitot_nis"] + 1.0),
            nis_gate=float(self.config.get("navigation.nis_gate", 6.0)),
            student_nu=float(self.config.get("navigation.student_t_nu", 4.0)),
        )

        # CRP wind has no physical ultrasonic sample and no supplied spatial
        # gradient in the current SARG contract.  Therefore it must not create
        # artificial horizontal information.  Pressure/pitot consistency can
        # still contribute to the reference observability score.
        wind_information = 0.0 if self.contract_profile == "PHOENIX_FA_20260820" else wind_est.get("flow_anchor_valid", 0.0)
        observability = self.mesa_call(
            15,
            min_information_eigenvalue=max(0.0, wind_information + pressure["pressure_anchor_valid"] + robust["robust_weight"]),
            min_condition_score=policy_weights["condition_score"],
        )
        excitation_inputs = {"info_11": 0.0, "info_12": 0.0, "info_22": 0.0, "candidate_count": 3.0}
        for candidate in range(1, 21):
            active = candidate <= 3
            scale = policy_weights["condition_score"] * candidate if active else 0.0
            excitation_inputs[f"candidate_{candidate:02d}_info11"] = scale
            excitation_inputs[f"candidate_{candidate:02d}_info12"] = 0.05 * scale if active else 0.0
            excitation_inputs[f"candidate_{candidate:02d}_info22"] = 0.8 * scale if active else 0.0
        excitation = self.mesa_call(19, **excitation_inputs)

        if self.contract_profile == "PHOENIX_FA_20260820":
            mechanical = self.mesa_call(
                24,
                airspeed=airspeed_command,
                groundspeed=motion["groundspeed"],
                bank_angle=bank,
                sideslip=0.0,
                load_factor=aero["load_factor"],
                p_rate=0.0, q_rate=0.0, r_rate=0.0,
                stall_speed=float(self.config.get("speed.stall_speed_m_s", aero["stall_speed"])),
                command_min_speed=float(self.config.get("speed.minimum_command_speed_m_s", 12.0)),
                max_airspeed=float(self.config.get("speed.maximum_speed_m_s", 18.0)),
                max_load_factor=float(self.config.get("control_limits.max_load_factor", 2.5)),
                max_sideslip=math.radians(float(self.config.get("control_limits.max_sideslip_deg", 12.0))),
                min_lambda=0.0,
            )
        else:
            mechanical = self.mesa_call(
                20, airspeed=airspeed_command, stall_speed=aero["stall_speed"],
                load_factor=aero["load_factor"],
                max_load_factor=float(self.config.get("control_limits.max_load_factor", 2.5)),
                sideslip=0.0,
                max_sideslip=math.radians(float(self.config.get("control_limits.max_sideslip_deg", 10.0))),
            )

        covariance = self.mesa_call(21)
        objective = self.wing_call(
            16, path_length=accumulation["path_length_total"], mission_time=accumulation["time_total"],
            energy=accumulation["energy_total"],
            safety_penalty=max(0.0, -safety["terrain_margin"]) + max(0.0, -safety["obstacle_margin"]),
            length_reference=target["initial_distance"], time_reference=max(dt, target["time_reference"]),
            energy_reference=max(1.0, target["energy_reference"]), safety_reference=1.0,
            weight_length=policy_weights["length"], weight_time=policy_weights["time"],
            weight_energy=policy_weights["energy"], weight_safety=policy_weights["safety"],
        )
        accepted = bool(
            envelope["flight_envelope_valid"] >= 0.5 and
            safety["safety_valid"] >= 0.5 and
            mechanical["mechanical_constraints_valid"] >= 0.5
        )
        return {
            "goal": goal, "motion": motion, "air": air, "aero": aero, "envelope": envelope,
            "safety": safety, "accumulation": accumulation, "pitot": pitot, "wind": wind_est,
            "pressure": pressure, "robust": robust, "observability": observability, "excitation": excitation,
            "mechanical": mechanical, "covariance": covariance, "objective": objective,
            "accepted": accepted, "contract_profile": self.contract_profile,
        }
