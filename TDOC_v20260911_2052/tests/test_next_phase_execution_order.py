from test_online_climate_handoff_alignment_v2052 import mod as workflow
import pytest


@pytest.mark.parametrize('status,executed,l7,allowed',[
    ('HELD',False,'NOT_INVOKED',False),('FAILED',False,'FAILED',False),
    ('EXECUTED',False,'OK',False),('EXECUTED',True,'REJECTED',False),
    ('EXECUTED',True,'OK',True)])
def test_controlled_phase_cannot_replace_its_artifact_before_closed_cycle(status,executed,l7,allowed):
    assert workflow._next_phase_bootstrap_allowed('vertical_takeoff',
        {'status':status,'actuation_executed':executed},l7) is allowed


def test_ground_boundary_still_qualifies_takeoff_without_actuation():
    assert workflow._next_phase_bootstrap_allowed('on_ground',{},None)
