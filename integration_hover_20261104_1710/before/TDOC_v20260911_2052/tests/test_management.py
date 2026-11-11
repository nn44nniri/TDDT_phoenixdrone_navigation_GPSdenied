from pathlib import Path
from tddt_tdoc.twin_registry import TwinRegistry
from tddt_tdoc.network_state import NetworkStateMirror
from tddt_tdoc.feedback import FeedbackRouter

def test_twin_registry_accepts_traceable_packet(tmp_path):
    p={"MESA_NAV":{"twin_id":"MESA_NAV","layer":2,"state":{"x":1},"uncertainty":{},"quality":{"score":1},"validity":{"score":1},"reliability":{"prior":1},"context":{},"timestamp":1.0,"provenance":{"model":"MESA_NAV.fmu","version":"v1"}}}
    r=TwinRegistry(tmp_path/"twins.json"); out=r.observe(p)
    assert out["ok"] and r.snapshot()["MESA_NAV"]["accepted"]

def test_network_state_mirror_is_versioned(tmp_path):
    s={"policy_version":"v1","reference_time_s":1.0,"std":{},"weights":{},"couplings":{},"topology":{},"center_of_consistency":{},"mismatch":0.0,"consistency_margin":1.0,"provenance":{"snapshot_kind":"atomic_layer3_network_snapshot"}}
    m=NetworkStateMirror(tmp_path/"n.json"); d=m.update(s)
    assert d["policy_version"]=="v1" and len(d["network_state_hash"])==64

def test_feedback_router_routes_without_updating_models():
    r=FeedbackRouter().route({"domain_error":True,"coupling_error":True})
    assert r["routes"]==["Layer2","Layer3.2"]
    assert r["update_owner"].startswith("Layer7")
