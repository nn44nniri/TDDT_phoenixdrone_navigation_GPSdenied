import importlib.util
import json
import os
import pytest
from pathlib import Path

TDOC = Path(__file__).resolve().parents[1]
SCRIPT = TDOC / 'scripts' / 'run_training_workflows.py'
_spec = importlib.util.spec_from_file_location('solution8_training_workflow', SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


@pytest.fixture(autouse=True)
def current_bundle_roots(monkeypatch):
    root=TDOC.parent
    for key,rel in {'TDDT_INTEGRATED_ROOT':'.','TDDT_LAYER4_ROOT':'layer4_v20260907_0608',
                    'TDDT_LAYER5_ROOT':'layer5_v20260911_1510',
                    'TDDT_LAYER6_ROOT':'layer6_v20261007_1032'}.items():
        monkeypatch.setenv(key,str((root/rel).resolve()))
    monkeypatch.delenv('TDDT_PTLIB_ROOT',raising=False)
    monkeypatch.delenv('EXT_FMU_SIM_ROOT',raising=False)


def test_solution8_current_run_provenance_is_complete(tmp_path, monkeypatch):
    report = tmp_path / 'reports'
    runtime = tmp_path / 'runtime'
    prov = _mod._pin_run_provenance(report, runtime)
    assert prov['ok'] is True
    assert prov['release_identity_match'] is True
    assert prov['bundle_release'] == TDOC.parent.name
    for key in ('root_manifest_sha256','TDOC_source_hash','Layer4_source_hash','Layer5_source_hash','absolute_execution_root'):
        assert prov.get(key)
    assert prov['Layer6_source_hash']
    assert prov['execution_transport']=='Layer6Client_GUI_MAVLink'
    assert prov['root_manifest_error'] is None
    assert prov['historical_absolute_paths_are_current_authority'] is False
    assert (report/'provenance'/'RUN_PROVENANCE.json').is_file()
    assert (runtime/'provenance'/'run_provenance.json').is_file()


def test_solution8_release_manifest_mismatch_is_fail_closed(tmp_path, monkeypatch):
    fake = tmp_path / 'TDDT_integrated_fix_fake_solution8_fixed_v3'
    fake.mkdir()
    (fake/'MANIFEST.json').write_text(json.dumps({'release':'TDDT_integrated_fix_old_solution4_fixed_v2'}))
    monkeypatch.setenv('TDDT_INTEGRATED_ROOT', str(fake))
    prov = _mod._pin_run_provenance(tmp_path/'r', tmp_path/'u')
    assert prov['ok'] is False
    assert prov['release_identity_match'] is False
    assert 'bundle_release_manifest_release_mismatch' in prov['blockers']


def test_solution8_root_manifest_release_matches_extracted_root():
    root = TDOC.parent
    manifest = json.loads((root/'MANIFEST.json').read_text())
    assert manifest['release'] == root.name
    assert manifest['release'] == TDOC.parent.name


def test_solution8_current_authority_files_do_not_name_solution4_v2():
    root = TDOC.parent
    authority = [
        root/'MANIFEST.json', root/'PROVENANCE_POLICY.json',
        TDOC/'configs'/'environment.sh', TDOC/'scripts'/'run_training_workflows.py'
    ]
    for p in authority:
        assert 'solution4_fixed_v2' not in p.read_text(errors='ignore')


def test_missing_root_manifest_remains_fail_closed(tmp_path,monkeypatch):
    fake=tmp_path/'bundle'; fake.mkdir()
    monkeypatch.setenv('TDDT_INTEGRATED_ROOT',str(fake))
    prov=_mod._pin_run_provenance(tmp_path/'reports',tmp_path/'runtime')
    assert not prov['ok']
    assert 'root_manifest_missing' in prov['blockers']
    assert not (fake/'MANIFEST.json').exists()


@pytest.mark.parametrize('content',['{broken','[]','{"release":null}'])
def test_invalid_root_manifest_remains_fail_closed(tmp_path,monkeypatch,content):
    fake=tmp_path/'bundle'; fake.mkdir(); (fake/'MANIFEST.json').write_text(content)
    monkeypatch.setenv('TDDT_INTEGRATED_ROOT',str(fake))
    prov=_mod._pin_run_provenance(tmp_path/'reports',tmp_path/'runtime')
    assert not prov['ok']
    assert 'root_manifest_invalid' in prov['blockers']
