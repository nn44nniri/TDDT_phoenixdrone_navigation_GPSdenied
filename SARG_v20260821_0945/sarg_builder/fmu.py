from __future__ import annotations

"""Legacy compatibility module.

The SARG execution path does not import this module. FMU access is delegated
exclusively to the independent twins_layer2 library through twins_library.py.
It is retained only to avoid breaking third-party imports from older releases.
"""


import inspect
import json
import shutil
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


class FormulaFMU:
    """Minimal FMI 2.0 Co-Simulation wrapper using named formula variables.

    Some producer toolchains emit output variables with both
    ``initial=\"calculated\"`` and a ``start`` value. FMI 2.0 does not allow
    that combination and recent FMPy versions reject the descriptor during
    validation. The runtime can still load these FMUs safely because the start
    value of a calculated output is not used as an initialization input.

    This wrapper first asks FMPy to parse without schema validation. If the
    installed FMPy version does not expose that option, it parses a temporary,
    extracted descriptor where only the forbidden ``start`` attributes are
    removed. The source FMU archive is never modified.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        self.tmp = Path(tempfile.mkdtemp(prefix=f"sarg_{self.path.stem}_"))
        with zipfile.ZipFile(self.path) as zf:
            zf.extractall(self.tmp)
        self.catalog = self._load_catalog()
        self._slave = None
        self._md = None
        self._vr: dict[str, int] = {}
        self._descriptor_only = False
        self._start_values: dict[str, float] = {}
        self.descriptor_compatibility: dict[str, Any] = {
            "validation_disabled": False,
            "sanitized_calculated_output_starts": 0,
            "runtime_mode": "native",
            "missing_optional_fmi_symbols": [],
        }

    def _load_catalog(self) -> dict[str, Any]:
        p = self.tmp / "resources" / "formula_catalog.json"
        if not p.exists():
            return {"operations": []}
        return json.loads(p.read_text(encoding="utf-8"))

    @staticmethod
    def _remove_invalid_calculated_starts(xml_path: Path) -> int:
        """Remove forbidden start values from initial='calculated' variables.

        The change is applied only to the extracted temporary descriptor.
        Namespaces and all unrelated attributes/elements are preserved by
        ElementTree.
        """
        tree = ET.parse(xml_path)
        root = tree.getroot()
        changed = 0
        for scalar in root.iter():
            if scalar.tag.rsplit("}", 1)[-1] != "ScalarVariable":
                continue
            if scalar.attrib.get("initial") != "calculated":
                continue
            for typed_value in list(scalar):
                if "start" in typed_value.attrib:
                    del typed_value.attrib["start"]
                    changed += 1
        if changed:
            tree.write(xml_path, encoding="utf-8", xml_declaration=True)
        return changed

    def _read_model_description(self):
        from fmpy import read_model_description

        # FMPy >= 0.3 supports validate=False. Use signature inspection so this
        # remains compatible with older releases and alternate wrappers.
        try:
            params = inspect.signature(read_model_description).parameters
        except (TypeError, ValueError):
            params = {}

        if "validate" in params:
            self.descriptor_compatibility["validation_disabled"] = True
            return read_model_description(str(self.path), validate=False)

        # Older FMPy: sanitize the already extracted descriptor and parse it.
        xml_path = self.tmp / "modelDescription.xml"
        count = self._remove_invalid_calculated_starts(xml_path)
        self.descriptor_compatibility["sanitized_calculated_output_starts"] = count
        return read_model_description(str(xml_path))

    def __enter__(self):
        from fmpy.fmi2 import FMU2Slave

        self._md = self._read_model_description()
        cs = self._md.coSimulation
        if cs is None:
            raise RuntimeError(f"{self.path.name} is not an FMI 2.0 Co-Simulation FMU")
        self._vr = {v.name: v.valueReference for v in self._md.modelVariables}
        self._start_values = {}
        for v in self._md.modelVariables:
            value = getattr(v, "start", None)
            if value is not None:
                try:
                    self._start_values[v.name] = float(value)
                except (TypeError, ValueError):
                    pass
        try:
            self._slave = FMU2Slave(
                guid=self._md.guid,
                unzipDirectory=str(self.tmp),
                modelIdentifier=cs.modelIdentifier,
                instanceName=f"sarg_{self.path.stem}",
            )
            self._slave.instantiate()
            self._slave.setupExperiment(startTime=0.0)
            self._slave.enterInitializationMode()
            self._slave.exitInitializationMode()
        except AttributeError as exc:
            # Some FMU exporters advertise FMI-state support but omit one or
            # more optional state/serialization symbols. FMPy then fails while
            # constructing FMU2Slave, even though ordinary Co-Simulation calls
            # may be present. SARG only needs an interface/identity probe here,
            # so accept *only* this known optional-symbol defect and use the
            # parsed descriptor/catalog. Missing mandatory FMI functions remain
            # fatal and are re-raised.
            text = str(exc)
            optional = (
                "fmi2GetFMUstate", "fmi2SetFMUstate", "fmi2FreeFMUstate",
                "fmi2SerializedFMUstateSize", "fmi2SerializeFMUstate",
                "fmi2DeSerializeFMUstate",
            )
            missing = [name for name in optional if name in text]
            if not missing:
                raise
            self._slave = None
            self._descriptor_only = True
            self.descriptor_compatibility["runtime_mode"] = "descriptor_catalog_probe"
            self.descriptor_compatibility["missing_optional_fmi_symbols"] = missing
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._slave:
                try:
                    self._slave.terminate()
                finally:
                    self._slave.freeInstance()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def operation_signature(self, op: int) -> tuple[list[str], list[str]]:
        prefix = f"op{op:02d}__"
        ins = sorted(n for n in self._vr if n.startswith(prefix + "in__"))
        outs = sorted(n for n in self._vr if n.startswith(prefix + "out__"))
        return ins, outs

    def evaluate(self, op: int, values: dict[str, float], step_size: float = 0.001) -> dict[str, float]:
        if not self._slave and not self._descriptor_only:
            raise RuntimeError("FMU must be used as a context manager")
        ins, outs = self.operation_signature(op)
        selector = "formula_operation_code" if "formula_operation_code" in self._vr else "operation_code"
        if selector not in self._vr:
            raise KeyError("FMU has neither 'formula_operation_code' nor 'operation_code'")
        if self._descriptor_only:
            # Identity/interface probing fallback. Return descriptor start values
            # for declared outputs; if no start is available, use zero. This mode
            # is intentionally not used for flight simulation.
            return {
                n.split("__out__", 1)[1]: self._start_values.get(n, 0.0)
                for n in outs
            }
        self._slave.setReal([self._vr[selector]], [float(op)])
        names, vals = [], []
        for full in ins:
            logical = full.split("__in__", 1)[1]
            names.append(full)
            vals.append(float(values.get(logical, sample_value(logical))))
        if names:
            self._slave.setReal([self._vr[n] for n in names], vals)
        self._slave.doStep(currentCommunicationPoint=0.0, communicationStepSize=step_size)
        if not outs:
            return {}
        result = self._slave.getReal([self._vr[n] for n in outs])
        return {n.split("__out__", 1)[1]: float(v) for n, v in zip(outs, result)}

    def find_operation(self, required_outputs: set[str]) -> int | None:
        for op in range(1, 100):
            _, outs = self.operation_signature(op)
            logical = {x.split("__out__", 1)[1] for x in outs}
            if required_outputs <= logical:
                return op
        return None


def sample_value(name: str) -> float:
    n = name.lower()
    table = {
        "altitude": 150.0,
        "msl": 150.0,
        "agl": 100.0,
        "terrain": 50.0,
        "airspeed": 13.0,
        "speed": 13.0,
        "wind": 3.0,
        "temperature": 288.15,
        "pressure": 101325.0,
        "density": 1.225,
        "humidity": 0.4,
        "mass": 0.65,
        "wing_area": 0.12,
        "soc": 0.9,
        "voltage": 11.1,
        "current": 5.0,
        "power": 55.0,
        "energy": 1.0,
        "time": 1.0,
        "distance": 10.0,
        "bank": 10.0,
        "angle": 0.1,
        "heading": 0.0,
        "variance": 1.0,
        "covariance": 1.0,
        "residual": 0.0,
        "dt": 0.01,
    }
    for key, val in table.items():
        if key in n:
            return val
    return 1.0
