from layer4 import Layer4Config
from layer4.vtol import VTOLProfile


def _profile(root):
    cfg = Layer4Config.from_yaml(root / "configs/configuration_boulder.yaml")
    return VTOLProfile(cfg)


def test_vertical_takeoff_uses_takeoff_constraint_profile(root):
    p = _profile(root)
    lim = p.limits("vertical_takeoff")
    assert lim["mode"] == "vertical_takeoff"
    assert lim["constraint_mode"] == "takeoff"
    assert lim["min_airspeed_m_s"] == p.limits("takeoff")["min_airspeed_m_s"] == 0.0
    assert lim["max_wind_m_s"] == p.limits("takeoff")["max_wind_m_s"]


def test_vertical_takeoff_low_airspeed_is_not_rejected_by_fixed_wing_stall_floor(root):
    p = _profile(root)
    lim = p.limits("vertical_takeoff")
    # problem20_fixed_v1 evidence reports predicted airspeed about 0..0.53 m/s.
    for airspeed in (0.0, 0.1, 0.53):
        assert airspeed >= lim["min_airspeed_m_s"]


def test_fixed_wing_phases_keep_stall_floor(root):
    p = _profile(root)
    for phase in ("climb", "cruise", "turn", "approach"):
        lim = p.limits(phase)
        assert lim["constraint_mode"] == phase
        assert lim["min_airspeed_m_s"] == 9.1


def test_vertical_landing_is_owned_by_problem24(root):
    p = _profile(root)
    lim = p.limits("vertical_landing")
    assert lim["constraint_mode"] == "landing"
    assert lim["min_airspeed_m_s"] == 0.0
