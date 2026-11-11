from pathlib import Path
import math
import pytest
from twins_fmi import FormulaFMU


def test_compiled_wing_fmu_distinguishes_broadside_translation_from_chord_flow():
    root=Path(__file__).resolve().parents[2]/'twins_layer2_v20260820_1233'
    with FormulaFMU(root/'fmus/WingMPET.fmu',config=root/'config/phoenix_drone_v3.yaml') as f:
        args=dict(dt=.01,pn=0.,pe=0.,pd=-10.,vn=13.,ve=0.,vd=0.,
            p_rate=0.,q_rate=0.,r_rate=0.,omega_left=700.,omega_right=700.,soc=.9,
            left_motor_cmd=.875,right_motor_cmd=.875,left_elevon_rad=0.,right_elevon_rad=0.,
            wind_n=0.,wind_e=0.,wind_d=0.,air_density=1.225)
        upright=f.evaluate_operation(27,**args,qw=math.sqrt(.5),qx=0.,qy=math.sqrt(.5),qz=0.)
        wingborne=f.evaluate_operation(27,**args,qw=1.,qx=0.,qy=0.,qz=0.)
        assert upright['airspeed']==pytest.approx(13.)
        assert upright['lambda_w']==0.
        assert wingborne['lambda_w']==1.
        assert all(math.isfinite(v) for v in upright.values())
