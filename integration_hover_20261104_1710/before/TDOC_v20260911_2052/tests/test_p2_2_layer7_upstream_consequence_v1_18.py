from tddt_tdoc.orchestrator import _layer7_trigger_eligibility


def _exec_evidence(**extra):
    d={
        'schema':'TDOC-L6-online-execution-evidence-v5',
        'source':'Layer6/PT_LIB','destination':'Layer7',
        'validation_only':False,
    }
    d.update(extra); return d


def test_no_execution_evidence_is_upstream_consequence_not_layer7_root_cause():
    g=_layer7_trigger_eligibility(
        layer6_output={'status':'HELD','actuation_executed':False},
        execution_evidence=None,
        layer7_preflight={'ok':True},
        request={},
    )
    assert g['eligible'] is False
    assert g['status']=='NOT_INVOKED_UPSTREAM_EXECUTION'
    assert g['reason']=='upstream_execution_evidence_unavailable'
    assert g['consequence_only'] is True
    assert g['root_cause'] is False


def test_realized_execution_makes_layer7_eligible():
    g=_layer7_trigger_eligibility(
        layer6_output={'status':'EXECUTED','actuation_executed':True},
        execution_evidence=_exec_evidence(),
        layer7_preflight={'ok':True},
        request={},
    )
    assert g['eligible'] is True
    assert g['realized_execution'] is True
    assert g['reason']=='layer7_trigger_ready'


def test_explicit_observation_only_validation_remains_supported():
    ev={
        'schema':'TDOC-L6-online-observation-evidence-v2',
        'source':'Layer6/PT_LIB','destination':'Layer7','validation_only':True,
    }
    g=_layer7_trigger_eligibility(
        layer6_output={'status':'OBSERVATION_ONLY','actuation_executed':False},
        execution_evidence=ev,
        layer7_preflight={'ok':True},
        request={'online_observation_only':True},
    )
    assert g['eligible'] is True
    assert g['validation_observation'] is True
    assert g['realized_execution'] is False


def test_layer7_preflight_failure_after_real_execution_is_layer7_root_cause():
    g=_layer7_trigger_eligibility(
        layer6_output={'status':'EXECUTED','actuation_executed':True},
        execution_evidence=_exec_evidence(),
        layer7_preflight={'ok':False},
        request={},
    )
    assert g['eligible'] is False
    assert g['upstream_eligible'] is True
    assert g['status']=='HELD_LAYER7_NOT_READY'
    assert g['consequence_only'] is False
    assert g['root_cause'] is True
