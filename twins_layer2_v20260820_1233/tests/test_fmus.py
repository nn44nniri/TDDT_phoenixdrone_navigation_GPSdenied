from pathlib import Path
import sys, math
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from native_fmu import NativeFMU

def section(fmu, op, vals, nout):
    fmu.set(operation_code=op, **{f'input_{i:03d}':v for i,v in enumerate(vals,1)})
    fmu.step(0.01)
    return fmu.get(*[f'output_{i:03d}' for i in range(1,nout+1)])

with NativeFMU(ROOT/'fmus'/'WingMPET.fmu') as f:
    a=section(f,5,[1000,0,0,0,.3,25],5)
    assert a['output_003']>0.9 and a['output_003']<1.3
    e=section(f,31,[.1,1.225,8.0,6.0,.9,22.2,8.0,1.0],9)
    assert e['output_005']>=0 and 0 <= e['output_007'] <= 1
    f.set(operation_code=0,dt=.1,vtol_enable=1,vtol_pn=0,vtol_pe=0,vtol_pd=-100,vtol_vn=8,vtol_ve=0,vtol_vd=0,vtol_qw=1,vtol_qx=0,vtol_qy=0,vtol_qz=0,vtol_p_rate=0,vtol_q_rate=0,vtol_r_rate=0,left_motor_cmd=.6,right_motor_cmd=.6,left_elevon_rad=0,right_elevon_rad=0,vtol_wind_n=0,vtol_wind_e=0,vtol_wind_d=0,vtol_air_density=1.225,vtol_soc=.9,vtol_motor_omega_left=500,vtol_motor_omega_right=500)
    f.step(.1); q=f.get('vtol_Va','vtol_predicted_power','vtol_next_soc','vtol_feasible'); assert q['vtol_Va']>=0 and q['vtol_predicted_power']>=0
with NativeFMU(ROOT/'fmus'/'MESA_NAV.fmu') as f:
    ident=section(f,1,[],3); assert ident['output_001']>=4 and ident['output_002']==24 and ident['output_003']==27
    rls=section(f,16,[0,1,2,.99],3); assert abs(rls['output_001'])>0
    f.set(operation_code=0,dt=.01,imu_gz=.01,imu_az=-9.80665,pressure_pa=101325,pitot_q=100,qw=1,dem_h=0)
    f.step(.01); q=f.get('qw','rN','airspeed','obs_score'); assert math.isfinite(q['qw']) and q['airspeed']>=0
print('ALL_FMU_TESTS_PASSED')
