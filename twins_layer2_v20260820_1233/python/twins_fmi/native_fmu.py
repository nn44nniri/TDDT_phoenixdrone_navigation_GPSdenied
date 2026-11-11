from __future__ import annotations

import ctypes
import json
import math
import platform
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote
import xml.etree.ElementTree as ET


class FMIError(RuntimeError):
    """Raised when an FMI call fails or the FMU is invalid."""


@dataclass(frozen=True)
class VariableInfo:
    name: str
    value_reference: int
    scalar_type: str
    causality: str
    variability: str
    initial: str | None
    start: Any = None
    unit: str | None = None
    display_unit: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class SignalEnvelope:
    """Semantic value transported through FMI plus side-band metadata.

    FMI 2.0 transports the numerical value itself. The additional fields are
    intentionally kept in the Python orchestration layer and the embedded
    ``resources/signal_schema.json`` so the native FMU remains standard FMI.
    """

    value: Any
    unit: str
    frame: str
    timestamp: float
    source: str
    quality: Any = None
    uncertainty: Any = None
    validity: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value, "unit": self.unit, "frame": self.frame,
            "timestamp": self.timestamp, "source": self.source,
            "quality": self.quality, "uncertainty": self.uncertainty,
            "validity": self.validity,
        }


class NativeFMU:
    """Small dependency-free FMI 2.0 Co-Simulation runtime for native FMUs.

    It manages one independent FMU instance, extracts the FMU to a private
    directory, loads its platform binary, and exposes typed batch set/get,
    lifecycle, reset, and stepping operations. Existing ``set(**kw)`` and
    ``get(*names)`` methods are retained for backward compatibility.
    """

    FMI_OK = 0
    FMI_WARNING = 1
    FMI_DISCARD = 2
    FMI_ERROR = 3
    FMI_FATAL = 4
    FMI_PENDING = 5
    FMI_CO_SIMULATION = 1

    def __init__(
        self,
        path: str | Path,
        *,
        instance_name: str | None = None,
        start_time: float = 0.0,
        stop_time: float | None = None,
        visible: bool = False,
        logging_on: bool = False,
        auto_initialize: bool = True,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        if not zipfile.is_zipfile(self.path):
            raise FMIError(f"Not a valid FMU ZIP archive: {self.path}")

        self.instance_name = instance_name or f"{self.path.stem}_{uuid.uuid4().hex[:10]}"
        self.tmp = Path(tempfile.mkdtemp(prefix=f"fmu_{self.path.stem}_"))
        self.component: int | None = None
        self.c = None  # backward-compatible alias
        self.t = float(start_time)
        self.initialized = False
        self.terminated = False
        self.closed = False

        try:
            with zipfile.ZipFile(self.path) as archive:
                archive.extractall(self.tmp)
            self._parse_model_description()
            self._load_binary()
            self._bind_functions()
            self._instantiate(visible=visible, logging_on=logging_on)
            if auto_initialize:
                self.initialize(start_time=start_time, stop_time=stop_time)
        except Exception:
            self.close(suppress_errors=True)
            raise

    def _parse_model_description(self) -> None:
        xml_path = self.tmp / "modelDescription.xml"
        if not xml_path.exists():
            raise FMIError("FMU has no modelDescription.xml")
        root = ET.parse(xml_path).getroot()
        if root.attrib.get("fmiVersion") != "2.0":
            raise FMIError(f"Unsupported FMI version: {root.attrib.get('fmiVersion')}")
        self.name = root.attrib["modelName"]
        self.guid = root.attrib["guid"]
        cs = root.find("CoSimulation")
        if cs is None:
            raise FMIError("Only FMI 2.0 Co-Simulation FMUs are supported")
        self.model_identifier = cs.attrib.get("modelIdentifier", self.name)

        self.variables: dict[str, VariableInfo] = {}
        self.vars: dict[str, int] = {}  # backward-compatible value-reference map
        model_variables = root.find("ModelVariables")
        if model_variables is None:
            raise FMIError("FMU has no ModelVariables")
        for scalar in model_variables.findall("ScalarVariable"):
            child = next(iter(scalar), None)
            if child is None:
                continue
            scalar_type = child.tag
            start: Any = child.attrib.get("start")
            if start is not None:
                if scalar_type == "Real":
                    start = float(start)
                elif scalar_type in {"Integer", "Enumeration"}:
                    start = int(start)
                elif scalar_type == "Boolean":
                    start = str(start).lower() == "true"
            info = VariableInfo(
                name=scalar.attrib["name"],
                value_reference=int(scalar.attrib["valueReference"]),
                scalar_type=scalar_type,
                causality=scalar.attrib.get("causality", "local"),
                variability=scalar.attrib.get("variability", "continuous"),
                initial=scalar.attrib.get("initial"),
                start=start,
                unit=child.attrib.get("unit"),
                display_unit=child.attrib.get("displayUnit"),
                description=scalar.attrib.get("description"),
            )
            self.variables[info.name] = info
            self.vars[info.name] = info.value_reference

        schema_path = self.tmp / "resources" / "signal_schema.json"
        if schema_path.exists():
            self.signal_schema = json.loads(schema_path.read_text())
        else:
            self.signal_schema = {"schema_version": "0", "fields": [], "signals": {}}
        self._signal_runtime_metadata: dict[str, dict[str, Any]] = {}

    def _platform_layout(self) -> tuple[str, str]:
        system = platform.system()
        machine = platform.machine().lower()
        is_64 = ctypes.sizeof(ctypes.c_void_p) == 8
        if system == "Windows":
            return ("win64" if is_64 else "win32", ".dll")
        if system == "Darwin":
            return ("darwin64" if is_64 else "darwin32", ".dylib")
        if system == "Linux":
            if machine in {"aarch64", "arm64"}:
                candidates = ["linuxaarch64", "linux64"]
            else:
                candidates = ["linux64" if is_64 else "linux32"]
            for candidate in candidates:
                if (self.tmp / "binaries" / candidate).exists():
                    return candidate, ".so"
            return candidates[0], ".so"
        raise FMIError(f"Unsupported host platform: {system}/{machine}")

    def _load_binary(self) -> None:
        platform_dir, extension = self._platform_layout()
        binary = self.tmp / "binaries" / platform_dir / f"{self.model_identifier}{extension}"
        if not binary.exists():
            available = [str(p.relative_to(self.tmp)) for p in (self.tmp / "binaries").rglob("*") if p.is_file()]
            raise FMIError(f"FMU binary unavailable for {platform_dir}: {binary}; available={available}")
        self.binary_path = binary
        self.lib = ctypes.CDLL(str(binary))

    def _bind_functions(self) -> None:
        c_void = ctypes.c_void_p
        c_uint_p = ctypes.POINTER(ctypes.c_uint)
        c_double_p = ctypes.POINTER(ctypes.c_double)
        c_int_p = ctypes.POINTER(ctypes.c_int)
        c_char_pp = ctypes.POINTER(ctypes.c_char_p)

        self.lib.fmi2Instantiate.restype = c_void
        self.lib.fmi2Instantiate.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p, c_void, ctypes.c_int, ctypes.c_int]
        for name, args in {
            "fmi2SetupExperiment": [c_void, ctypes.c_int, ctypes.c_double, ctypes.c_double, ctypes.c_int, ctypes.c_double],
            "fmi2EnterInitializationMode": [c_void],
            "fmi2ExitInitializationMode": [c_void],
            "fmi2Terminate": [c_void],
            "fmi2Reset": [c_void],
            "fmi2FreeInstance": [c_void],
            "fmi2DoStep": [c_void, ctypes.c_double, ctypes.c_double, ctypes.c_int],
            "fmi2SetReal": [c_void, c_uint_p, ctypes.c_size_t, c_double_p],
            "fmi2GetReal": [c_void, c_uint_p, ctypes.c_size_t, c_double_p],
            "fmi2SetInteger": [c_void, c_uint_p, ctypes.c_size_t, c_int_p],
            "fmi2GetInteger": [c_void, c_uint_p, ctypes.c_size_t, c_int_p],
            "fmi2SetBoolean": [c_void, c_uint_p, ctypes.c_size_t, c_int_p],
            "fmi2GetBoolean": [c_void, c_uint_p, ctypes.c_size_t, c_int_p],
            "fmi2SetString": [c_void, c_uint_p, ctypes.c_size_t, c_char_pp],
            "fmi2GetString": [c_void, c_uint_p, ctypes.c_size_t, c_char_pp],
        }.items():
            fn = getattr(self.lib, name)
            fn.argtypes = args
            if name != "fmi2FreeInstance":
                fn.restype = ctypes.c_int

    def _resource_uri(self) -> bytes:
        resource = (self.tmp / "resources").resolve().as_posix()
        return f"file:///{quote(resource.lstrip('/'))}/".encode()

    def _instantiate(self, *, visible: bool, logging_on: bool) -> None:
        component = self.lib.fmi2Instantiate(
            self.instance_name.encode(),
            self.FMI_CO_SIMULATION,
            self.guid.encode(),
            self._resource_uri(),
            None,
            int(visible),
            int(logging_on),
        )
        if not component:
            raise FMIError(f"fmi2Instantiate failed for {self.path.name}")
        self.component = component
        self.c = component

    def _check(self, status: int, function: str) -> None:
        if status >= self.FMI_ERROR:
            raise FMIError(f"{function} failed for {self.name} with FMI status {status}")

    def initialize(self, *, start_time: float = 0.0, stop_time: float | None = None) -> None:
        if self.closed or self.component is None:
            raise FMIError("FMU instance is closed")
        if self.initialized:
            return
        status = self.lib.fmi2SetupExperiment(self.component, 0, 0.0, float(start_time), int(stop_time is not None), float(stop_time or 0.0))
        self._check(status, "fmi2SetupExperiment")
        self._check(self.lib.fmi2EnterInitializationMode(self.component), "fmi2EnterInitializationMode")
        self._check(self.lib.fmi2ExitInitializationMode(self.component), "fmi2ExitInitializationMode")
        self.t = float(start_time)
        self.initialized = True
        self.terminated = False

    def _group_names(self, values: Mapping[str, Any]) -> dict[str, list[tuple[VariableInfo, Any]]]:
        grouped: dict[str, list[tuple[VariableInfo, Any]]] = {}
        for name, value in values.items():
            try:
                info = self.variables[name]
            except KeyError as exc:
                raise KeyError(f"Unknown FMI variable {name!r} in {self.name}") from exc
            grouped.setdefault(info.scalar_type, []).append((info, value))
        return grouped

    def set_values(self, values: Mapping[str, Any]) -> None:
        if not values:
            return
        for scalar_type, items in self._group_names(values).items():
            n = len(items)
            refs = (ctypes.c_uint * n)(*(info.value_reference for info, _ in items))
            if scalar_type == "Real":
                data = (ctypes.c_double * n)(*(float(value) for _, value in items))
                status = self.lib.fmi2SetReal(self.component, refs, n, data)
                self._check(status, "fmi2SetReal")
            elif scalar_type in {"Integer", "Enumeration"}:
                data = (ctypes.c_int * n)(*(int(value) for _, value in items))
                status = self.lib.fmi2SetInteger(self.component, refs, n, data)
                self._check(status, "fmi2SetInteger")
            elif scalar_type == "Boolean":
                data = (ctypes.c_int * n)(*(int(bool(value)) for _, value in items))
                status = self.lib.fmi2SetBoolean(self.component, refs, n, data)
                self._check(status, "fmi2SetBoolean")
            elif scalar_type == "String":
                encoded = [str(value).encode() for _, value in items]
                data = (ctypes.c_char_p * n)(*encoded)
                status = self.lib.fmi2SetString(self.component, refs, n, data)
                self._check(status, "fmi2SetString")
            else:
                raise FMIError(f"Unsupported scalar type: {scalar_type}")

    def get_values(self, names: Iterable[str]) -> dict[str, Any]:
        names = list(names)
        grouped: dict[str, list[VariableInfo]] = {}
        for name in names:
            if name not in self.variables:
                raise KeyError(f"Unknown FMI variable {name!r} in {self.name}")
            info = self.variables[name]
            grouped.setdefault(info.scalar_type, []).append(info)
        result: dict[str, Any] = {}
        for scalar_type, items in grouped.items():
            n = len(items)
            refs = (ctypes.c_uint * n)(*(info.value_reference for info in items))
            if scalar_type == "Real":
                data = (ctypes.c_double * n)()
                self._check(self.lib.fmi2GetReal(self.component, refs, n, data), "fmi2GetReal")
                result.update((info.name, float(data[i])) for i, info in enumerate(items))
            elif scalar_type in {"Integer", "Enumeration"}:
                data = (ctypes.c_int * n)()
                self._check(self.lib.fmi2GetInteger(self.component, refs, n, data), "fmi2GetInteger")
                result.update((info.name, int(data[i])) for i, info in enumerate(items))
            elif scalar_type == "Boolean":
                data = (ctypes.c_int * n)()
                self._check(self.lib.fmi2GetBoolean(self.component, refs, n, data), "fmi2GetBoolean")
                result.update((info.name, bool(data[i])) for i, info in enumerate(items))
            elif scalar_type == "String":
                data = (ctypes.c_char_p * n)()
                self._check(self.lib.fmi2GetString(self.component, refs, n, data), "fmi2GetString")
                result.update((info.name, data[i].decode() if data[i] else "") for i, info in enumerate(items))
            else:
                raise FMIError(f"Unsupported scalar type: {scalar_type}")
        return {name: result[name] for name in names}

    def signal_metadata(self, name: str) -> dict[str, Any]:
        """Return static semantic metadata embedded in the FMU."""
        if name not in self.variables:
            raise KeyError(f"Unknown FMI variable {name!r} in {self.name}")
        info = self.variables[name]
        meta = dict(self.signal_schema.get("signals", {}).get(name, {}))
        meta.setdefault("unit", info.unit or "1")
        meta.setdefault("frame", "SCALAR")
        meta.setdefault("source", "external" if info.causality == "input" else f"{self.name}.fmu")
        return meta

    def set_enveloped(self, values: Mapping[str, SignalEnvelope | Mapping[str, Any] | Any]) -> None:
        """Set FMI values while preserving provenance/quality side metadata.

        Units and frames are checked when supplied. Numeric unit conversion is
        deliberately not implicit: callers must pass the unit declared by FMI.
        """
        raw: dict[str, Any] = {}
        for name, item in values.items():
            meta = self.signal_metadata(name)
            if isinstance(item, SignalEnvelope):
                env = item.to_dict()
            elif isinstance(item, Mapping) and "value" in item:
                env = dict(item)
            else:
                env = {"value": item}
            declared_unit = meta.get("unit", self.variables[name].unit or "1")
            if env.get("unit") not in (None, declared_unit):
                raise ValueError(f"Unit mismatch for {name}: expected {declared_unit!r}, got {env.get('unit')!r}")
            declared_frame = meta.get("frame", "SCALAR")
            if env.get("frame") not in (None, declared_frame, "CONTEXT_DEPENDENT") and declared_frame != "CONTEXT_DEPENDENT":
                raise ValueError(f"Frame mismatch for {name}: expected {declared_frame!r}, got {env.get('frame')!r}")
            raw[name] = env["value"]
            self._signal_runtime_metadata[name] = {
                "timestamp": float(env.get("timestamp", self.t)),
                "source": env.get("source", meta.get("source", "external")),
                "quality": env.get("quality"),
                "uncertainty": env.get("uncertainty"),
                "validity": env.get("validity", "valid"),
            }
        self.set_values(raw)

    def get_enveloped(self, names: Iterable[str]) -> dict[str, SignalEnvelope]:
        """Read FMI values and attach unit/frame/time/provenance metadata."""
        names = list(names)
        raw = self.get_values(names)
        result: dict[str, SignalEnvelope] = {}
        for name in names:
            meta = self.signal_metadata(name)
            runtime = self._signal_runtime_metadata.get(name, {})
            value = raw[name]
            validity = runtime.get("validity")
            if validity is None:
                if isinstance(value, (int, float)):
                    validity = "valid" if math.isfinite(float(value)) else "invalid"
                else:
                    validity = "valid"
            result[name] = SignalEnvelope(
                value=value,
                unit=meta.get("unit", self.variables[name].unit or "1"),
                frame=meta.get("frame", "SCALAR"),
                timestamp=float(runtime.get("timestamp", self.t)),
                source=runtime.get("source", meta.get("source", f"{self.name}.fmu")),
                quality=runtime.get("quality", meta.get("quality")),
                uncertainty=runtime.get("uncertainty", meta.get("uncertainty")),
                validity=validity,
            )
        return result

    def set(self, **values: Any) -> None:
        self.set_values(values)

    def get(self, *names: str) -> dict[str, Any]:
        return self.get_values(names)

    def step(self, dt: float, *, no_set_fmu_state_prior: bool = False) -> float:
        if not self.initialized:
            raise FMIError("FMU must be initialized before stepping")
        if dt <= 0:
            raise ValueError("communication step must be positive")
        status = self.lib.fmi2DoStep(self.component, float(self.t), float(dt), int(no_set_fmu_state_prior))
        self._check(status, "fmi2DoStep")
        self.t += float(dt)
        return self.t

    def reset(self, *, start_time: float = 0.0, stop_time: float | None = None, reinitialize: bool = True) -> None:
        if self.component is None:
            raise FMIError("FMU instance is closed")
        self._check(self.lib.fmi2Reset(self.component), "fmi2Reset")
        self.initialized = False
        self.terminated = False
        self.t = float(start_time)
        if reinitialize:
            self.initialize(start_time=start_time, stop_time=stop_time)

    def terminate(self) -> None:
        if self.component is not None and self.initialized and not self.terminated:
            self._check(self.lib.fmi2Terminate(self.component), "fmi2Terminate")
            self.terminated = True
            self.initialized = False

    def list_variables(self, *, causality: str | None = None, scalar_type: str | None = None) -> list[VariableInfo]:
        values = self.variables.values()
        return sorted(
            [v for v in values if (causality is None or v.causality == causality) and (scalar_type is None or v.scalar_type == scalar_type)],
            key=lambda v: v.name,
        )

    def close(self, *, suppress_errors: bool = False) -> None:
        if self.closed:
            return
        try:
            if self.component is not None:
                try:
                    self.terminate()
                finally:
                    self.lib.fmi2FreeInstance(self.component)
                    self.component = None
                    self.c = None
        except Exception:
            if not suppress_errors:
                raise
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)
            self.closed = True

    def __enter__(self) -> "NativeFMU":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close(suppress_errors=exc is not None)
