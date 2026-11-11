#!/usr/bin/env python3
"""Standards-oriented FMI 2.0 Co-Simulation ctypes client for MicroMetTwin."""
from __future__ import annotations
import ctypes
import math
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Mapping
from urllib.parse import quote
from fmi_variables import INPUT_VR, OUTPUT_NAMES, OUTPUT_START, OUTPUT_VR

FMI_OK = 0
FMI_WARNING = 1

class MicroMetFMU:
    def __init__(self, fmu_path: str | Path, start_time: float = 0.0):
        self.fmu_path = Path(fmu_path).resolve()
        if not self.fmu_path.exists():
            raise FileNotFoundError(self.fmu_path)
        self._tmp = tempfile.TemporaryDirectory(prefix="micromet_fmu_")
        with zipfile.ZipFile(self.fmu_path) as zf:
            zf.extractall(self._tmp.name)
        root = Path(self._tmp.name)
        md = ET.parse(root / "modelDescription.xml").getroot()
        self.guid = md.attrib["guid"]
        model_id = md.find("CoSimulation").attrib["modelIdentifier"]
        lib_path = root / "binaries" / "linux64" / f"{model_id}.so"
        if not lib_path.exists():
            raise RuntimeError(f"Native linux64 binary not found in FMU: {lib_path}")
        self.lib = ctypes.CDLL(str(lib_path))
        self._bind()
        resource_uri = "file://" + quote(str((root / "resources").resolve()))
        self.component = self.lib.fmi2Instantiate(
            b"micromet_instance", 1, self.guid.encode(), resource_uri.encode(), None, 0, 0
        )
        if not self.component:
            raise RuntimeError("fmi2Instantiate returned NULL (GUID/type validation failed)")
        self._check(self.lib.fmi2SetupExperiment(self.component, 0, 0.0, start_time, 0, 0.0), "fmi2SetupExperiment")
        self._check(self.lib.fmi2EnterInitializationMode(self.component), "fmi2EnterInitializationMode")
        self._check(self.lib.fmi2ExitInitializationMode(self.component), "fmi2ExitInitializationMode")
        self.time = float(start_time)

    def _bind(self) -> None:
        lib = self.lib
        lib.fmi2Instantiate.restype = ctypes.c_void_p
        lib.fmi2Instantiate.argtypes = [ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_char_p,ctypes.c_void_p,ctypes.c_int,ctypes.c_int]
        for name in ("fmi2SetupExperiment",):
            getattr(lib,name).restype=ctypes.c_int
        lib.fmi2SetupExperiment.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_double,ctypes.c_double,ctypes.c_int,ctypes.c_double]
        for name in ("fmi2EnterInitializationMode","fmi2ExitInitializationMode","fmi2Terminate","fmi2Reset"):
            fn=getattr(lib,name);fn.restype=ctypes.c_int;fn.argtypes=[ctypes.c_void_p]
        lib.fmi2SetReal.restype=ctypes.c_int;lib.fmi2SetReal.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint),ctypes.c_size_t,ctypes.POINTER(ctypes.c_double)]
        lib.fmi2GetReal.restype=ctypes.c_int;lib.fmi2GetReal.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint),ctypes.c_size_t,ctypes.POINTER(ctypes.c_double)]
        lib.fmi2DoStep.restype=ctypes.c_int;lib.fmi2DoStep.argtypes=[ctypes.c_void_p,ctypes.c_double,ctypes.c_double,ctypes.c_int]
        lib.fmi2FreeInstance.argtypes=[ctypes.c_void_p]

    @staticmethod
    def _check(status: int, operation: str) -> None:
        if status not in (FMI_OK, FMI_WARNING):
            raise RuntimeError(f"{operation} failed with FMI status {status}")

    def set_real_dict(self, values: Mapping[str, float]) -> None:
        unknown=[k for k in values if k not in INPUT_VR]
        if unknown:
            raise KeyError(f"Unknown FMI input variables: {unknown}")
        items=[(INPUT_VR[k],float(v)) for k,v in values.items()]
        if not items:
            raise ValueError("No FMI input variables were supplied")
        refs=(ctypes.c_uint*len(items))(*[x[0] for x in items]);vals=(ctypes.c_double*len(items))(*[x[1] for x in items])
        self._check(self.lib.fmi2SetReal(self.component,refs,len(items),vals),"fmi2SetReal")

    def step(self, values: Mapping[str, float], step_seconds: float = 1.0) -> Dict[str, float]:
        if step_seconds <= 0:
            raise ValueError("step_seconds must be positive")
        self.set_real_dict(values)
        self._check(self.lib.fmi2DoStep(self.component,self.time,step_seconds,1),"fmi2DoStep")
        self.time += step_seconds
        refs=(ctypes.c_uint*len(OUTPUT_NAMES))(*[OUTPUT_VR[n] for n in OUTPUT_NAMES])
        got=(ctypes.c_double*len(OUTPUT_NAMES))()
        self._check(self.lib.fmi2GetReal(self.component,refs,len(OUTPUT_NAMES),got),"fmi2GetReal")
        out=dict(zip(OUTPUT_NAMES,map(float,got)))
        out["wind_direction_background_deg"]=math.degrees(out["wind_direction_background_rad"])%360.0
        out["wind_direction_terrain_deg"]=math.degrees(out["wind_direction_terrain_rad"])%360.0
        out["slope_deg"]=math.degrees(out["slope_rad"])
        out["aspect_deg"]=math.degrees(out["aspect_rad"])%360.0
        return out

    def close(self) -> None:
        if getattr(self,"component",None):
            try:self.lib.fmi2Terminate(self.component)
            finally:self.lib.fmi2FreeInstance(self.component)
            self.component=None
        if getattr(self,"_tmp",None):self._tmp.cleanup()
    def __enter__(self): return self
    def __exit__(self,exc_type,exc,tb): self.close()
