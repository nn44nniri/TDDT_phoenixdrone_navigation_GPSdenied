"""Retry an explicitly failed GUI connection within the existing deadline."""
from types import SimpleNamespace
from layer6.client import Layer6Client
import pytest


def test_failed_connect_is_retried_before_heartbeat_arrives(monkeypatch):
    client=Layer6Client.__new__(Layer6Client);calls=[]
    client.gui_status=lambda **kw:{'mavlink_connected':False}
    client.request_mavlink_connect=lambda:calls.append(True)
    times=iter([0.,2.,2.]);monkeypatch.setattr('layer6.client.time.monotonic',lambda:next(times))
    def wait(predicate,**kwargs):
        assert kwargs['timeout']==30.
        assert not predicate({'mavlink_connected':False,'mavlink_connecting':False,'mavlink_connection_error':'no heartbeat within timeout'})
        assert not predicate({'mavlink_connected':False,'mavlink_connecting':True,'mavlink_connection_error':None})
        ready={'mavlink_connected':True};assert predicate(ready);return ready
    client._gui=SimpleNamespace(wait_for=wait)
    assert client.ensure_mavlink(30.)['mavlink_connected']
    assert len(calls)==2


def test_missing_heartbeat_stays_fail_closed_without_duplicate_pending_connections():
    client=Layer6Client.__new__(Layer6Client);calls=[]
    client.gui_status=lambda **kw:{'mavlink_connected':False}
    client.request_mavlink_connect=lambda:calls.append(True)
    state={'mavlink_connected':False,'mavlink_connecting':True,'mavlink_connection_error':None}
    def wait(predicate,**kwargs):assert not predicate(state);return state
    client._gui=SimpleNamespace(wait_for=wait)
    from layer6.client import Layer6TimeoutError
    with pytest.raises(Layer6TimeoutError):client.ensure_mavlink(.01)
    assert len(calls)==1
