from pathlib import Path
from tddt_tdoc.climate_forcing import forcing_filename, resolve_climate_forcing, write_active_manifest, write_central_active_manifest


def test_forcing_filename_and_manifest_select_custom_horizon(tmp_path,monkeypatch):
    climate=tmp_path/'climate'; ds=climate/'climate_dataset'; ds.mkdir(parents=True)
    p24=ds/'openmeteo_24h.csv'; p72=ds/'openmeteo_72h.csv'; p24.write_text('x'); p72.write_text('x')
    assert forcing_filename(72)=='openmeteo_72h.csv'
    monkeypatch.setenv('TDDT_CLIMATE_INTERNET_DATASETS_ROOT',str(tmp_path/'no-central-cache'))
    assert resolve_climate_forcing(climate,require_exists=True)==p24.resolve()
    write_active_manifest(climate,p72,hours=72)
    assert resolve_climate_forcing(climate,require_exists=True)==p72.resolve()
    monkeypatch.setenv('TDDT_CLIMATE_FORCING_FILE','openmeteo_24h.csv')
    assert resolve_climate_forcing(climate,require_exists=True)==p24.resolve()


def test_prepare_all_uses_horizon_specific_output_and_active_manifest():
    text=Path('scripts/prepare_all.py').read_text(encoding='utf-8')
    assert "forcing_filename(a.climate_hours)" in text
    assert "write_central_active_manifest(forcing,hours=a.climate_hours)" in text
    assert "TDDT_CLIMATE_INTERNET_DATASETS_ROOT" in text
    assert "'--hours',str(a.climate_hours)" in text


def test_missing_authoritative_override_does_not_silently_fallback(tmp_path,monkeypatch):
    climate=tmp_path/'climate'; ds=climate/'climate_dataset'; ds.mkdir(parents=True)
    (ds/'openmeteo_24h.csv').write_text('x')
    monkeypatch.setenv('TDDT_CLIMATE_FORCING_FILE','openmeteo_72h.csv')
    assert resolve_climate_forcing(climate,require_exists=True) is None


def test_central_climate_cache_resolves_without_library_copy(tmp_path,monkeypatch):
    climate=tmp_path/'climate'; (climate/'climate_dataset').mkdir(parents=True)
    central=tmp_path/'internet_datasets/climate'; central.mkdir(parents=True)
    forcing=central/'openmeteo_24h.csv'; forcing.write_text('x')
    monkeypatch.setenv('TDDT_CLIMATE_INTERNET_DATASETS_ROOT',str(central))
    write_central_active_manifest(forcing,hours=24)
    assert resolve_climate_forcing(climate,require_exists=True)==forcing.resolve()
