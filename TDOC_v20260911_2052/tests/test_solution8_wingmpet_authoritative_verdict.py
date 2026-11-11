from tddt_tdoc.adapters.layer4 import Layer4Adapter


def _row(*, replay=True, output=1.0, trace=(True, True), consumed=True, source="WingMPET/op27-substepped"):
    return {
        "physical_mission_safe": bool(replay),
        "physical_action_consumed": bool(consumed),
        "physical_replay": {
            "source": source,
            "action_consumed": bool(consumed),
            "vtol_feasible": bool(replay),
            "outputs": {"vtol_feasible": float(output)},
            "substep_trace": [{"vtol_feasible": bool(x)} for x in trace],
        },
    }


def test_solution8_requires_every_wingmpet_signal_to_pass():
    verdict=Layer4Adapter._wingmpet_physical_verdict([_row(),_row()])
    assert verdict["authoritative"] is True
    assert verdict["non_bypassable"] is True
    assert verdict["override_allowed"] is False
    assert verdict["all_rows_vtol_feasible"] is True


def test_solution8_rejects_any_failed_substep_even_if_final_output_says_pass():
    verdict=Layer4Adapter._wingmpet_physical_verdict([_row(trace=(True,False), output=1.0, replay=True)])
    assert verdict["all_rows_vtol_feasible"] is False
    assert verdict["rows"][0]["substeps_all_vtol_feasible"] is False


def test_solution8_rejects_failed_final_output_even_if_replay_flag_says_pass():
    verdict=Layer4Adapter._wingmpet_physical_verdict([_row(output=0.0, replay=True)])
    assert verdict["all_rows_vtol_feasible"] is False
    assert verdict["rows"][0]["output_vtol_feasible"] is False


def test_solution8_rejects_unconsumed_or_non_wingmpet_replay():
    assert Layer4Adapter._wingmpet_physical_verdict([_row(consumed=False)])["all_rows_vtol_feasible"] is False
    assert Layer4Adapter._wingmpet_physical_verdict([_row(source="synthetic/replay")])["all_rows_vtol_feasible"] is False


def test_solution8_empty_replay_is_never_physical_proof():
    verdict=Layer4Adapter._wingmpet_physical_verdict([])
    assert verdict["all_rows_vtol_feasible"] is False
