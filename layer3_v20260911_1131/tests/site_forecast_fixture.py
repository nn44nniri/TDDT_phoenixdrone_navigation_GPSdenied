"""Strict site integration test clock: observation only, never alters runtime forcing.

The old test fixture used an August 2026 fixed date. Operational cached forecast
rotates: requesting that date in October 2026 is correctly rejected by Climate.
The test-only epoch comes from the ACTUAL forecast file in the same deployment.
No extrapolation, silent clock correction, synthetic weather or gate bypass.
"""
from __future__ import annotations
import csv
import hashlib
import json
import os
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from pathlib import Path

LEGACY_TEST_EPOCH = "2026-08-24T11:00:00+00:00"


def _aware(t: str) -> datetime:
    ts = datetime.fromisoformat(str(t).strip().replace('Z', '+00:00'))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def effective_file(deps_root: Path | None) -> Path | None:
    raw = os.environ.get('TDDT_CLIMATE_FORCING_FILE', '').strip()
    if raw:
        p=Path(raw).expanduser().resolve()
        if not p.is_file(): raise RuntimeError(f'Explicit forcing not available: {p}')
        return p
    raw = os.environ.get('TDDT_CLIMATE_INTERNET_DATASETS_ROOT', '').strip()
    if raw:
        folder=Path(raw).expanduser().resolve()
        manifest=folder/'tdoc_active_forcing.json'
        if manifest.is_file():
            rel=str((json.loads(manifest.read_text(encoding='utf-8')) or {}).get('file') or '').strip()
            if not rel: raise RuntimeError(f'Active forcing manifest lacks file: {manifest}')
            q=Path(rel); p=(q if q.is_absolute() else folder/q).resolve()
            if not p.is_file(): raise RuntimeError(f'Active forcing manifest references missing data: {p}')
            return p
        p=folder/'openmeteo_24h.csv'
        if p.is_file(): return p
        raise RuntimeError(f'Central forcing missing: {p}')
    if deps_root is None: return None
    candidates=(deps_root/'internet_datasets/climate_twin/openmeteo_24h.csv',
                deps_root/'climate_twin_v20260911_0922/climate_dataset/openmeteo_24h.csv')
    for p in candidates:
        if p.is_file(): return p.resolve()
    raise RuntimeError(f'No real climate forcing for Layer3 site tests under {deps_root}; do not synthesize weather')


def covered_instant(path: Path, override: str | None = None) -> tuple[str, dict]:
    per_point: dict[str, list[datetime]] = {}
    with path.open(encoding='utf-8', newline='') as fh:
        reader=csv.DictReader(fh)
        if not reader.fieldnames or 'time' not in reader.fieldnames:
            raise RuntimeError(f'Climate forcing lacks time column: {path}')
        for row in reader:
            name=(row.get('point') or '').strip() or f"{row.get('latitude','')},{row.get('longitude','')}"
            per_point.setdefault(name, []).append(_aware(row['time']))
    if not per_point: raise RuntimeError(f'No weather points in actual forcing: {path}')
    # Every required point must cover the selected instant. Use the common
    # interior interval, not a manufactured timestamp beyond any point's data.
    start=max(min(xs) for xs in per_point.values())
    end=min(max(xs) for xs in per_point.values())
    if end <= start:
        raise RuntimeError(f'No common forecast interval across {len(per_point)} points: {path}')
    selected=_aware(override) if override else start + min(timedelta(hours=1), (end-start)/2)
    if not (start < selected < end):
        raise RuntimeError(f'Chosen site test time {selected.isoformat()} outside shared interior ({start.isoformat()}, {end.isoformat()})')
    # Test-time provenance is diagnostic, never evidence of flight authorization.
    return selected.isoformat(), {
        'purpose':'TEST_ONLY_VALID_FORECAST_DATE_NOT_RUNTIME_CLOCK_CHANGE',
        'forcing_file':str(path),'forcing_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'common_start_utc':start.isoformat(),'common_end_utc':end.isoformat(),
        'test_epoch_utc':selected.isoformat(),'point_count':len(per_point),
        'flight_authorized':False,
    }


@lru_cache(maxsize=1)
def test_epoch() -> str:
    raw=os.environ.get('L3_TEST_DEPS_ROOT','').strip()
    deps=Path(raw).resolve() if raw else None
    forcing=effective_file(deps)
    if forcing is None: return LEGACY_TEST_EPOCH
    epoch, _=covered_instant(forcing, os.environ.get('L3_TEST_CLIMATE_TIME_UTC') or None)
    return epoch
