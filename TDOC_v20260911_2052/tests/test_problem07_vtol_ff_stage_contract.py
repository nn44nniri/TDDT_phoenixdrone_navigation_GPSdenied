from __future__ import annotations

from tddt_tdoc.adapters.layer4 import Layer4Adapter


def test_problem07_controlled_vtol_phases_are_full_fidelity_applicable():
    expected = {
        "vertical_takeoff": "takeoff_transition",
        "hover": "takeoff_transition",
        "transition_back": "approach",
        "vertical_landing": "approach",
    }
    for phase, prior in expected.items():
        contract = Layer4Adapter._full_fidelity_stage_contract({"stage": phase})
        assert contract["applicable"] is True
        assert contract["requested_stage"] == phase
        assert contract["sarg_stage"] == prior
        assert contract["reason"] == "vtol_or_forward_phase_supported"


def test_problem07_sarg_aliases_match_layer3_knowledge_contract():
    # Import through the environment-selected Layer3 root (configured by conftest/
    # TDOC environment); no alternate library path is allowed.
    from layer3.knowledge import SARGRepository
    for phase, prior in Layer4Adapter._FF_STAGE_MAP.items():
        assert SARGRepository.MISSION_STAGE_ALIASES.get(phase, phase) == prior


def test_problem07_passive_boundaries_remain_deferred_fail_closed():
    for phase in ("on_ground", "touchdown"):
        contract = Layer4Adapter._full_fidelity_stage_contract({"stage": phase})
        assert contract["applicable"] is False
        assert contract["passive_boundary_phase"] is True
        assert contract["reason"] == "passive_boundary_phase_deferred"
