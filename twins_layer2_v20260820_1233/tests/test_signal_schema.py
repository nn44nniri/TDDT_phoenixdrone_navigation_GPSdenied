from pathlib import Path
import json, math, sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from twins_fmi import FormulaFMU, SignalEnvelope

for model in ['MESA_NAV','WingMPET']:
    with FormulaFMU(ROOT/'fmus'/f'{model}.fmu', config=ROOT/'config'/'phoenix_drone_v3.yaml') as f:
        total=len(f.variables)
        assert total > 0
        assert all(v.unit for v in f.variables.values())
        assert set(f.signal_schema['fields'])=={'value','unit','frame','timestamp','source','quality','uncertainty','validity'}
        assert len(f.signal_schema['signals'])==total
        f.set_enveloped({'dt': SignalEnvelope(0.01,'s','SCALAR',f.t,'test',quality=1.0,uncertainty=0.0,validity='valid')})
        e=f.get_enveloped(['dt'])['dt']
        assert e.unit=='s' and e.frame=='SCALAR' and e.source=='test'

with FormulaFMU(ROOT/'fmus'/'WingMPET.fmu', config=ROOT/'config'/'phoenix_drone_v3.yaml') as f:
    out=f.evaluate_operation_enveloped(5, altitude=1000, external_temperature=0, external_pressure=0, external_density=0, relative_humidity=.45, airspeed=13)
    assert out['air_density']['unit']=='kg/m3'
    assert out['air_density']['frame']=='SCALAR'
    assert out['air_density']['validity']=='valid'
    assert math.isfinite(out['air_density']['value'])

with FormulaFMU(ROOT/'fmus'/'MESA_NAV.fmu', config=ROOT/'config'/'phoenix_drone_v3.yaml') as f:
    out=f.evaluate_operation_enveloped(5, dt=.01)
    assert out['position_n']['unit']=='m' and out['position_n']['frame']=='NED'
print('ALL_SIGNAL_SCHEMA_TESTS_PASSED')
