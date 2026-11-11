from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any, Mapping

from .drone_config import DroneConfig
from .native_fmu import FMIError, NativeFMU


class FormulaFMU(NativeFMU):
    """Formula-level FMI interface backed by the FMU's embedded catalog."""

    def __init__(self, path: str | Path, *, config: DroneConfig | str | Path | None = None, **kwargs: Any) -> None:
        super().__init__(path, **kwargs)
        try:
            with zipfile.ZipFile(Path(path)) as archive:
                self.catalog = json.loads(archive.read("resources/formula_catalog.json"))
        except KeyError as exc:
            self.close(suppress_errors=True)
            raise FMIError(f"FMU has no resources/formula_catalog.json: {path}") from exc
        self.formulas = {item["id"]: item for item in self.catalog["formulas"]}
        self.operations = {int(key): value for key, value in self.catalog["operations"].items()}
        self.config = config if isinstance(config, DroneConfig) else (DroneConfig(config) if config is not None else None)

    def list_formulas(self) -> list[str]:
        return sorted(self.formulas)

    def list_operations(self) -> list[int]:
        return sorted(self.operations)

    def operation_signature(self, operation_code: int) -> Mapping[str, Any]:
        try:
            return self.operations[int(operation_code)]
        except KeyError as exc:
            raise KeyError(f"Unknown operation code {operation_code} for {self.name}") from exc

    def configuration_inputs(self, operation_code: int) -> dict[str, float]:
        if self.config is None:
            return {}
        signature = self.operation_signature(operation_code)
        return self.config.overlay(signature.get("inputs", []), strict=False)

    def evaluate_operation(
        self,
        operation_code: int,
        *,
        communication_step: float = 1e-3,
        use_config: bool = True,
        require_all_inputs: bool = False,
        reset_before: bool = False,
        inputs: Mapping[str, Any] | None = None,
        **named_inputs: Any,
    ) -> dict[str, float]:
        operation_code = int(operation_code)
        signature = self.operation_signature(operation_code)
        supplied = dict(inputs or {})
        overlap = set(supplied).intersection(named_inputs)
        if overlap:
            raise ValueError(f"Inputs supplied twice: {sorted(overlap)}")
        supplied.update(named_inputs)
        allowed = set(signature.get("inputs", []))
        unknown = set(supplied) - allowed
        if unknown:
            raise KeyError(f"Unknown inputs for operation {operation_code}: {sorted(unknown)}")

        resolved = self.configuration_inputs(operation_code) if use_config else {}
        resolved.update(supplied)
        missing = allowed - set(resolved)
        if require_all_inputs and missing:
            raise KeyError(f"Missing inputs for operation {operation_code}: {sorted(missing)}")
        for name in missing:
            resolved[name] = 0.0

        if reset_before:
            self.reset()
        fmi_inputs = {f"op{operation_code:02d}__in__{name}": value for name, value in resolved.items()}
        fmi_inputs["formula_operation_code"] = float(operation_code)
        self.set_values(fmi_inputs)
        self.step(communication_step)
        output_names = list(signature.get("outputs", []))
        fmi_names = [f"op{operation_code:02d}__out__{name}" for name in output_names]
        raw = self.get_values(fmi_names)
        return {name: float(raw[f"op{operation_code:02d}__out__{name}"]) for name in output_names}


    def evaluate_operation_enveloped(
        self,
        operation_code: int,
        *,
        communication_step: float = 1e-3,
        use_config: bool = True,
        require_all_inputs: bool = False,
        reset_before: bool = False,
        inputs: Mapping[str, Any] | None = None,
        **named_inputs: Any,
    ) -> dict[str, Any]:
        """Evaluate an operation and return the full semantic signal schema.

        Returned values use the canonical fields: value, unit, frame, timestamp,
        source, quality, uncertainty, validity.
        """
        operation_code = int(operation_code)
        signature = self.operation_signature(operation_code)
        supplied = dict(inputs or {})
        supplied.update(named_inputs)
        allowed = set(signature.get("inputs", []))
        unknown = set(supplied) - allowed
        if unknown:
            raise KeyError(f"Unknown inputs for operation {operation_code}: {sorted(unknown)}")
        resolved = self.configuration_inputs(operation_code) if use_config else {}
        resolved.update(supplied)
        missing = allowed - set(resolved)
        if require_all_inputs and missing:
            raise KeyError(f"Missing inputs for operation {operation_code}: {sorted(missing)}")
        for name in missing:
            resolved[name] = 0.0
        if reset_before:
            self.reset()
        payload = {f"op{operation_code:02d}__in__{name}": value for name, value in resolved.items()}
        payload["formula_operation_code"] = float(operation_code)
        self.set_enveloped(payload)
        self.step(communication_step)
        output_names = list(signature.get("outputs", []))
        fmi_names = [f"op{operation_code:02d}__out__{name}" for name in output_names]
        raw = self.get_enveloped(fmi_names)
        return {name: raw[f"op{operation_code:02d}__out__{name}"].to_dict() for name in output_names}

    def formula_signal_schema(self, formula_id: str) -> dict[str, Any]:
        """Return semantic metadata for one formula output."""
        formula = self.formulas[formula_id]
        op = int(formula["operation_code"])
        name = formula["output"]["name"]
        return self.signal_metadata(f"op{op:02d}__out__{name}")

    def evaluate_formula(
        self,
        formula_id: str,
        *,
        communication_step: float = 1e-3,
        use_config: bool = True,
        require_all_inputs: bool = False,
        reset_before: bool = False,
        **inputs: Any,
    ) -> float:
        try:
            formula = self.formulas[formula_id]
        except KeyError as exc:
            raise KeyError(f"Unknown formula id {formula_id!r} for {self.name}") from exc
        operation_code = int(formula["operation_code"])
        result = self.evaluate_operation(
            operation_code,
            communication_step=communication_step,
            use_config=use_config,
            require_all_inputs=require_all_inputs,
            reset_before=reset_before,
            **inputs,
        )
        return result[formula["output"]["name"]]
