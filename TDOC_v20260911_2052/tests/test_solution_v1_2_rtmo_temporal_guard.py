from pathlib import Path

from tddt_tdoc.rtmo import MissionPhaseManager, RTClock

ROOT=Path(__file__).resolve().parents[1]


def _all_execution_gates():
    return dict(
        current_state_fresh=True,
        current_state_executable_release=True,
        l4_artifact_valid=True,
        l5_final=True,
        runtime_release=True,
        l6_executed=True,
        actuation_ack=True,
        realized_state_evidence=True,
    )


def test_p0_2_missing_simulation_time_is_hard_phase_advance_blocker():
    m=MissionPhaseManager(offline_min_dwell_s={'on_ground':0.0})
    ok,reasons=m.can_advance(sim_time_s=None,next_capability=True,**_all_execution_gates())
    assert ok is False
    assert 'simulation_time_missing' in reasons
    assert m.phase=='on_ground'
    assert m.entry_sim_time_s is None


def test_p0_2_first_valid_plant_time_starts_dwell_instead_of_skipping_it():
    m=MissionPhaseManager(offline_min_dwell_s={'on_ground':2.0})
    ok,reasons=m.can_advance(sim_time_s=10.0,next_capability=True,**_all_execution_gates())
    assert ok is False
    assert 'offline_min_dwell_not_met' in reasons
    assert m.entry_sim_time_s==10.0
    ok2,reasons2=m.can_advance(sim_time_s=12.1,next_capability=True,**_all_execution_gates())
    assert ok2 is True
    assert 'offline_min_dwell_not_met' not in reasons2


def test_p0_2_reference_time_fallback_is_not_plant_time_authority():
    c=RTClock(period_s=0.1)
    assert c.reference_time(123.456)==123.456
    s=c.plant_time_state()
    assert s['plant_time_present'] is False
    assert s['state_fresh'] is False
    assert s['reason']=='simulation_time_missing'
    assert c.last_sim_time_s is None


def test_p0_2_missing_current_sample_does_not_inherit_previous_freshness():
    c=RTClock(period_s=0.1)
    first=c.observe_sim_time(5.0)
    assert first['ok'] is True and first['state_fresh'] is True
    missing=c.observe_sim_time(None)
    assert missing['ok'] is False
    assert missing['state_fresh'] is False
    assert missing['plant_time_present'] is False
    assert missing['last_valid_simulation_time_s']==5.0
    assert c.last_sim_time_s==5.0


def test_p0_2_nonadvancing_plant_clock_is_not_fresh():
    c=RTClock(period_s=0.1)
    c.observe_sim_time(3.0)
    same=c.observe_sim_time(3.0)
    assert same['ok'] is True
    assert same['advanced'] is False
    assert same['state_fresh'] is False
    assert same['reason']=='simulation_time_not_advanced'


def test_p0_1_regression_historical_capability_cannot_advance_without_execution():
    m=MissionPhaseManager(offline_min_dwell_s={'on_ground':0.0})
    ok,reasons=m.can_advance(
        sim_time_s=1.0,next_capability=True,
        current_state_fresh=True,
        current_state_executable_release=False,
        l4_artifact_valid=False,l5_final=False,runtime_release=True,
        l6_executed=False,actuation_ack=False,realized_state_evidence=False,
    )
    assert ok is False
    assert reasons==['current_state_executable_release_missing']
    assert m.phase=='on_ground'


def test_workflow_uses_current_cycle_sim_time_not_stale_last_sim_time_for_advance():
    src=(ROOT/'scripts/run_training_workflows.py').read_text(encoding='utf-8')
    assert "_post_obs.get('plant_time_s')" in src
    assert "_current_obs.get('plant_time_s')" in src
    assert "phase in ('on_ground','touchdown')" in src
    assert "_plant_time_sample='POST_EXECUTION_LAYER6_NATIVE'" in src
    assert "_plant_time_sample='PASSIVE_BOUNDARY_CURRENT_LAYER6'" in src
    assert "op.get('simulation_time_s')" not in src
    assert "_bootstrap_ref=float(sim_now if sim_now is not None else req['reference_time_s'])" in src
    assert 'or not _bootstrap_clock_aligned' in src
    assert 'reference_time_s=_bootstrap_ref' in src
    assert "_bootstrap_obs.get('plant_time_s')" in src
    assert "ev['artifact_time_authority']='Layer6/GUI/MAVLink plant clock'" in src
    assert 'reference_time_s=_artifact_reference_time' in src
    assert 'sim_time_s=sim_now,next_capability=ncap' in src
    assert "'reference_time_is_physical_freshness_evidence':False" in src
    assert "rec['rtmo_operational_state_fresh']=bool(clock_state.get('state_fresh'))" in src
    assert "rec['rtmo_phase_transition']['status']='WAIT_PLANT_TIME'" in src
    assert "'reason':'rtmo_wait_plant_time'" in src
    assert 'phase_mgr.advance(sim_time_s=sim_now)' in src
