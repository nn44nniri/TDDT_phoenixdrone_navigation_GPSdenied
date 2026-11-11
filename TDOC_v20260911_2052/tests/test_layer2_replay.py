from __future__ import annotations
from pathlib import Path
from tddt_tdoc import TDOCConfig
from tddt_tdoc.adapters.layer2 import Layer2Adapter

def pkt(name):
    return {"twin_id":name,"state":{},"uncertainty":{},"quality":{"score":1},"validity":{"score":1},"reliability":{"prior":1},"context":{},"timestamp":0,"provenance":{}}

def test_provided_packets_do_not_invoke_external_layer2():
    cfg=TDOCConfig.from_yaml(Path(__file__).parents[1]/"configs/tdoc.yaml")
    a=Layer2Adapter(cfg)
    r=a.invoke({"packets":{"MESA_NAV":pkt("MESA_NAV"),"WingMPET":pkt("WingMPET"),"MicroMetTwin":pkt("MicroMetTwin")}})
    assert r["ok"] and r["mode"]=="provided_packets"
    assert r["direct_layer_ownership"] is False
