from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location(
        "offline_cycle_auditor", ROOT / "scripts" / "audit_offline_cycle_contract.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_auditor_uses_centralized_offline_full_horizon_deadline():
    contract = _module()._offline_layer4_timing_contract(ROOT)
    assert contract == {
        "timing_segment": "NON_ACTUATING_OFFLINE_FULL_HORIZON",
        "deadline_key": "deadlines.layer4_pre_release_s",
        "deadline_s": 30.0,
        "actuation_forbidden_by_segment": True,
    }
