from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import yaml

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "phoenix_drone_v3.yaml"


def _flatten(data: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            result.update(_flatten(value, path))
        else:
            result[path] = value
    return result


ALIASES: dict[str, str] = {
    "gravity": "model.gravity_m_s2",
    "g": "model.gravity_m_s2",
    "mass": "derived.mission_mass_kg",
    "dry_mass": "mass.empty_or_flight_mass_kg",
    "wing_area": "geometry.wing_area_m2",
    "s": "geometry.wing_area_m2",
    "wing_span": "geometry.wing_span_m",
    "b": "geometry.wing_span_m",
    "aspect_ratio": "derived.aspect_ratio",
    "e": "aerodynamics.oswald_efficiency",
    "cd0": "aerodynamics.cd0",
    "cl_max": "aerodynamics.cl_max",
    "clmax": "aerodynamics.cl_max",
    "max_cl": "aerodynamics.cl_max",
    "induced_drag_factor": "derived.induced_drag_factor",
    "min_airspeed": "speed.minimum_command_speed_m_s",
    "max_airspeed": "speed.maximum_speed_m_s",
    "command_min_speed": "speed.minimum_command_speed_m_s",
    "airspeed": "speed.cruise_speed_m_s",
    "stall_speed": "derived.stall_speed_sea_level_m_s",
    "propeller_diameter": "propulsion.propeller_diameter_m",
    "prop_d": "propulsion.propeller_diameter_m",
    "motor_efficiency": "propulsion.motor_efficiency",
    "motor_eff": "propulsion.motor_efficiency",
    "propeller_efficiency": "propulsion.propeller_efficiency",
    "prop_eff": "propulsion.propeller_efficiency",
    "inverter_efficiency": "propulsion.inverter_efficiency",
    "inverter_eff": "propulsion.inverter_efficiency",
    "thrust": "propulsion.maximum_total_thrust_N",
    "thrust_cmd": "propulsion.maximum_total_thrust_N",
    "battery_ocv": "battery.nominal_voltage_V",
    "battery_voc": "battery.nominal_voltage_V",
    "battery_capacity_ah": "battery.capacity_Ah",
    "battery_q_ah": "battery.capacity_Ah",
    "battery_resistance": "battery.internal_resistance_ohm_estimated",
    "battery_r": "battery.internal_resistance_ohm_estimated",
    "soc": "battery.initial_soc",
    "dt": "navigation.imu_dt_s",
    "flow_dt": "navigation.imu_dt_s",
    "pressure_time_scale": "navigation.pressure_time_scale_s",
    "nis_gate": "navigation.nis_gate",
    "student_nu": "navigation.student_t_nu",
    "forgetting_factor": "navigation.rls_forgetting_factor",
    "background_time_constant": "navigation.wind_filter_tau_s",
    "flow_filter_tau": "navigation.wind_filter_tau_s",
    "max_load_factor": "control_limits.max_load_factor",
}


class DroneConfig:
    """Validated aircraft configuration and FMI-name resolver."""

    def __init__(self, path: str | Path = DEFAULT_CONFIG) -> None:
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        loaded = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        if not isinstance(loaded, Mapping):
            raise ValueError("Drone configuration root must be a mapping")
        self.data = dict(loaded)
        self.flat = _flatten(self.data)
        self._derive()
        self._validate()

    def _derive(self) -> None:
        f = self.flat
        baseline_mass = float(f["mass.empty_or_flight_mass_kg"])
        payload = float(f.get("mass.payload_nominal_kg", 0.0))
        f["derived.mission_mass_kg"] = baseline_mass + payload
        span = float(f["geometry.wing_span_m"])
        area = float(f["geometry.wing_area_m2"])
        aspect_ratio = span * span / area
        f["derived.aspect_ratio"] = aspect_ratio
        oswald = float(f["aerodynamics.oswald_efficiency"])
        f["derived.induced_drag_factor"] = 1.0 / (math.pi * oswald * aspect_ratio)
        f["derived.nominal_energy_Wh"] = float(f["battery.nominal_voltage_V"]) * float(f["battery.capacity_Ah"])
        density = 1.225
        gravity = float(f["model.gravity_m_s2"])
        cl_max = float(f["aerodynamics.cl_max"])
        f["derived.stall_speed_sea_level_m_s"] = math.sqrt(2.0 * f["derived.mission_mass_kg"] * gravity / (density * area * cl_max))

    def _validate(self) -> None:
        required = [
            "model.gravity_m_s2",
            "mass.empty_or_flight_mass_kg",
            "geometry.wing_span_m",
            "geometry.wing_area_m2",
            "aerodynamics.cl_max",
            "aerodynamics.cd0",
            "aerodynamics.oswald_efficiency",
            "battery.capacity_Ah",
        ]
        missing = [name for name in required if name not in self.flat]
        if missing:
            raise ValueError(f"Missing required parameters: {missing}")
        positive = [
            "model.gravity_m_s2",
            "mass.empty_or_flight_mass_kg",
            "geometry.wing_area_m2",
            "geometry.wing_span_m",
            "aerodynamics.cl_max",
            "aerodynamics.oswald_efficiency",
            "battery.capacity_Ah",
        ]
        invalid = [name for name in positive if float(self.flat[name]) <= 0]
        if invalid:
            raise ValueError(f"Non-positive aircraft parameters: {invalid}")
        if not 0.0 <= float(self.flat.get("battery.initial_soc", 1.0)) <= 1.0:
            raise ValueError("battery.initial_soc must be in [0, 1]")

    def get(self, path: str, default: Any = None) -> Any:
        return self.flat.get(path, default)

    def require(self, path: str) -> Any:
        if path not in self.flat:
            raise KeyError(f"Configuration parameter not found: {path}")
        return self.flat[path]

    def resolve(self, fmi_name: str, default: Any = None) -> Any:
        name = fmi_name.strip()
        normalized = name.lower()
        key = ALIASES.get(name) or ALIASES.get(normalized)
        if key is not None and key in self.flat:
            return float(self.flat[key])
        # Never resolve by arbitrary substring.  Short aliases such as ``g``
        # and ``s`` are valid exact FMI names, but substring matching made
        # unrelated variables (e.g. event_age or msl_altitude) silently map
        # to gravity or wing area.  Formula aliases are explicit in the FMI
        # catalog, so exact normalized matching is the safe contract.
        if "max_bank" in normalized:
            return math.radians(float(self.require("control_limits.max_bank_angle_deg")))
        if "max_flightpath" in normalized:
            return math.radians(float(self.require("control_limits.max_flightpath_angle_deg")))
        if "min_flightpath" in normalized:
            return -math.radians(float(self.require("control_limits.max_flightpath_angle_deg")))
        if "max_sideslip" in normalized or normalized == "sideslip":
            return math.radians(float(self.require("control_limits.max_sideslip_deg")))
        if normalized == "log_scale_height":
            return math.log(float(self.require("navigation.pressure_scale_height_m")))
        return default

    def overlay(
        self,
        names: Iterable[str],
        fallback: Callable[[str, int], Any] | None = None,
        *,
        strict: bool = False,
    ) -> dict[str, float]:
        result: dict[str, float] = {}
        unresolved: list[str] = []
        for index, name in enumerate(names, 1):
            value = self.resolve(name)
            if value is None and fallback is not None:
                value = fallback(name, index)
            if value is None:
                unresolved.append(name)
            else:
                result[name] = float(value)
        if strict and unresolved:
            raise KeyError(f"Unresolved FMI variables from configuration: {unresolved}")
        return result

    def direct_parameter_paths(self, model: str) -> list[str]:
        common = [
            "model.gravity_m_s2",
            "mass.empty_or_flight_mass_kg",
            "mass.payload_nominal_kg",
            "geometry.wing_area_m2",
            "aerodynamics.cl_max",
            "aerodynamics.cd0",
            "aerodynamics.induced_drag_factor",
            "speed.minimum_command_speed_m_s",
            "speed.maximum_speed_m_s",
            "propulsion.maximum_total_thrust_N",
            "control_limits.max_bank_angle_deg",
            "control_limits.max_flightpath_angle_deg",
            "control_limits.max_load_factor",
        ]
        model = model.upper().replace("-", "_")
        if model == "MESA_NAV":
            return common + [
                "control_limits.max_sideslip_deg",
                "navigation.imu_dt_s",
                "navigation.pitot_scale_initial",
                "navigation.pressure_scale_height_m",
                "navigation.pressure_time_scale_s",
                "navigation.default_sensor_variance",
                "navigation.nis_gate",
                "navigation.student_t_nu",
                "navigation.rls_forgetting_factor",
                "navigation.wind_filter_tau_s",
            ]
        if model == "WINGMPET":
            return common + [
                "geometry.wing_span_m",
                "aerodynamics.cl_min",
                "aerodynamics.oswald_efficiency",
                "speed.cruise_speed_m_s",
                "propulsion.motor_count",
                "propulsion.propeller_diameter_m",
                "propulsion.propeller_pitch_m",
                "propulsion.propeller_efficiency",
                "propulsion.maximum_thrust_each_N",
                "propulsion.motor_kv_rpm_v",
                "propulsion.motor_max_current_A_each",
                "propulsion.motor_cont_current_A_each",
                "propulsion.motor_efficiency",
                "propulsion.peak_power_total_W",
                "propulsion.continuous_power_total_W",
                "propulsion.esc_current_limit_A_each",
                "propulsion.inverter_efficiency",
                "battery.nominal_voltage_V",
                "battery.full_voltage_V",
                "battery.capacity_Ah",
                "battery.internal_resistance_ohm_estimated",
                "battery.maximum_current_A_estimated",
                "battery.initial_soc",
                "mission.recommended_service_ceiling_m_msl",
            ]
        raise ValueError(f"Unknown model: {model}")
