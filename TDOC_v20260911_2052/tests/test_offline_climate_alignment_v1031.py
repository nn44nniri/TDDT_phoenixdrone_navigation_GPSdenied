from __future__ import annotations
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

ROOT=Path(__file__).parents[1]
spec=importlib.util.spec_from_file_location("tdoc_training_workflow_timefix",ROOT/"scripts/run_training_workflows.py")
mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)


def test_offline_uses_layer3_epoch_when_inside_forcing(monkeypatch):
    start=datetime(2026,8,24,9,0,0); end=start+timedelta(hours=23)
    monkeypatch.setattr(mod,"_layer3_mission_epoch_utc",lambda: datetime(2026,8,24,11,0,0,tzinfo=timezone.utc))
    t,meta=mod._climate_time_for_elapsed((start,end),90.0,return_metadata=True)
    assert t==datetime(2026,8,24,11,1,30)
    assert meta["basis"]=="layer3_mission_epoch_utc"
    assert meta["epoch_rebased_to_forcing_start"] is False


def test_offline_rebases_to_active_forcing_when_layer3_epoch_is_outside(monkeypatch):
    start=datetime(2026,9,7,10,0,0); end=datetime(2026,9,8,9,0,0)
    monkeypatch.setattr(mod,"_layer3_mission_epoch_utc",lambda: datetime(2026,8,24,11,0,0,tzinfo=timezone.utc))
    t,meta=mod._climate_time_for_elapsed((start,end),0.0,return_metadata=True)
    assert t==start
    assert meta["basis"]=="active_offline_forcing_window_start"
    assert meta["epoch_rebased_to_forcing_start"] is True
    assert meta["configured_layer3_epoch"].startswith("2026-08-24T11:00:00")


def test_offline_rebased_clock_advances_with_mission_elapsed(monkeypatch):
    start=datetime(2026,9,7,10,0,0); end=datetime(2026,9,8,9,0,0)
    monkeypatch.setattr(mod,"_layer3_mission_epoch_utc",lambda: datetime(2026,8,24,11,0,0,tzinfo=timezone.utc))
    t,meta=mod._climate_time_for_elapsed((start,end),200.0,return_metadata=True)
    assert t==datetime(2026,9,7,10,3,20)
    assert meta["elapsed_s"]==200.0


def test_offline_still_fails_closed_when_elapsed_exceeds_forcing_horizon(monkeypatch):
    start=datetime(2026,9,7,10,0,0); end=start+timedelta(seconds=10)
    monkeypatch.setattr(mod,"_layer3_mission_epoch_utc",lambda: datetime(2026,8,24,11,0,0,tzinfo=timezone.utc))
    with pytest.raises(RuntimeError,match="Increase/refresh the forcing horizon"):
        mod._climate_time_for_elapsed((start,end),11.0)
