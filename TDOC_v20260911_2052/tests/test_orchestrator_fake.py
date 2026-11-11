from __future__ import annotations
import json
from pathlib import Path
from tddt_tdoc import TDOCConfig,TDOCOrchestrator
from tddt_tdoc.storage import iter_jsonl_auto

class Fake2:
    layer_id=2
    def preflight(self): return {"ok":True}
    def invoke(self,payload):
        p={n:{"twin_id":n,"state":{},"uncertainty":{},"quality":{"score":1},"validity":{"score":1},"reliability":{"prior":1},"context":{},"timestamp":payload.get("reference_time_s",0),"provenance":{}} for n in ("MESA_NAV","WingMPET","MicroMetTwin")}
        applied=bool(payload.get("training_feedback"))
        return {"ok":True,"packets":p,"training_feedback_applied":applied,"control_effect_applied":applied,"direct_layer_ownership":False}
    def close(self):pass
class Fake3:
    layer_id=3
    def preflight(self): return {"ok":True}
    def invoke(self,payload):
        s={"policy_version":"L3-policy-v1","reference_time_s":payload["request"].get("reference_time_s",0),"std":{},"topology":{},"weights":{},"couplings":{},"center_of_consistency":{},"uncertainty_td":{},"guidance":{},"constraints":{},"context":{},"provenance":{"snapshot_kind":"atomic_layer3_network_snapshot","fast_index":1}}
        return {"ok":True,"result":{"snapshot":s},"snapshot":s}
    def close(self):pass

class Fake5:
    layer_id=5
    def preflight(self): return {"ok":True,"implemented":True,"root":"/tmp/layer5_v20260907_0608","expected_package":"layer5_v20260907_0608"}
    def invoke(self,payload): return {"ok":True,"status":"OK","result":{"ok":True,"learning_mode":"OFFLINE_SE","control_action":{"first_move":[0.1,0.0,0.5,0.0]},"gates":{"FINAL":True}}}
    def offline_training_feedback(self,envelope,version=None):
        return {"ok":True,"route":"LAYER2_TRAINING_FEEDBACK","training_feedback":{"schema":"TDOC-L5-offline-training-feedback-v1","source":"Layer5/OFFLINE_SE","destination":"Layer2","feedback_hash":"fake","actuator_settings":{"order":["aileron","elevator","throttle","rudder"],"values":[0.1,0.0,0.5,0.0]}}}
    def close(self):pass
class Fake6:
    layer_id=6
    def preflight(self): return {"ok":False,"implemented":True,"offline_in_path":False,"online_ready":False,"ptlib":{"ok":False,"external_only":True}}
    def invoke(self,payload): raise AssertionError("Layer6 must not be invoked in OFFLINE_SE")
    def close(self):pass

class Fake7:
    layer_id=7
    def preflight(self): return {"ok":True,"implemented":True,"external_only":True,"root":"/tmp/layer7_v20260901_0921","version":"0.3.0","registration_descriptor":{"trigger_owner":"TDOC","direct_actuation":False}}
    def invoke(self,payload): raise AssertionError("Layer7 must not be invoked in OFFLINE_SE")
    def close(self):pass

class Fake4:
    layer_id=4
    def preflight(self):return {"ok":True}
    def invoke(self,payload):return {"ok":True,"result":{"snapshot_version":payload["snapshot"]["policy_version"],"candidates":[],"verified_candidates":[],"provenance":{"actuator_command":False}}}
    def close(self):pass

def test_full_current_pipeline_is_tdoc_owned(tmp_path):
    cfg_path=Path(__file__).parents[1]/"configs/tdoc.yaml"; cfg=TDOCConfig.from_yaml(cfg_path)
    cfg.data["runtime"]["root"]=str(tmp_path/"runtime")
    o=TDOCOrchestrator(cfg);o.adapters[2]=Fake2();o.adapters[3]=Fake3();o.adapters[4]=Fake4();o.adapters[5]=Fake5();o.adapters[6]=Fake6();o.adapters[7]=Fake7()
    try:r=o.run_cycle({"reference_time_s":1.0,"context":{}}).to_dict()
    finally:o.close()
    assert r["status"]=="OK"
    assert r["conformance"]["A_invoke"] is True
    assert r["provenance"]["trigger_owner"]=="TDOC/RuntimeScheduler"
    assert r["downstream_layers"]["5"]["status"]=="OK"
    assert r["downstream_layers"]["6"]["status"]=="NOT_IN_OFFLINE_PATH"
    data=[]
    for pth in sorted((tmp_path/"runtime/events/invocations/chunks").glob("chunk_*.jsonl.gz")): data.extend(iter_jsonl_auto(pth))
    invokes=[e for e in data if e["event_type"]=="Invoke"]
    assert [e["destination"] for e in invokes]==["Layer2","Layer3","Layer4","Layer5","Layer2"]
    assert all(e["trigger_owner"]=="TDOC/RuntimeScheduler" for e in invokes)

def test_offline_layer_deadline_is_fail_closed(tmp_path):
    cfg_path=Path(__file__).parents[1]/"configs/tdoc.yaml"; cfg=TDOCConfig.from_yaml(cfg_path)
    cfg.data["runtime"]["root"]=str(tmp_path/"runtime")
    cfg.data["deadlines"]["layer2_s"]=1e-12
    o=TDOCOrchestrator(cfg);o.adapters[2]=Fake2();o.adapters[3]=Fake3();o.adapters[4]=Fake4();o.adapters[5]=Fake5();o.adapters[6]=Fake6();o.adapters[7]=Fake7()
    try:r=o.run_cycle({"reference_time_s":1.0,"context":{}}).to_dict()
    finally:o.close()
    assert r["status"]!="OK"
    assert r["layer3_result"] is None
    timing=r["provenance"]["component_timing_s"]
    assert timing["L2"]>=0.0
    assert r["provenance"]["offline_timing_contract"]["ok"] is False
