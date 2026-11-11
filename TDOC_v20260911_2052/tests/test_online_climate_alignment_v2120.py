from __future__ import annotations
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

ROOT=Path(__file__).parents[1]
spec=importlib.util.spec_from_file_location("tdoc_training_workflow_v2120",ROOT/"scripts/run_training_workflows.py")
mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)


def test_online_climate_time_uses_layer3_epoch_not_forcing_start(monkeypatch):
    # Regression for the ONLINE CoC deadlock: forcing begins 09:00 while Layer-3's
    # authoritative mission epoch is 11:00.  The climate sample must be stamped at
    # 11:00 + simulator elapsed, otherwise Layer-3 sees it as ~7200 s stale.
    start=datetime(2026,8,24,9,0,0)
    end=datetime(2026,8,25,8,0,0)
    epoch=datetime(2026,8,24,11,0,0)
    monkeypatch.setattr(mod,"_layer3_mission_epoch_utc",lambda: epoch)
    req={"climate":{},"context":{}}
    t=mod._apply_online_climate_time(req,(start,end),90.0)
    assert t==datetime(2026,8,24,11,1,30)
    assert req["climate"]["time"]["value"].startswith("2026-08-24T11:01:30")
    assert req["context"]["online_climate_mission_epoch"]=="2026-08-24T11:00:00"
    assert req["context"]["climate_time_alignment_basis"]=="Layer3 mission_epoch_utc + ONLINE simulator elapsed_s"


def test_online_climate_time_fails_closed_outside_forcing(monkeypatch):
    start=datetime(2026,8,24,9,0,0)
    end=datetime(2026,8,25,8,0,0)
    monkeypatch.setattr(mod,"_layer3_mission_epoch_utc",lambda: datetime(2026,8,26,11,0,0))
    with pytest.raises(RuntimeError,match="outside active ONLINE forcing window"):
        mod._apply_online_climate_time({"climate":{},"context":{}},(start,end),0.0)


def test_online_climate_time_does_not_clamp_end(monkeypatch):
    start=datetime(2026,8,24,9,0,0)
    end=datetime(2026,8,24,12,0,0)
    monkeypatch.setattr(mod,"_layer3_mission_epoch_utc",lambda: datetime(2026,8,24,11,0,0))
    with pytest.raises(RuntimeError,match="will not clamp Online climate time"):
        mod._apply_online_climate_time({"climate":{},"context":{}},(start,end),7201.0)
