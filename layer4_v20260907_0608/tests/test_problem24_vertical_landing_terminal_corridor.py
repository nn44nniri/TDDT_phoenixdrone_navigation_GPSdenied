from types import SimpleNamespace
import numpy as np
from layer4 import Layer4Config
from layer4.verification import FullFidelityVerifier
from layer4.vtol import VTOLProfile


def _rows(agls, mode="vertical_landing"):
    return [{"step":i+1,"snapshot":{"context":{"flight_mode":mode,"altitude_agl_m":a},"std":{"representations":{}},"guidance":{}}} for i,a in enumerate(agls)]


def test_vertical_landing_uses_landing_mode(root):
    cfg=Layer4Config.from_yaml(root/"configs/configuration_boulder.yaml")
    lim=VTOLProfile(cfg).limits("vertical_landing")
    assert lim["constraint_mode"]=="landing"
    assert lim["min_airspeed_m_s"]==0.0


def test_full_fidelity_landing_below_30m_is_not_terrain_clearance_violation(root):
    cfg=Layer4Config.from_yaml(root/"configs/configuration_boulder.yaml")
    v=FullFidelityVerifier(cfg, None)
    cand=SimpleNamespace(actions=np.zeros((3,5)))
    snap=SimpleNamespace(context={"flight_mode":"vertical_landing","altitude_agl_m":12.0})
    out=v._full_feasibility(_rows([12.0,10.0,8.0]),cand,snap,None)
    assert "terrain_clearance" not in out["reasons"]
    assert "landing_ground_penetration" not in out["reasons"]
    assert out["safe"]


def test_landing_ground_penetration_still_fails_closed(root):
    cfg=Layer4Config.from_yaml(root/"configs/configuration_boulder.yaml")
    v=FullFidelityVerifier(cfg, None)
    cand=SimpleNamespace(actions=np.zeros((2,5)))
    snap=SimpleNamespace(context={"flight_mode":"vertical_landing","altitude_agl_m":2.0})
    out=v._full_feasibility(_rows([1.0,-0.1]),cand,snap,None)
    assert not out["safe"]
    assert "landing_ground_penetration" in out["reasons"]


def test_landing_upward_excursion_fails_closed(root):
    cfg=Layer4Config.from_yaml(root/"configs/configuration_boulder.yaml")
    v=FullFidelityVerifier(cfg, None)
    cand=SimpleNamespace(actions=np.zeros((2,5)))
    snap=SimpleNamespace(context={"flight_mode":"vertical_landing","altitude_agl_m":10.0})
    out=v._full_feasibility(_rows([10.5,12.0]),cand,snap,None)
    assert not out["safe"]
    assert "landing_corridor_upward_excursion" in out["reasons"]


def test_cruise_keeps_30m_terrain_floor(root):
    cfg=Layer4Config.from_yaml(root/"configs/configuration_boulder.yaml")
    v=FullFidelityVerifier(cfg, None)
    cand=SimpleNamespace(actions=np.zeros((1,5)))
    snap=SimpleNamespace(context={"flight_mode":"cruise","altitude_agl_m":12.0})
    out=v._full_feasibility(_rows([12.0],mode="cruise"),cand,snap,None)
    assert not out["safe"]
    assert "terrain_clearance" in out["reasons"]
