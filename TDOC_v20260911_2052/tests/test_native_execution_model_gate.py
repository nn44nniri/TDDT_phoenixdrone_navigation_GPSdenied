"""Model-only FF must not certify the different native pilot execution law."""
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tddt_tdoc.adapters.layer5 import Layer5Adapter


def invoke(mode, transport, phase="vertical_takeoff", final=True):
    core_result={"ok":True,"gates":{"FINAL":final},
        "layer6_package":{"u_cmd":[0.,-.321,.797,0.]},
        "control_action":{"first_move":[0.,-.321,.797,0.]}}
    original=copy.deepcopy(core_result)
    adapter=Layer5Adapter.__new__(Layer5Adapter)
    adapter.config=SimpleNamespace(get=lambda key, default=None:
        transport if key=="layers.layer6.online.transport" else default)
    adapter._ensure=lambda:None
    # Isolate the execution-law gate after a compatible model was checked.
    # R3.06 tests separately exercise rejection at the real dependency gate.
    adapter._certificate_model_gate=lambda cert:{'ok':True}
    adapter.engine=SimpleNamespace(run=lambda *args, **kwargs:core_result)
    adapter.kstore_workspace=SimpleNamespace(sync=lambda:{"changed_files":0})
    adapter.root=Path("/external/layer5")
    adapter.kstore_root=Path("/isolated/kstore")
    adapter.module=SimpleNamespace(__version__="test")
    result=adapter.invoke({"learning_mode":mode,"layer4_result":{},
        "request":{"stage":phase,"transaction_id":"test-tx"}})
    assert core_result==original
    return result["result"]


@pytest.mark.parametrize("phase",["vertical_takeoff","hover","transition_forward",
    "climb","cruise","turn","approach","transition_back","vertical_landing"])
def test_model_only_ff_cannot_authorize_native_pilot_execution(phase):
    decision=invoke("ONLINE_JSBSIM","Layer6Client_GUI_MAVLink",phase)
    assert decision["gates"]["FINAL"] is False
    assert decision["gates"]["native_execution_model_match"] is False
    assert decision["layer6_package"] is None
    assert decision["native_execution_model_rejection"]["actuation_authority"] is False
    assert decision["native_intent_rejection"]=="native_pilot_controller_prediction_unavailable"


@pytest.mark.parametrize("mode,transport",[("OFFLINE_SE","Layer6Client_GUI_MAVLink"),
    ("OFFLINE_SE","legacy"),("ONLINE_JSBSIM","legacy")])
def test_offline_and_existing_legacy_decisions_are_preserved(mode,transport):
    decision=invoke(mode,transport)
    assert decision["gates"]["FINAL"] is True
    assert decision["layer6_package"]["u_cmd"]==[0.,-.321,.797,0.]
    assert "native_execution_model_rejection" not in decision


def test_existing_final_rejection_is_preserved():
    decision=invoke("ONLINE_JSBSIM","Layer6Client_GUI_MAVLink",final=False)
    assert decision["gates"]["FINAL"] is False
    assert "native_execution_model_rejection" not in decision
