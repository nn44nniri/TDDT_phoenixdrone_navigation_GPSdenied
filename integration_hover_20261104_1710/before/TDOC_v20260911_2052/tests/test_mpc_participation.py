from tddt_tdoc.participation import cycle_mpc_participation, aggregate_mpc_participation


def cycle(tx, *, l4=True, l5=True, final=False, executed=False):
    c={"transaction_id":tx,"reference_time_s":1.0,"provenance":{"learning_mode":"OFFLINE_SE"},"downstream_layers":{}}
    if l4:
        c["layer4_result"]={"conformance":{"C_optimizer":True,"C_tube":True,"C_robust":True},"candidates":[{"feasible":True}],"verified_candidates":[],"pareto_front":[{}],"actuator_command":False,"network_commit":False}
    if l5:
        c["downstream_layers"]["5"]={"status":"OK","output":{"ok":True,"result":{"operational_mode":"AUTO" if final else "HOLD","provenance":{"only_first_mpc_move":True},"gates":{"FINAL":final},"decision":{"candidate_id":"C1" if final else None,"approval":{"final_gate":final,"reason":None if final else "hold"}},"control_action":{"first_move":[0,0,0,0]} if final else None}}}
    c["downstream_layers"]["6"]={"status":"EXECUTED" if executed else "NOT_IN_OFFLINE_PATH","output":{"actuation_executed":executed}}
    return c


def test_cycle_participation_separates_mpc_from_actuation():
    r=cycle_mpc_participation(cycle("a",final=False,executed=False))
    assert r["layer4"]["mpc_participated"] is True
    assert r["layer5"]["mpc_first_move_participated"] is True
    assert r["layer5"]["final_gate"] is False
    assert r["layer5"]["actuation_executed"] is False


def test_aggregate_participation_ratios():
    r=aggregate_mpc_participation([cycle("a"),cycle("b",final=True,executed=True),cycle("c",l4=False,l5=False)])
    assert r["layer4"]["invoked_cycles"]==2
    assert r["layer4"]["mpc_participation_cycles"]==2
    assert r["layer5"]["mpc_first_move_cycles"]==2
    assert r["layer5"]["final_gate_cycles"]==1
    assert r["layer5"]["actuation_executed_cycles"]==1


def test_final_is_unevaluated_when_layer5_did_not_run():
    r=cycle_mpc_participation(cycle("held-upstream",l4=False,l5=False))
    assert r["layer5"]["invoked"] is False
    assert r["layer5"]["final_gate"] is None
    assert r["layer5"]["final_gate_evaluated"] is False
