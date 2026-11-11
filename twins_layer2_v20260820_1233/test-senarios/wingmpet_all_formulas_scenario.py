#!/usr/bin/env python3
"""Independent future-facing scenario runner for WingMPET.fmu.

Edit only SCENARIO to change the scenario. The runner discovers every FMI
operation from the FMU formula catalog, builds a complete input dictionary,
executes every operation through DomainTwin/FMI, and returns one dictionary
containing the inputs, outputs, and TDDT Layer-2 packet for every operation.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "python"))

from twins_fmi import DomainTwin, DroneConfig  # noqa: E402
from twins_fmi.scenario_defaults import scenario_value  # noqa: E402

# ---------------------------------------------------------------------------
# FUTURE SCENARIO SETTINGS -- edit this dictionary only.
# common_inputs are applied only when a matching input name exists in an
# operation. operation_inputs then override them for a specific operation.
# ---------------------------------------------------------------------------
SCENARIO: dict[str, Any] = {
    "name": "WingMPET_all_formulas_future_view",
    "context_id": "CRP_future_demo",
    "stage_id": "cruise",
    "source": "test-senarios/wingmpet_all_formulas_scenario.py",
    "communication_step": 0.001,
    "common_inputs": {
        "dt": 0.02,
        "altitude": 1000.0,
        "state_h": 1000.0,
        "terrain_height": 900.0,
        "air_density": 1.10,
        "external_density": 1.10,
        "external_pressure": 90000.0,
        "external_temperature": 288.15,
        "relative_humidity": 0.45,
        "airspeed": 13.0,
        "wind_x": 3.0,
        "wind_y": 1.0,
        "wind_h": 0.0,
    },
    "operation_inputs": {
        # Example future override:
        # 5: {"altitude": 1500.0, "airspeed": 15.0},
        # 6: {"wind_x": 6.0, "wind_y": -2.0, "wind_h": 0.5},
    },
}


def _finite_mapping(values: dict[str, Any]) -> bool:
    for value in values.values():
        try:
            if not math.isfinite(float(value)):
                return False
        except (TypeError, ValueError):
            return False
    return True


def build_operation_inputs(
    twin: DomainTwin,
    config: DroneConfig,
    operation_code: int,
) -> dict[str, Any]:
    """Return a complete editable input dictionary for one FMI operation."""
    signature = twin.fmu.operation_signature(operation_code)
    values = config.overlay(signature["inputs"], lambda name, index: scenario_value("WingMPET", name, index))

    common = SCENARIO.get("common_inputs", {})
    for name in signature["inputs"]:
        if name in common:
            values[name] = common[name]

    overrides = SCENARIO.get("operation_inputs", {}).get(operation_code, {})
    values.update(overrides)
    return values


def run_scenario(
    fmu_path: Path,
    config_path: Path,
) -> dict[str, Any]:
    """Execute every formula operation in WingMPET via its independent FMI."""
    config = DroneConfig(config_path)
    result: dict[str, Any] = {
        "scenario": dict(SCENARIO),
        "twin": "WingMPET",
        "fmu": str(fmu_path),
        "config": str(config_path),
        "operations": {},
    }

    total_outputs = 0
    with DomainTwin(fmu_path, config=config) as twin:
        operation_codes = sorted(twin.fmu.operations)
        result["twin_id"] = twin.twin_id
        result["operation_codes"] = operation_codes

        for op in operation_codes:
            input_dict = build_operation_inputs(twin, config, op)
            packet = twin.evaluate_packet(
                op,
                inputs=input_dict,
                use_config=False,
                require_all_inputs=True,
                reset_before=False,
                communication_step=float(SCENARIO["communication_step"]),
                context_id=str(SCENARIO["context_id"]),
                stage_id=str(SCENARIO["stage_id"]),
                source=str(SCENARIO["source"]),
            )
            packet_dict = packet.to_dict()
            output_dict = dict(packet_dict["state"])
            total_outputs += len(output_dict)
            result["operations"][str(op)] = {
                "inputs": input_dict,
                "outputs": output_dict,
                "packet": packet_dict,
                "all_outputs_finite": _finite_mapping(output_dict),
            }

    quality_flags = {"good": 0, "degraded": 0, "invalid": 0}
    novelty_ops = []
    validity_degraded_ops = []
    for op, item in result["operations"].items():
        packet = item["packet"]
        flag = str(packet.get("quality", {}).get("flag", "invalid"))
        quality_flags[flag] = quality_flags.get(flag, 0) + 1
        if packet.get("context", {}).get("tep_novelty", False):
            novelty_ops.append(int(op))
        if float(packet.get("validity", {}).get("score", 0.0)) < 1.0:
            validity_degraded_ops.append(int(op))
    result["summary"] = {
        "operation_count": len(result["operations"]),
        "output_count": total_outputs,
        "all_operations_executed": len(result["operations"]) == len(result["operation_codes"]),
        "all_outputs_finite": all(
            item["all_outputs_finite"] for item in result["operations"].values()
        ),
        "quality_flags": quality_flags,
        "tep_novelty_operation_codes": novelty_ops,
        "validity_degraded_operation_codes": validity_degraded_ops,
        "scenario_inputs_are": "engineering verification priors; not measured field truth",
    }
    return result


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fmu", default=str(ROOT / "fmus" / "WingMPET.fmu"))
    parser.add_argument("--config", default=str(ROOT / "config" / "phoenix_drone_v3.yaml"))
    parser.add_argument(
        "--output",
        default=str(HERE / "WingMPET_all_formulas_scenario_output.json"),
        help="JSON copy of the returned dictionary",
    )
    args = parser.parse_args()

    result = run_scenario(Path(args.fmu), Path(args.config))
    Path(args.output).write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Dictionary view requested for interactive/future use.
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print("\nSUMMARY:", result["summary"])
    return result


if __name__ == "__main__":
    main()
