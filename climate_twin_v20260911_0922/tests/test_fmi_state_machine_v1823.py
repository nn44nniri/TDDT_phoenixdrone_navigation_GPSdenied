import ctypes, tempfile, zipfile, xml.etree.ElementTree as ET
from pathlib import Path
fmu=Path('fmu/MicroMetTwin.fmu')
with tempfile.TemporaryDirectory() as d:
    with zipfile.ZipFile(fmu) as z:z.extractall(d)
    root=ET.parse(Path(d)/'modelDescription.xml').getroot();guid=root.attrib['guid'].encode()
    lib=ctypes.CDLL(str(Path(d)/'binaries/linux64/MicroMet.so'))
    lib.fmi2Instantiate.restype=ctypes.c_void_p
    lib.fmi2Instantiate.argtypes=[ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_char_p,ctypes.c_void_p,ctypes.c_int,ctypes.c_int]
    bad=lib.fmi2Instantiate(b'bad',1,b'{wrong}',b'',None,0,0)
    assert not bad
    c=lib.fmi2Instantiate(b'ok',1,guid,b'',None,0,0); assert c
    for n in ['fmi2SetupExperiment','fmi2EnterInitializationMode','fmi2ExitInitializationMode','fmi2Terminate']:
        getattr(lib,n).restype=ctypes.c_int
    lib.fmi2SetupExperiment.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_double,ctypes.c_double,ctypes.c_int,ctypes.c_double]
    for n in ['fmi2EnterInitializationMode','fmi2ExitInitializationMode','fmi2Terminate']:
        getattr(lib,n).argtypes=[ctypes.c_void_p]
    lib.fmi2DoStep.restype=ctypes.c_int;lib.fmi2DoStep.argtypes=[ctypes.c_void_p,ctypes.c_double,ctypes.c_double,ctypes.c_int]
    lib.fmi2FreeInstance.argtypes=[ctypes.c_void_p]
    assert lib.fmi2DoStep(c,0,1,1)==3
    assert lib.fmi2SetupExperiment(c,0,0,0,0,0)==0
    assert lib.fmi2EnterInitializationMode(c)==0
    assert lib.fmi2ExitInitializationMode(c)==0
    assert lib.fmi2DoStep(c,0,1,1) in (0,1)
    assert lib.fmi2Terminate(c)==0
    lib.fmi2FreeInstance(c)
print('FMI 2.0 CS lifecycle and GUID checks passed')
