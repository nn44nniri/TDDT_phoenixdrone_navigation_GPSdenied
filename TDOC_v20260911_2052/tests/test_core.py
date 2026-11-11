from __future__ import annotations
import json
from pathlib import Path
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.invocation import InvocationGate
from tddt_tdoc.scheduler import TDOCScheduler
from tddt_tdoc.versioning import VersionManager
from tddt_tdoc.provenance import ProvenanceLog
from tddt_tdoc.models import InvocationEvent
from tddt_tdoc.utils import utc_now

def test_scheduler_fast_meso_slow():
    s=TDOCScheduler(1,3,2)
    assert s.boundary(1).label()=="fast"
    assert s.boundary(3).label()=="meso"
    assert s.boundary(6).label()=="slow"

def test_gate_all_true():
    r=InvocationGate().evaluate(4,valid=True,version_ok=True,boundary_ok=True,health_ok=True,policy_route=True)
    assert r.ready and not r.reasons

def test_gate_blocks_not_ready():
    r=InvocationGate().evaluate(3,valid=False,version_ok=True,boundary_ok=True,health_ok=True,policy_route=True)
    assert not r.ready and "valid" in r.reasons

def test_version_safe_commit_and_rollback(tmp_path):
    v=VersionManager(tmp_path,"v1")
    assert not v.safe_commit("v2",authorized=False,validated=True)["committed"]
    assert v.safe_commit("v2",authorized=True,validated=True)["committed"]
    assert v.snapshot()["active"]=="v2"
    assert v.rollback()["active"]=="v2"

def test_hash_chained_provenance(tmp_path):
    p=ProvenanceLog(tmp_path/"events.jsonl")
    e1=InvocationEvent("i1","tx","TDOC/RuntimeScheduler","A","B","Invoke","v","fast","ready","OK",utc_now())
    e2=InvocationEvent("i2","tx","TDOC/RuntimeScheduler","B","TDOC","Route","v","fast","output","OK",utc_now())
    p.append(e1);p.append(e2)
    assert p.verify()["ok"] and p.verify()["events"]==2

def test_provenance_chain_continues_across_instances(tmp_path):
    path=tmp_path/"events.jsonl"
    p=ProvenanceLog(path)
    p.append(InvocationEvent("i1","tx","TDOC/RuntimeScheduler","A","B","Invoke","v","fast","ready","OK",utc_now()))
    q=ProvenanceLog(path)
    q.append(InvocationEvent("i2","tx2","TDOC/RuntimeScheduler","B","C","Invoke","v","fast","ready","OK",utc_now()))
    assert q.verify()["ok"] and q.verify()["events"]==2
