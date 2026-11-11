from types import SimpleNamespace

import tddt_tdoc.orchestrator as orchestrator_module
from tddt_tdoc.orchestrator import TDOCOrchestrator


class _Config:
    values = {
        "deadlines.layer4_s": 2.0,
        "deadlines.layer4_pre_release_s": 12.0,
    }

    def get(self, key):
        return self.values.get(key)


class _Layer4:
    def invoke(self, payload):
        return {"ok": True, "status": "OK", "result": {"verified_candidates": [{"candidate_id": "C1"}]}}


class _Ready:
    ready = True

    @staticmethod
    def to_dict():
        return {"ready": True}


def _orchestrator():
    obj = object.__new__(TDOCOrchestrator)
    obj.config = _Config()
    obj.adapters = {4: _Layer4()}
    obj._event = lambda *args, **kwargs: None
    return obj


def test_offline_layer4_full_horizon_uses_non_actuating_full_horizon_deadline(monkeypatch):
    ticks = iter((100.0, 110.5))
    monkeypatch.setattr(orchestrator_module.time, "perf_counter", lambda: next(ticks))

    out = _orchestrator()._invoke(
        4,
        "tx-offline",
        "Layer3.3-snapshot",
        {"snapshot": {}, "request": {"learning_mode": "OFFLINE_SE"}},
        "v1",
        "slow",
        _Ready(),
    )

    timing = out["orchestration_timing"]
    assert out["ok"] is True
    assert timing["deadline_key"] == "deadlines.layer4_pre_release_s"
    assert timing["deadline_s"] == 12.0
    assert timing["deadline_ok"] is True
    assert timing["timing_segment"] == "NON_ACTUATING_OFFLINE_FULL_HORIZON"
    assert timing["actuation_forbidden"] is True


def test_online_control_layer4_keeps_two_second_control_deadline(monkeypatch):
    ticks = iter((100.0, 102.5))
    monkeypatch.setattr(orchestrator_module.time, "perf_counter", lambda: next(ticks))

    out = _orchestrator()._invoke(
        4,
        "tx-online",
        "Layer3.3-snapshot",
        {"snapshot": {}, "request": {"learning_mode": "ONLINE_JSBSIM"}},
        "v1",
        "fast",
        _Ready(),
    )

    timing = out["orchestration_timing"]
    assert out["ok"] is False
    assert out["status"] == "DEADLINE_EXCEEDED"
    assert timing["deadline_key"] == "deadlines.layer4_s"
    assert timing["deadline_s"] == 2.0
    assert timing["timing_segment"] == "CONTROL_TRANSACTION"
    assert timing["actuation_forbidden"] is False
