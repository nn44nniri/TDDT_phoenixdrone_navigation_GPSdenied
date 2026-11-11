from tddt_tdoc.orchestrator import (
    _current_online_observation_contract,
    _post_execution_observation_contract,
)


def _online_state(t=10.0, released=False):
    return {
        'ok': True,
        'released': released,
        'ptlib_ingress': {
            'schema': 'TDOC-online-PTLIB-state-ingress-v2',
            'state': {'sim_time_s': t, 'latitude_deg': 40.01, 'longitude_deg': -105.30},
        },
    }


def _post(t=10.0, episode='ep1', cycle=1):
    return {
        'schema': 'TDOC-post-execution-observation-v1',
        'transaction_id': f'tx{cycle}',
        'episode_id': episode,
        'cycle_in_episode': cycle,
        'plant_time_s': t,
        'plant_time_present': True,
        'actuation_executed': True,
        'valid_for_next_cycle_correlation': True,
    }


def test_current_observation_without_prior_execution_is_visible_but_diagnostic_only():
    obs = _current_online_observation_contract(
        online_state=_online_state(40.22), transaction_id='tx2', episode_id='ep1', cycle_in_episode=2,
        previous_current_plant_time_s=None, previous_post_execution=None,
    )
    assert obs['plant_time_present'] is True
    assert obs['plant_time_monotonic'] is True
    assert obs['classification'] == 'CURRENT_DIAGNOSTIC_ONLY'
    assert obs['execution_correlated'] is False
    assert obs['fresh_for_control_state'] is False
    assert obs['navigation_authority'] == 'MESA_STD'
    assert obs['truth_injection_to_mesa'] is False
    assert obs['mesa_observation_allowed'] is False


def test_next_cycle_after_real_execution_is_execution_correlated_when_plant_time_is_monotonic():
    obs = _current_online_observation_contract(
        online_state=_online_state(10.2, released=True), transaction_id='tx2', episode_id='ep1', cycle_in_episode=2,
        previous_current_plant_time_s=10.0, previous_post_execution=_post(10.1, 'ep1', 1),
    )
    assert obs['execution_correlated'] is True
    assert obs['fresh_for_control_state'] is True
    assert obs['classification'] == 'EXECUTION_CORRELATED_CURRENT'
    assert obs['correlation_reason'] == 'correlated_to_previous_realized_execution'


def test_plant_time_regression_fails_closed_for_freshness():
    obs = _current_online_observation_contract(
        online_state=_online_state(9.5), transaction_id='tx2', episode_id='ep1', cycle_in_episode=2,
        previous_current_plant_time_s=10.0, previous_post_execution=_post(9.4, 'ep1', 1),
    )
    assert obs['plant_time_monotonic'] is False
    assert obs['execution_correlated'] is False
    assert obs['fresh_for_control_state'] is False
    assert obs['correlation_reason'] == 'plant_time_regression'


def test_old_nonadjacent_execution_is_not_reused_as_current_freshness():
    obs = _current_online_observation_contract(
        online_state=_online_state(13.0), transaction_id='tx4', episode_id='ep1', cycle_in_episode=4,
        previous_current_plant_time_s=12.0, previous_post_execution=_post(11.0, 'ep1', 1),
    )
    assert obs['plant_time_monotonic'] is True
    assert obs['execution_correlated'] is False
    assert obs['fresh_for_control_state'] is False
    assert obs['correlation_reason'] == 'prior_execution_not_immediately_preceding_cycle'


def test_post_execution_anchor_exists_only_for_realized_layer6_execution():
    held = _post_execution_observation_contract(
        layer6_output={'status':'HELD','actuation_executed':False}, transaction_id='h', episode_id='ep1', cycle_in_episode=1)
    assert held is None
    executed = _post_execution_observation_contract(
        layer6_output={
            'status':'EXECUTED','actuation_executed':True,
            'execution_evidence':{'io_snapshot':{'groups':{},'simulation_time_s':22.5}},
        }, transaction_id='e', episode_id='ep1', cycle_in_episode=1)
    assert executed is not None
    assert executed['plant_time_s'] == 22.5
    assert executed['valid_for_next_cycle_correlation'] is True
    assert executed['navigation_authority'] == 'MESA_STD'
    assert executed['truth_injection_to_mesa'] is False


def test_missing_plant_time_never_becomes_fresh():
    obs = _current_online_observation_contract(
        online_state={'ok':True,'released':True,'ptlib_ingress':{'state':{'latitude_deg':40.0}}},
        transaction_id='tx2', episode_id='ep1', cycle_in_episode=2,
        previous_current_plant_time_s=10.0, previous_post_execution=_post(10.0,'ep1',1),
    )
    assert obs['plant_time_present'] is False
    assert obs['execution_correlated'] is False
    assert obs['fresh_for_control_state'] is False
    assert obs['correlation_reason'] == 'plant_time_missing'
