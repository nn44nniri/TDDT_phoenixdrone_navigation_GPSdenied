from __future__ import annotations
import csv
from pathlib import Path
import pytest
from site_forecast_fixture import covered_instant


def _fake_file(tmp_path):
    p=tmp_path/'fixture_test_only_weather.csv'
    with p.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=['point','time','latitude','longitude']);w.writeheader()
        for point in ('a','b'):
            for hour in range(4):
                w.writerow({'point':point,'time':f'2026-10-09T{12+hour:02d}:00:00',
                            'latitude':40.0,'longitude':-105.0})
    return p


def test_selects_real_common_covered_interior(tmp_path):
    epoch, evidence=covered_instant(_fake_file(tmp_path))
    assert epoch=='2026-10-09T13:00:00+00:00'
    assert evidence['point_count']==2
    assert evidence['forcing_sha256']
    assert evidence['flight_authorized'] is False


def test_rejects_override_outside_coverage(tmp_path):
    with pytest.raises(RuntimeError,match='outside shared interior'):
        covered_instant(_fake_file(tmp_path),'2026-08-24T11:00:00+00:00')


def test_rejects_empty_or_missing_file(tmp_path):
    p=tmp_path/'empty.csv';p.write_text('point,time\n')
    with pytest.raises(RuntimeError,match='No weather points'):
        covered_instant(p)


def test_rejects_nonoverlapping_weather_points(tmp_path):
    p=tmp_path/'nonoverlap.csv'
    p.write_text('point,time\na,2026-10-09T12:00:00\na,2026-10-09T13:00:00\nb,2026-10-09T14:00:00\nb,2026-10-09T15:00:00\n')
    with pytest.raises(RuntimeError,match='No common forecast interval'):
        covered_instant(p)
