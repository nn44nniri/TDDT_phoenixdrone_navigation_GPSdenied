from tddt_tdoc.participation import cycle_decision_gate_trace


def _held_l5():
    return {"status":"HELD","output":{"ok":False,"status":"HELD","readiness":{"ready":False,"reasons":["valid"]}}}


def test_online_fast_lane_missing_l4_artifact_precedes_l5_final():
    cycle={
        "transaction_id":"online-c1",
        "layer4_result":None,
        "layer_status":{"4":{"ready":True}},
        "downstream_layers":{"5":_held_l5(),"6":{"status":"HELD","output":{"ok":False,"status":"HELD"}}},
        "provenance":{"learning_mode":"ONLINE_JSBSIM","rtmo_pre_release_verify":False},
    }
    t=cycle_decision_gate_trace(cycle)
    assert t["first_stop_gate"]=="L4_ARTIFACT_UNAVAILABLE"
    assert t["first_stop_reasons"]==["no_valid_l4_artifact"]
    assert t["layer4"]["invoked"] is False
    assert t["layer4"]["admission_ready"] is True
    assert t["layer4"]["artifact_unavailable"] is True
    assert t["layer5"]["invoked"] is False
    assert t["layer5"]["FINAL"] is None
    assert t["layer5"]["FINAL_evaluated"] is False


def test_pre_release_missing_l4_is_not_mislabeled_artifact_cache_stop():
    cycle={
        "transaction_id":"online-pre",
        "layer4_result":None,
        "layer_status":{"4":{"ready":True}},
        "downstream_layers":{"5":_held_l5(),"6":{"status":"HELD","output":{"ok":False,"status":"HELD"}}},
        "provenance":{"learning_mode":"ONLINE_JSBSIM","rtmo_pre_release_verify":True},
    }
    t=cycle_decision_gate_trace(cycle)
    assert t["first_stop_gate"]!="L4_ARTIFACT_UNAVAILABLE"


def test_actual_l5_failure_still_attributes_to_l5():
    cycle={
        "transaction_id":"online-l5",
        "layer4_result":{"candidates":[{"candidate_id":"c1","feasible":True,"verified":True}],"verified_candidates":[{"candidate_id":"c1"}]},
        "layer_status":{"4":{"ready":True}},
        "downstream_layers":{"5":{"status":"OK","output":{"ok":True,"result":{"fallback":{"reason":"policy_hold"},"gates":{"FINAL":False}}}},"6":{"status":"HELD","output":{"status":"HELD"}}},
        "provenance":{"learning_mode":"ONLINE_JSBSIM","rtmo_pre_release_verify":False},
    }
    t=cycle_decision_gate_trace(cycle)
    assert t["first_stop_gate"]=="L5_FALLBACK"
    assert t["layer4"]["invoked"] is True
    assert t["layer5"]["invoked"] is True
    assert t["layer5"]["FINAL"] is False
    assert t["layer5"]["FINAL_evaluated"] is True
