"""Structured error dictionaries shared by Climate Twin CLI scripts."""
from __future__ import annotations

from typing import Any, Dict

ALTITUDE_BELOW_GROUND = "CLIMATE_ALTITUDE_BELOW_GROUND"
ALTITUDE_LAYER_UNAVAILABLE = "CLIMATE_ALTITUDE_LAYER_UNAVAILABLE"
INVALID_VERTICAL_REFERENCE = "CLIMATE_INVALID_VERTICAL_REFERENCE"
TIME_OUTSIDE_FORECAST_RANGE = "CLIMATE_TIME_OUTSIDE_FORECAST_RANGE"


def error_dict(code: str, message: str, **details: Any) -> Dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "details": details,
        },
    }


def below_ground_error(*, requested_msl_m: float, ground_msl_m: float, latitude: float | None = None,
                       longitude: float | None = None, context: str = "") -> Dict[str, Any]:
    agl = requested_msl_m - ground_msl_m
    details: Dict[str, Any] = {
        "requested_altitude_msl_m": requested_msl_m,
        "ground_elevation_msl_m": ground_msl_m,
        "computed_altitude_agl_m": agl,
        "vertical_reference": "MSL",
        "context": context,
    }
    if latitude is not None:
        details["latitude"] = latitude
    if longitude is not None:
        details["longitude"] = longitude
    return error_dict(
        ALTITUDE_BELOW_GROUND,
        "Requested MSL altitude is below the local ground surface; climate estimation is physically invalid.",
        **details,
    )


def time_outside_forecast_error(*, requested_time: str, forecast_start: str, forecast_end: str,
                                input_file: str, affected_points: list[str] | None = None,
                                context: str = "") -> Dict[str, Any]:
    """Return a stable machine-readable error for an out-of-range request time."""
    details: Dict[str, Any] = {
        "requested_time": requested_time,
        "forecast_start": forecast_start,
        "forecast_end": forecast_end,
        "input_file": input_file,
        "context": context,
    }
    if affected_points:
        details["affected_points"] = affected_points
    return error_dict(
        TIME_OUTSIDE_FORECAST_RANGE,
        "Requested time is outside the available offline forecast range.",
        **details,
    )
