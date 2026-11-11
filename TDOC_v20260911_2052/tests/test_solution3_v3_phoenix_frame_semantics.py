import math, os
from pathlib import Path

from tddt_tdoc.phoenix_frames import fallback_attitude, body_x_ned, vertical_alignment_ok


def _root():
    return Path(os.environ.get("TDDT_INTEGRATED_ROOT", Path(__file__).resolve().parents[2]))


def test_vertical_phases_map_body_x_to_negative_down_ned():
    for phase in ("on_ground","vertical_takeoff","hover","vertical_landing","touchdown"):
        a=fallback_attitude(phase,yaw_rad=0.37,kinematic_pitch_rad=0.0)
        assert math.isclose(math.degrees(a["pitch_rad"]),90.0,abs_tol=1e-10)
        assert vertical_alignment_ok(a["qw"],a["qx"],a["qy"],a["qz"],tol_deg=1e-6)
        bx=body_x_ned(a["qw"],a["qx"],a["qy"],a["qz"])
        assert bx[2] < -0.999999


def test_wingborne_fallback_does_not_force_vertical_attitude():
    p=math.radians(7.0)
    a=fallback_attitude("cruise",yaw_rad=0.2,kinematic_pitch_rad=p)
    assert math.isclose(a["pitch_rad"],p,abs_tol=1e-12)
    assert a["basis"]=="kinematic_pitch_fallback"


def test_forward_transition_uses_realized_kinematic_pitch_after_hover_departure():
    p=math.radians(31.0)
    a=fallback_attitude("transition_forward",yaw_rad=-0.1,kinematic_pitch_rad=p)
    assert math.isclose(a["pitch_rad"],p,abs_tol=1e-12)
    assert a["basis"]=="kinematic_pitch_fallback"


def test_wingmpet_op27_uses_canonical_frd_thrust_axis_for_tailsitter():
    root=_root(); twins=root/"twins_layer2_v20260820_1233"
    import sys
    sys.path.insert(0,str(twins/"python"))
    from twins_fmi import FormulaFMU,DroneConfig
    f=FormulaFMU(twins/"fmus/WingMPET.fmu",config=DroneConfig(twins/"config/phoenix_drone_v3.yaml"))
    try:
        def run(q):
            return f.evaluate_operation(27,dt=.05,pn=0,pe=0,pd=0,vn=0,ve=0,vd=0,
                qw=q[0],qx=q[1],qy=q[2],qz=q[3],p_rate=0,q_rate=0,r_rate=0,
                omega_left=650,omega_right=650,soc=1,left_motor_cmd=.82,right_motor_cmd=.82,
                left_elevon_rad=0,right_elevon_rad=0,wind_n=0,wind_e=0,wind_d=0,air_density=1.225)
        identity=run((1,0,0,0))
        c=math.sqrt(.5); vertical=run((c,0,c,0))
        # The old identity attitude sends +body-X rotor thrust north.
        assert identity["next_vn"] > 0.4
        # The corrected tailsitter attitude redirects the same thrust vertically.
        assert abs(vertical["next_vn"]) < 1e-6
        assert abs(vertical["next_ve"]) < 1e-9
        assert vertical["next_vd"] < 0.0
        assert vertical["vtol_feasible"] == 1.0
    finally:
        f.close()


def test_native_jsbsim_reset_and_airframe_represent_current_tailsitter_start():
    import xml.etree.ElementTree as ET
    root=_root()
    native=root/'layer6_v20261007_1032'/'FlightStack'
    source=native/'ardupilot/Tools/autotest/aircraft/phoenix_v3'
    assert (source/'phoenix_v3.xml').is_file(), source
    reset=ET.parse(source/'reset.xml').getroot()
    theta=reset.find('theta')
    assert theta is not None and theta.attrib.get('unit')=='DEG'
    assert math.isclose(float(theta.text),89.0,abs_tol=1e-9)
    # A source XML being present is not a controller- or flight-authority certificate.
    assert 'phoenix' in (source/'phoenix_v3.xml').read_text().lower()



def test_native_jsbsim_binary_is_present_without_claiming_retired_fmu_provenance():
    root=_root();native=root/'layer6_v20261007_1032'/'FlightStack'
    binary=native/'jsbsim/build/src/JSBSim'
    assert binary.is_file() and binary.stat().st_size>0, binary
    assert (native/'ardupilot/Tools/autotest/aircraft/phoenix_v3/reset.xml').is_file()
    env=(root/'TDOC_v20260911_2052/configs/environment.sh').read_text()
    assert 'unset TDDT_PTLIB_ROOT EXT_FMU_SIM_ROOT' in env
    # Verify the native binary is real; the presence check does not certify
    # rebuild reproducibility, source/binary hash linkage or flight fitness.
    assert 'ardupilot_JSBsim_v20260914_1308' not in env

