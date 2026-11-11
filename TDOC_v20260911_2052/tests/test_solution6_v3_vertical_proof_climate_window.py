from __future__ import annotations
import importlib.util
from pathlib import Path
import csv

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'run_training_workflows.py'
spec = importlib.util.spec_from_file_location('solution6_rtw', SCRIPT)
rtw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rtw)


def _make_roots(tmp_path: Path):
    climate = tmp_path / 'climate'
    twins = tmp_path / 'twins'
    (climate / 'climate_dataset').mkdir(parents=True)
    (twins / 'config').mkdir(parents=True)
    forcing = climate / 'climate_dataset' / 'openmeteo_24h.csv'
    rows = [
        ('p1','2026-09-17T17:00',4.9),('p2','2026-09-17T17:00',5.1),
        ('p1','2026-09-17T18:00',2.0),('p2','2026-09-17T18:00',3.2),
        ('p1','2026-09-17T19:00',1.2),('p2','2026-09-17T19:00',1.8),
    ]
    with forcing.open('w', newline='') as f:
        w=csv.writer(f); w.writerow(['point','time','wind_speed_10m_ms']); w.writerows(rows)
    (climate / 'climate_dataset' / 'tdoc_active_forcing.json').write_text('{"file":"openmeteo_24h.csv"}')
    (twins / 'config' / 'phoenix_drone_v3.yaml').write_text('mission:\n  maximum_hover_wind_m_s: 5.0\n')
    return climate, twins


def test_auto_selects_real_safe_window_without_relaxing_constraint(tmp_path, monkeypatch):
    climate, twins = _make_roots(tmp_path)
    monkeypatch.setattr(rtw, 'resolve_climate_forcing', lambda root, require_exists=True: climate/'climate_dataset'/'openmeteo_24h.csv')
    epoch, meta = rtw._select_offline_vertical_proof_epoch(climate, twins, 320.0, mode='auto')
    assert epoch.isoformat() == '2026-09-17T18:00:00'
    assert meta['hover_limit_m_s'] == 5.0
    assert meta['source_wind_limit_m_s'] == 4.0
    assert meta['constraints_relaxed'] is False
    assert meta['synthetic_environment'] is False


def test_forcing_start_mode_does_not_claim_safe_selection(tmp_path, monkeypatch):
    climate, twins = _make_roots(tmp_path)
    monkeypatch.setattr(rtw, 'resolve_climate_forcing', lambda root, require_exists=True: climate/'climate_dataset'/'openmeteo_24h.csv')
    epoch, meta = rtw._select_offline_vertical_proof_epoch(climate, twins, 320.0, mode='forcing-start')
    assert epoch.isoformat() == '2026-09-17T17:00:00'
    assert meta['reason'] == 'forcing_start_requested'
    assert meta['synthetic_environment'] is False


def test_effective_epoch_override_preserves_true_forcing_window(monkeypatch):
    monkeypatch.setattr(rtw, '_layer3_mission_epoch_utc', lambda: None)
    from datetime import datetime
    start=datetime.fromisoformat('2026-09-17T17:00:00')
    end=datetime.fromisoformat('2026-09-18T16:00:00')
    override=datetime.fromisoformat('2026-09-17T18:00:00')
    t, meta=rtw._climate_time_for_elapsed((start,end), 7.0, return_metadata=True, effective_epoch_override=override)
    assert t.isoformat() == '2026-09-17T18:00:07'
    assert meta['forcing_start'] == '2026-09-17T17:00:00'
    assert meta['effective_climate_epoch'] == '2026-09-17T18:00:00'
    assert meta['basis'] == 'offline_selected_vertical_proof_safe_forcing_epoch'
