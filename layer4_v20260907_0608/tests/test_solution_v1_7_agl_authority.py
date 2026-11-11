from __future__ import annotations

import numpy as np

from layer4 import Layer4Config, AtomicSnapshot, Candidate
from layer4.state import StateCodec
from layer4.feasibility import FeasibilityEngine
from layer4.verification import FullFidelityVerifier
from layer4.agl import resolve_operational_terrain, _load_dem


def _cfg(root):
    return Layer4Config.from_yaml(root / "configs/configuration_boulder.yaml")


def _snapshot(agl_marker=False, mesa_position=True):
    ctx={"flight_mode":"transition_forward"}
    if agl_marker:
        ctx.update({
            "altitude_agl_m":12.5,
            "terrain_elevation_m":1795.0,
            "agl_available":True,
            "agl_authority":{"schema":"TDOC-Operational-AGL-v1","valid":True,"reason":"MESA_STD_PLUS_DEM","simulator_truth_used":False},
        })
    return AtomicSnapshot(
        version="p0-7", reference_time_s=0.0, hash="h",
        std={"representations":{
            "WingMPET":{"canonical_state":{"airspeed_m_s":0.0},"validity":1.0},
            "MESA_NAV":{"canonical_state":({"position_ned_m":[4428919.0,474405.0,-1807.5]} if mesa_position else {}),"validity":1.0},
            "MicroMetTwin":{"canonical_state":{"wind_ned_m_s":[0.0,0.0,0.0]},"validity":1.0},
        }}, center_of_consistency={}, mismatch=0.0, weights={}, couplings={}, topology={}, consistency_margin=1.0,
        uncertainty_td={}, guidance={}, constraints={}, context=ctx, stability={"stable":True}, provenance={}, slow_policy={},
    )


def _trajectory(codec, *, north=0.0, east=0.0):
    X=np.zeros((7,codec.dim),float)
    X[:,codec.index["airspeed_m_s"]]=np.linspace(0.0,3.0,7)
    X[:,codec.index["mesa_position_ned_m_0"]]=north
    X[:,codec.index["mesa_position_ned_m_1"]]=east
    X[:,codec.index["mesa_position_ned_m_2"]]=-1807.5
    return X


def test_missing_agl_is_explicit_fail_closed_not_permissive_fallback(root):
    cfg=_cfg(root); codec=StateCodec(cfg.package_root/cfg.get("qlpv.state_schema_file")); eng=FeasibilityEngine(cfg,codec)
    net=[{"J_C":0.0,"mu":1.0,"W":{},"K":{}} for _ in range(7)]
    out=eng.evaluate_trajectory(_trajectory(codec),net,_snapshot(False,mesa_position=False),terminal={"certified":True})
    assert out["feasible"] is False
    assert "AGL_UNAVAILABLE" in out["reasons"]
    assert all(r["agl_m"] is None for r in out["per_step"])
    assert all(r["obstacle_clearance_m"] is None for r in out["per_step"])
    assert all(r["terrain_policy"]["policy"]=="AGL_UNAVAILABLE" for r in out["per_step"])
    assert "1000000000" not in repr(out)


def test_authoritative_agl_remains_usable(root):
    cfg=_cfg(root); codec=StateCodec(cfg.package_root/cfg.get("qlpv.state_schema_file")); eng=FeasibilityEngine(cfg,codec)
    net=[{"J_C":0.0,"mu":1.0,"W":{},"K":{}} for _ in range(7)]
    out=eng.evaluate_trajectory(_trajectory(codec,north=4428919.0,east=474405.0),net,_snapshot(True),terminal={"certified":True})
    assert "AGL_UNAVAILABLE" not in out["reasons"]
    assert out["per_step"][0]["agl_m"] is not None
    assert out["per_step"][0]["terrain_prediction"]["ok"] is True
    assert out["per_step"][0]["agl_authority"]["reason"] == "MESA_STD_PLUS_DEM"


def test_authoritative_entry_agl_does_not_mask_trajectory_outside_dem(root):
    cfg=_cfg(root); codec=StateCodec(cfg.package_root/cfg.get("qlpv.state_schema_file")); eng=FeasibilityEngine(cfg,codec)
    net=[{"J_C":0.0,"mu":1.0,"W":{},"K":{}} for _ in range(7)]
    out=eng.evaluate_trajectory(_trajectory(codec),net,_snapshot(True),terminal={"certified":True})
    assert out["feasible"] is False
    assert "AGL_UNAVAILABLE" in out["reasons"]
    assert all(row["agl_m"] is None for row in out["per_step"])
    assert all(row["terrain_policy"]["policy"]=="AGL_UNAVAILABLE" for row in out["per_step"])


def test_full_fidelity_cannot_execute_or_verify_without_authoritative_agl(root):
    cfg=_cfg(root); codec=StateCodec(cfg.package_root/cfg.get("qlpv.state_schema_file")); v=FullFidelityVerifier(cfg,codec)
    cand=Candidate(candidate_id="C",scenario_id="S",snapshot_version="p0-7",snapshot_hash="h",probability=1.0,
                   trajectory=[],uncertainty=[],network_trajectory=[],actions=[],objective_vector={})
    out=v.verify(cand,_snapshot(False,mesa_position=False),None)
    assert out["verified"] is False
    assert out["candidate_verified"] is False
    assert out["executed"] is False
    assert out["reason"] == "AGL_UNAVAILABLE"


def test_operational_terrain_reuses_exact_dem_content_cache(root,monkeypatch):
    map_root=root.parent/'boulder_multilayer_map_builder_v20260814_1215'/'outputs'/'region_v1_map'
    monkeypatch.setenv('TDDT_MAP_ROOT',str(map_root))
    cfg=_cfg(root); _load_dem.cache_clear()
    first=resolve_operational_terrain(4428910.218160756,474395.80553868186,cfg)
    before=_load_dem.cache_info()
    second=resolve_operational_terrain(4428910.228160756,474395.81553868187,cfg)
    after=_load_dem.cache_info()
    assert first['ok'] is True and second['ok'] is True
    assert after.hits == before.hits+1
    assert after.misses == before.misses
    assert first['authority']['terrain_interpolation']=='BILINEAR_PIXEL_CENTRE'
