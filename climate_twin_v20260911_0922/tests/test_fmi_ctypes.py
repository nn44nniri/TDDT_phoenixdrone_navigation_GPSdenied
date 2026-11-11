"""Updated legacy ctypes smoke test: use current FMU-declared VRs, not 2026-08 raw offsets.

This test is independent of Stage8 provenance checks and does not certify
real-world climate or the external ArduPilot/JSBSim environment adapter.
"""
from __future__ import annotations

import ctypes
import pathlib
import tempfile
import zipfile
import xml.etree.ElementTree as ET

from fmi_variables import INPUT_VR, OUTPUT_VR

root=pathlib.Path(__file__).resolve().parents[1]
# Load the exact distributed FMU binary, not a potentially stale CMake build.
_tmp=tempfile.TemporaryDirectory()
with zipfile.ZipFile(root/'fmu/MicroMetTwin.fmu') as zf:
    zf.extract('binaries/linux64/MicroMet.so',_tmp.name)
    xml=ET.fromstring(zf.read('modelDescription.xml'))
lib=ctypes.CDLL(str(pathlib.Path(_tmp.name)/'binaries/linux64/MicroMet.so'))
lib.fmi2Instantiate.restype=ctypes.c_void_p
lib.fmi2Instantiate.argtypes=[ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_char_p,ctypes.c_void_p,ctypes.c_int,ctypes.c_int]
guid=xml.attrib['guid'].encode()
assert lib.fmi2Instantiate(b'bad',1,b'{invalid-guid}',b'',None,0,0) is None
c=lib.fmi2Instantiate(b'ctypes_stage8',1,guid,b'',None,0,0)
assert c
lib.fmi2SetReal.restype=ctypes.c_int
lib.fmi2SetReal.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint),ctypes.c_size_t,ctypes.POINTER(ctypes.c_double)]
lib.fmi2GetReal.restype=ctypes.c_int
lib.fmi2GetReal.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint),ctypes.c_size_t,ctypes.POINTER(ctypes.c_double)]
inputs={
 'latitude':40.0,'longitude':-105.0,'elevation':1800.0,
 'source0_enabled':1.0,'source0_latitude':40.0,'source0_longitude':-105.0,
 'source0_elevation':1800.0,'source0_temperature':20.0,
 'source0_relative_humidity':50.0,'source0_wind_speed':3.0,
 'source0_wind_direction':270.0,'source0_precipitation':0.0,
 'source0_surface_pressure_hpa':820.0,
}
refs=(ctypes.c_uint*len(inputs))(*[INPUT_VR[k] for k in inputs])
vals=(ctypes.c_double*len(inputs))(*inputs.values())
assert lib.fmi2SetReal(c,refs,len(inputs),vals)==0
outputs=['temperature_local','relative_humidity_local','wind_speed_terrain','surface_pressure_pa','formula_valid']
vr=(ctypes.c_uint*len(outputs))(*[OUTPUT_VR[k] for k in outputs]); out=(ctypes.c_double*len(outputs))()
assert lib.fmi2GetReal(c,vr,len(outputs),out)==0
print('current VR temperature, RH, wind, pressure, valid =',list(out))
assert 0<out[0]<35 and 0<=out[1]<=100 and out[2]>=0 and out[3]>50000 and out[4]==1.0
lib.fmi2FreeInstance.argtypes=[ctypes.c_void_p];lib.fmi2FreeInstance(c)
_tmp.cleanup()
