from __future__ import annotations

from layer3.guidance import MesoGuidanceManager
from layer3.knowledge import SARGRepository


def test_transition_forward_real_valid_reference_not_falsely_downgraded(cfg_factory):
    repo=SARGRepository(cfg_factory())
    ref=repo.select(3, 'transition_forward', 4, reference_admissible=True)
    assert ref['selected'] is True
    # Site SARG [3,1,4] is VALID/Wing OK/MESA OK, not historical NAV_ONLY.
    assert ref['status']['record'] == 'VALID'
    assert ref['status']['wing'] == 'OK'
    assert ref['status']['mesa'] == 'OK'
    assert ref['reference_admissible'] is True
    assert ref['primary_flight_seed_eligible'] is True
    assert ref['reference_admission']['no_implicit_cross_regime_fallback'] is True


def test_actual_ineligible_sarg_exact_key_is_diagnostic_only(cfg_factory):
    repo=SARGRepository(cfg_factory())
    # Exact canonical primary key observed in read-only site diagnosis.
    # No stage alias, fallback, or alteration of actual SARG records.
    ref=repo.select(1, 6, 0, reference_admissible=True)
    assert ref['selected'] is True
    assert ref['status']['record'] == 'NOT_TRIGGERED'
    assert ref['status']['wing'] == 'FMU_REJECTED'
    assert ref['reference_admissible'] is False
    assert ref['primary_flight_seed_eligible'] is False
    assert ref['reference_mode'] == 'diagnostic_only'
    blockers=set(ref['reference_admission']['blockers'])
    assert 'record_status:NOT_TRIGGERED' in blockers
    assert 'wing_status:FMU_REJECTED' in blockers
    assert 'flight_ok:false' in blockers
    assert ref['reference_admission']['no_implicit_cross_regime_fallback'] is True


def test_valid_cruise_sarg_reference_remains_admissible(cfg_factory):
    repo = SARGRepository(cfg_factory())
    ref = repo.select(3, "cruise", 4, reference_admissible=True)
    assert ref["selected"] is True
    assert ref["status"]["record"] == "VALID"
    assert ref["status"]["wing"] == "OK"
    assert ref["status"]["mesa"] == "OK"
    assert ref["reference_admissible"] is True
    assert ref["primary_flight_seed_eligible"] is True
    assert ref["reference_admission"]["blockers"] == []


def test_invalid_exact_sarg_cannot_inject_mrg_guidance(cfg_factory):
    cfg = cfg_factory()
    repo = SARGRepository(cfg)
    ref = repo.select(1, 6, 0, reference_admissible=True)
    mgr = MesoGuidanceManager(cfg)
    aggregate = {"min_consistency_margin": 0.5, "fault_count": 0, "fast_count": 10, "mean_couplings": {"x": 1.0}}
    crp = {"selected": True, "matched": True, "quality_score": 0.9}
    deviation = {"score": 0.95, "available": True}
    out = mgr.build(1, aggregate, crp, ref, deviation, {})
    assert out["reference_admissible"] is False
    assert out["reference_mode"] == "diagnostic_nearest_only"
    assert out["sarg_injected_into_guidance"] is False
    assert out["constraint_delta"]["reference_min_clearance_m"] is None
    assert out["constraint_delta"]["reference_airspeed_m_s"] is None
    assert out["optimizer_delta"]["reference_objective"] is None
    assert out["provenance"]["invalid_sarg_guidance_injection"] is False
