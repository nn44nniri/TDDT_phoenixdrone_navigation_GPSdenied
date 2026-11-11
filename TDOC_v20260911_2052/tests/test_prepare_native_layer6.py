"""Preparation now validates native assets, without syncing retired PT_LIB resources."""
import importlib.util
from pathlib import Path


def load():
    root=Path(__file__).resolve().parents[1]
    spec=importlib.util.spec_from_file_location('prepare_native_assets',root/'scripts/prepare_all.py')
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def test_native_asset_preparation_is_read_only(tmp_path):
    m=load()
    for rel in ('ardupilot','jsbsim','models/phoenix_v3','phoenix_environment_adapter_v2'):
        (tmp_path/'FlightStack'/rel).mkdir(parents=True)
    before=sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*'))
    report={'stages':[],'_total':1,'ok':True}
    r=m.validate_layer6_flightstack(report,tmp_path)
    assert r['ok'] and r['runtime_started'] is False
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*'))==before


def test_missing_native_flightstack_fails_preparation(tmp_path):
    report={'stages':[],'_total':1,'ok':True}
    r=load().validate_layer6_flightstack(report,tmp_path)
    assert not r['ok'] and not report['ok'] and 'FlightStack/ardupilot' in r['missing']


def test_preparation_no_longer_has_execution_fmu_builder():
    m=load()
    assert not hasattr(m,'build_execution_fmus')
    assert not hasattr(m,'sync_ptlib_boulder_resources')
    assert 'TDDT_LAYER6_ROOT' in m.REQUIRED_ROOTS
    assert 'TDDT_PTLIB_ROOT' not in m.REQUIRED_ROOTS
