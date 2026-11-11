"""Independent section calls; scheduling remains outside both FMUs."""
from pathlib import Path
from native_fmu import NativeFMU
ROOT=Path(__file__).resolve().parents[1]

def call_section(fmu, operation_code, inputs, output_count, dt=0.01):
    fmu.set(operation_code=operation_code,
            **{f"input_{i:03d}": v for i,v in enumerate(inputs,1)})
    fmu.step(dt)
    return fmu.get(*[f"output_{i:03d}" for i in range(1,output_count+1)])

with NativeFMU(ROOT/'fmus'/'WingMPET.fmu') as wing:
    print('Atmosphere:', call_section(wing,5,[1000,0,0,0,0.3,25],5))
with NativeFMU(ROOT/'fmus'/'MESA_NAV.fmu') as mesa:
    print('Identity:', call_section(mesa,1,[],3))
