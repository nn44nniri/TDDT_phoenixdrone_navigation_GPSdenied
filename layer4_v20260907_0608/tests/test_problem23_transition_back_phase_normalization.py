from layer4 import Layer4Config
from layer4.vtol import VTOLProfile


def _profile(root):
    cfg = Layer4Config.from_yaml(root / "configs/configuration_boulder.yaml")
    return VTOLProfile(cfg)


def test_transition_back_uses_transition_constraint_profile(root):
    p = _profile(root)
    lim = p.limits("transition_back")
    assert lim["mode"] == "transition_back"
    assert lim["constraint_mode"] == "transition"
    assert lim["min_airspeed_m_s"] == p.limits("transition")["min_airspeed_m_s"] == 3.0


def test_transition_back_evidence_airspeed_no_longer_hits_cruise_stall_floor(root):
    p = _profile(root)
    lim = p.limits("transition_back")
    # The problem20_fixed_v1 evidence reports late transition-back airspeed near 6.7 m/s.
    assert 6.7 >= lim["min_airspeed_m_s"]
    assert lim["min_airspeed_m_s"] < 9.1


def test_fixed_wing_phases_keep_their_stall_floor(root):
    p = _profile(root)
    for phase in ("climb", "cruise", "turn", "approach"):
        assert p.limits(phase)["min_airspeed_m_s"] == 9.1


def test_vertical_landing_uses_landing_profile_after_problem24(root):
    p = _profile(root)
    lim = p.limits("vertical_landing")
    assert lim["constraint_mode"] == "landing"
    assert lim["min_airspeed_m_s"] == 0.0
