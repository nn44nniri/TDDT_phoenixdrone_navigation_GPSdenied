from tddt_tdoc.participation import cycle_decision_gate_trace


def test_trace_attributes_no_verified_candidate_to_layer4_verification():
    cycle = {
        "transaction_id": "tx1",
        "reference_time_s": 1.0,
        "provenance": {"learning_mode": "ONLINE_JSBSIM"},
        "layer4_result": {
            "candidates": [
                {
                    "candidate_id": "c1",
                    "feasible": True,
                    "verified": False,
                    "verification": {
                        "executed": True,
                        "verified": False,
                        "reason": "full_fidelity_verification_gate_failed",
                    },
                }
            ],
            "verified_candidates": [],
        },
        "downstream_layers": {
            "5": {
                "output": {
                    "result": {
                        "operational_mode": "HOLD",
                        "decision": {"candidate_id": None, "approval": {"final_gate": False}},
                        "fallback": {"used": True, "reason": "no_admissible_bound_candidate"},
                        "gates": {
                            "tdoc_boundary": {"ok": True},
                            "lifecycle": {"ok": True},
                            "FINAL": False,
                            "integrity": {"c1": {"ok": True, "reasons": []}},
                            "admissibility": {"c1": {"ok": False, "reasons": ["not_full_fidelity_verified"]}},
                            "phoenix_binding": {"c1": {"ok": True}},
                            "policy_gate": {"c1": {"ok": True}},
                        },
                        "layer6_package": None,
                    }
                }
            },
            "6": {"status": "OBSERVATION_ONLY", "output": {"status": "OBSERVATION_ONLY"}},
        },
    }
    t = cycle_decision_gate_trace(cycle)
    assert t["first_stop_gate"] == "L4_FULL_FIDELITY_VERIFICATION"
    assert t["layer4"]["verified_count"] == 0
    assert t["layer5"]["FINAL"] is False
    assert t["candidates"][0]["layer5"]["admissible"] is False
    assert "full_fidelity_verification_gate_failed" in t["first_stop_reasons"]


def test_trace_final_to_release_chain():
    release = {"ready": True, "released": True, "holding": False, "release_token_consumed": True}
    cycle = {
        "transaction_id": "tx2",
        "provenance": {"learning_mode": "ONLINE_JSBSIM"},
        "layer4_result": {
            "candidates": [
                {"candidate_id": "c1", "feasible": True, "verified": True, "verification": {"executed": True, "verified": True}}
            ],
            "verified_candidates": [{"candidate_id": "c1"}],
        },
        "downstream_layers": {
            "5": {
                "output": {
                    "result": {
                        "decision": {"candidate_id": "c1", "approval": {"final_gate": True}},
                        "layer6_package": {},
                        "gates": {
                            "FINAL": True,
                            "tdoc_boundary": {"ok": True},
                            "lifecycle": {"ok": True},
                            "integrity": {"c1": {"ok": True}},
                            "admissibility": {"c1": {"ok": True}},
                            "phoenix_binding": {"c1": {"ok": True}},
                            "policy_gate": {"c1": {"ok": True}},
                            "solver": {"c1": {"ok": True, "vp": {"ok": True}}},
                            "budget": {"ok": True},
                            "psf": {"gate": True},
                            "rta": {"gate": True, "source": "advanced"},
                            "governance": {"gate": True},
                        },
                    }
                }
            },
            "6": {
                "status": "EXECUTED",
                "output": {
                    "status": "EXECUTED",
                    "execution_evidence": {
                        "write_result": {
                            "stream_status": {
                                "running": True,
                                "send_count": 1,
                                "last_result": {"release_confirmation": release},
                            }
                        }
                    },
                },
            },
        },
    }
    t = cycle_decision_gate_trace(cycle)
    assert t["first_stop_gate"] == "FINAL"
    assert t["layer5"]["FINAL"] is True
    assert t["layer6"]["release_confirmed"] is True
    assert t["layer6"]["released"] is True
    assert t["layer6"]["release_token_consumed"] is True
    assert t["layer6"]["direct_pwm"] is False
