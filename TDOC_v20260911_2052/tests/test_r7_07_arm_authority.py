"""A healthy transport/prior and declarative flags must not become an ARM decision."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tddt_tdoc.adapters.layer5 import Layer5Adapter
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter


def adapter(readiness, *, bootstrap=True):
    a=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter)
    a.layer5=SimpleNamespace(online_control_readiness=lambda:deepcopy(readiness))
    a.prepare_online_transport_session=lambda:{'ok':True,'ready':True,'startup_actuation':False}
    a._option=lambda key,default=None:bootstrap if key=='ground_arm_bootstrap' else default
    def forbidden():
        raise AssertionError('session readiness may not emit any command')
    a._prepare_ground_arm=forbidden
    a._get_client=forbidden
    return a


@pytest.mark.parametrize('value',[None,False,1,'true',{},[]])
def test_nominal_prediction_readiness_must_be_explicit_true(value):
    a=adapter({'ok':True,'nominal_control_ready':value,'flight_release_blockers':[]})
    out=a.prepare_online_control_session()
    assert out['ok'] is False and out['transport_ready'] is True
    assert out['reason']=='layer5_nominal_control_not_ready'
    assert out['startup_actuation'] is False


@pytest.mark.parametrize('bootstrap',[True,False])
def test_even_declared_prediction_readiness_does_not_emit_startup_arm(bootstrap):
    # Explicit unit fixture for infrastructure handling, not provider qualification.
    a=adapter({'ok':True,'nominal_control_ready':True,'flight_release_blockers':[]},bootstrap=bootstrap)
    out=a.prepare_online_control_session()
    assert out['ok'] is True and out['startup_actuation'] is False
    if bootstrap:
        assert out['ground_arm']['status']=='DEFERRED_TO_CURRENT_D_EXEC'


def test_nominal_readiness_cannot_override_a_provider_blocker():
    a=adapter({'ok':True,'nominal_control_ready':True,
               'flight_release_blockers':['provider_scope_forbids_ARM']})
    out=a.prepare_online_control_session()
    assert not out['ok'] and out['reason']=='provider_scope_forbids_ARM'


def test_retired_arm_path_rejects_even_if_a_caller_injects_final():
    a=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter)
    a.layer5=SimpleNamespace(native_ground_arm_decision=lambda *a,**kw:{
        'ok':True,'gates':{'FINAL':True},'discrete':['ARM']})
    with pytest.raises(RuntimeError,match='ground_arm_requires_current_verified_D_exec'):
        a._prepare_ground_arm()


def test_ground_arm_api_has_no_candidate_or_certificate_input():
    # Keep the limitation explicit: it cannot construct execution evidence out of status.
    import inspect
    assert list(inspect.signature(Layer5Adapter.native_ground_arm_decision).parameters)==[
        'self','status','transport_ready']
