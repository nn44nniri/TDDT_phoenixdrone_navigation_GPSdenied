from pathlib import Path
import math, sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from twins_fmi import FormulaFMU
CFG=ROOT/'config'/'phoenix_drone_v3.yaml'

def finite(d):
    return all(math.isfinite(float(v)) for v in d.values())

# Representative six-stage points. These are deterministic verification states, not flight validation.
stages = [
    ('vertical_takeoff', 0.0, 0.72, 0.72),
    ('hover',            0.2, 0.62, 0.62),
    ('transition_forward', 7.0, 0.70, 0.70),
    ('cruise',          13.0, 0.45, 0.45),
    ('transition_back',  7.0, 0.62, 0.62),
    ('vertical_landing', 0.1, 0.55, 0.55),
]
with FormulaFMU(ROOT/'fmus'/'WingMPET.fmu',config=CFG) as f:
    for stage,va,ul,ur in stages:
        out=f.evaluate_operation(27,dt=.01,pn=0,pe=0,pd=-100,vn=va,ve=0,vd=0,
            qw=1,qx=0,qy=0,qz=0,p_rate=0,q_rate=0,r_rate=0,
            omega_left=560,omega_right=560,soc=.9,left_motor_cmd=ul,right_motor_cmd=ur,
            left_elevon_rad=0,right_elevon_rad=0,wind_n=0,wind_e=0,wind_d=0,air_density=1.225)
        assert finite(out), stage
        assert 0 <= out['lambda_w'] <= 1, stage
        assert 0 <= out['next_omega_left'] <= 800 and 0 <= out['next_omega_right'] <= 800, stage
        assert 0 <= out['next_soc'] <= 1, stage
        assert 0 <= out['predicted_power'] <= 350.0 + 1e-9, stage
    h=f.evaluate_operation(30,airspeed=0,sideslip=0,bank_angle=0,flightpath_angle=0,
        p_rate=0,q_rate=0,r_rate=0,load_factor=1,total_thrust=6.5,mass=.65,
        left_motor_cmd=.65,right_motor_cmd=.65,left_elevon_rad=0,right_elevon_rad=0)
    c=f.evaluate_operation(30,airspeed=13,sideslip=0,bank_angle=0,flightpath_angle=0,
        p_rate=0,q_rate=0,r_rate=0,load_factor=1,total_thrust=4,mass=.65,
        left_motor_cmd=.45,right_motor_cmd=.45,left_elevon_rad=0,right_elevon_rad=0)
    assert h['lambda_w'] == 0.0 and c['lambda_w'] > .99

with FormulaFMU(ROOT/'fmus'/'MESA_NAV.fmu',config=CFG) as f:
    fresh=f.evaluate_operation(23,wind_obs_n=3,wind_obs_e=1,wind_obs_d=0,sample_age_s=.02,max_age_s=.5,
        variance_n=.25,variance_e=.25,variance_d=.25,process_variance_n=.02,process_variance_e=.02,process_variance_d=.02,
        prior_wind_n=2.5,prior_wind_e=.8,prior_wind_d=0,bias_n=0,bias_e=0,bias_d=0,
        prior_variance_n=1,prior_variance_e=1,prior_variance_d=1,nis_gate=18)
    stale=f.evaluate_operation(23,wind_obs_n=3,wind_obs_e=1,wind_obs_d=0,sample_age_s=2,max_age_s=.5,
        variance_n=.25,variance_e=.25,variance_d=.25,process_variance_n=.02,process_variance_e=.02,process_variance_d=.02,
        prior_wind_n=2.5,prior_wind_e=.8,prior_wind_d=0,bias_n=0,bias_e=0,bias_d=0,
        prior_variance_n=1,prior_variance_e=1,prior_variance_d=1,nis_gate=18)
    assert finite(fresh) and fresh['update_valid']==1
    assert stale['update_valid']==0 and stale['updated_wind_n']==2.5
    hover=f.evaluate_operation(24,airspeed=0,groundspeed=0,bank_angle=0,sideslip=0,load_factor=1,p_rate=0,q_rate=0,r_rate=0,
        stall_speed=9.1,command_min_speed=12,max_airspeed=18,max_load_factor=2.5,max_sideslip=math.radians(12),min_lambda=0)
    cruise=f.evaluate_operation(24,airspeed=13,groundspeed=13,bank_angle=0,sideslip=0,load_factor=1,p_rate=0,q_rate=0,r_rate=0,
        stall_speed=9.1,command_min_speed=12,max_airspeed=18,max_load_factor=2.5,max_sideslip=math.radians(12),min_lambda=0)
    assert hover['lambda_w']==0 and hover['mechanical_constraints_valid']==1
    assert cruise['lambda_w']>.99 and cruise['mechanical_constraints_valid']==1

print('PHOENIX_V3_VTOL_FORMALISM_TESTS_PASSED')
