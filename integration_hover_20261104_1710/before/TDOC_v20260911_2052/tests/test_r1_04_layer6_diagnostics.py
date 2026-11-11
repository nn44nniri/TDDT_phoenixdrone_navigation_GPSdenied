"""Failure detail must survive preflight, workflow summary and opt-in logging."""
import ast
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter
from tddt_tdoc.diagnostics import diagnostic_session, online_preflight_diagnostics
from tddt_tdoc.layer6_diagnostics import CLIENT_CALLS, inspect_client_api

TDOC = Path(__file__).resolve().parents[1]
NATIVE = TDOC.parent / 'layer6_v20261007_1032'


def forbidden(*args, **kwargs):
    raise AssertionError('No constructor, socket or API execution allowed in preflight')


class Config:
    def __init__(self, package_root, native_root, **options):
        self.package_root, self.native_root, self.options = package_root, native_root, options

    def path_value(self, key):
        assert key == 'TDDT_LAYER6_ROOT'
        return self.native_root

    def get(self, key, default=None):
        return self.options.get(key, default)


@pytest.fixture
def ready(tmp_path, monkeypatch):
    root = tmp_path / 'layer6_v20261007_1032'
    root.mkdir()
    for name in ['src/layer6/client.py', 'src/layer6/contracts.py', 'src/layer6/validation.py',
                 'src/layer6/feedback.py', 'src/run_flightstack.py', 'scripts/run_layer6_stack.sh']:
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    for name in ['FlightStack/ardupilot', 'FlightStack/jsbsim', 'FlightStack/models/phoenix_v3']:
        (root/name).mkdir(parents=True)
    (root/'pyproject.toml').write_text('[project]\nversion="2026.10.7.0937"\n')
    cls = type('CompatibleClient', (), {name: forbidden for name in CLIENT_CALLS})
    adapter = Layer6OnlineAdapter(Config(tmp_path/'TDOC', root), None)
    monkeypatch.setattr(adapter, '_module', lambda: SimpleNamespace(Layer6Client=cls))
    return adapter, cls, root


def test_real_preflight_accepts_cancel_shape_without_starting_runtime(monkeypatch):
    import socket
    monkeypatch.setattr(socket, 'socket', forbidden)
    report = Layer6OnlineAdapter(Config(TDOC, NATIVE), None).preflight()
    probe = report['layer6_client']
    assert report['status'] == 'READY'
    assert report['failure_reasons'] == []
    assert probe['missing_api'] == []
    assert probe['signature_mismatches'] == []
    assert probe['actual_version'] == probe['expected_version'] == '2026.10.7.0937'
    assert not probe['runtime_started'] and not report['flight_ready'] and not report['actuation_executed']


def test_complete_signature_surface_passes_only_package_readiness(ready):
    adapter, cls, root = ready
    result = adapter.preflight()
    assert result['ok'] and result['online_ready']
    assert result['status'] == 'READY'
    assert result['readiness_scope'].startswith('PACKAGE_API_ONLY')
    assert not result['flight_ready'] and not result['actuation_executed']
    assert adapter._client is None and not adapter._session_open


def test_missing_cancel_is_not_hidden_by_matching_version(ready):
    adapter, cls, root = ready
    del cls.cancel
    probe = adapter.preflight()['layer6_client']
    assert probe['version_match'] and not probe['ok']
    assert probe['missing_api'] == ['cancel']


def test_version_mismatch_is_explicit_even_when_all_api_matches(ready):
    adapter, cls, root = ready
    (root/'pyproject.toml').write_text('[project]\nversion="wrong-release"\n')
    result = adapter.preflight()
    probe = result['layer6_client']
    assert probe['api_ok'] and not result['ok']
    assert probe['expected_version'] == '2026.10.7.0937' and probe['actual_version'] == 'wrong-release'
    assert probe['failures'] == [{'code':'version_mismatch','expected':'2026.10.7.0937','actual':'wrong-release'}]


def test_all_independent_failures_are_reported_together(ready):
    adapter, cls, root = ready
    del cls.cancel
    cls.gui_status = lambda self, timeout=.8: forbidden()
    (root/'pyproject.toml').write_text('[project]\nversion="old"\n')
    result = adapter.preflight()
    assert result['failure_reasons'] == ['version_mismatch', 'missing_api', 'signature_mismatch']
    assert result['layer6_client']['actual_version'] == 'old'


def test_non_callable_api_is_missing_and_missing_client_class_is_explicit(ready, monkeypatch):
    adapter, cls, root = ready
    cls.cancel = None
    assert adapter.preflight()['layer6_client']['missing_api'] == ['cancel']
    monkeypatch.setattr(adapter, '_module', lambda: SimpleNamespace())
    result = adapter.preflight()['layer6_client']
    assert result['missing_api'] == ['Layer6Client'] and not result['api_inspected']


def test_import_exception_retains_stage_version_and_error(ready, monkeypatch):
    adapter, cls, root = ready
    def fail():
        raise ImportError('broken_dep')
    monkeypatch.setattr(adapter, '_module', fail)
    probe = adapter.preflight()['layer6_client']
    assert probe['failure_reasons'] == ['api_import_error']
    assert probe['error_stage'] == 'api' and 'broken_dep' in probe['error']
    assert probe['expected_version'] == probe['actual_version']
    assert not probe['api_inspected'] and probe['missing_api'] == []


@pytest.mark.parametrize('text', [None, '[project]\nversion=', '[project]\nname="no version"\n'])
def test_bad_metadata_is_not_hidden_by_other_valid_api(ready, text):
    adapter, cls, root = ready
    path = root/'pyproject.toml'
    if text is None:
        path.unlink()
    else:
        path.write_text(text)
    probe = adapter.preflight()['layer6_client']
    assert probe['api_ok'] and not probe['ok']
    assert probe['actual_version'] is None and probe['expected_version'] == '2026.10.7.0937'
    assert probe['failure_reasons'] == ['version_metadata_error']
    assert probe['failures'][0]['error']


def test_missing_asset_is_reported_even_when_api_and_version_match(ready):
    adapter, cls, root = ready
    (root/'FlightStack/jsbsim').rmdir()
    result = adapter.preflight()['layer6_client']
    assert result['missing'] == ['FlightStack/jsbsim'] and not result['ok']
    assert result['failure_reasons'] == ['missing_package_paths']


def test_missing_root_has_expected_version_but_no_invented_actual(tmp_path):
    probe = Layer6OnlineAdapter(Config(tmp_path, None), None).preflight()['layer6_client']
    assert probe['failure_reasons'] == ['package_root_unavailable']
    assert probe['actual_version'] is None and probe['expected_version'] == '2026.10.7.0937'
    assert not probe['api_inspected']


def test_wrong_package_name_is_rejected_without_import(ready, monkeypatch):
    adapter, cls, root = ready
    adapter.config.options['layers.layer6.online.expected_package'] = 'other-release'
    monkeypatch.setattr(adapter, '_module', forbidden)
    probe = adapter.preflight()['layer6_client']
    assert probe['failure_reasons'] == ['package_root_mismatch']
    assert probe['expected_package'] == 'other-release' and probe['actual_package'] == root.name
    assert not probe['api_inspected']


def test_non_external_package_is_rejected_without_import(ready, monkeypatch):
    adapter, cls, root = ready
    adapter.config.package_root = root.parent
    monkeypatch.setattr(adapter, '_module', forbidden)
    probe = adapter.preflight()['layer6_client']
    assert probe['failure_reasons'] == ['layer6_must_be_external']
    assert not probe['external_only'] and not probe['api_inspected']


def test_module_origin_failure_identifies_expected_and_actual(ready, monkeypatch, tmp_path):
    adapter, cls, root = ready
    # Exercise the real origin guard without importing or executing a shadow module.
    import tddt_tdoc.adapters.layer6 as module
    monkeypatch.setattr(adapter, '_module', Layer6OnlineAdapter._module.__get__(adapter))
    monkeypatch.setattr(sys, 'path', list(sys.path))
    wrong = tmp_path/'shadow/client.py'
    monkeypatch.setattr(module.importlib, 'import_module', lambda name: SimpleNamespace(__file__=str(wrong)))
    probe = adapter.preflight()['layer6_client']
    assert probe['failure_reasons'] == ['module_origin_mismatch']
    failure, = probe['failures']
    assert failure['actual_origin'] == str(wrong) and failure['expected_under'] == str(root)
    assert failure['module'] == 'layer6'


def test_uninspectable_signature_fails_closed(ready):
    adapter, cls, root = ready
    cls.cancel = staticmethod(dict.update)
    probe = adapter.preflight()['layer6_client']
    assert probe['failure_reasons'] == ['api_inspection_error']
    assert probe['api_inspection_errors'][0]['method'] == 'cancel'


def test_required_keyword_cannot_hide_an_incompatible_call_that_omits_it(ready):
    adapter, cls, root = ready
    cls.gui_status = lambda self, *, timeout, include_imu_samples: forbidden()
    result = adapter.preflight()['layer6_client']
    assert any(row['method']=='gui_status' and 'missing a required argument' in row['error'] for row in result['signature_mismatches'])


def test_diagnostic_shapes_cover_every_r103_production_call():
    path = TDOC/'scripts/audit_r1_03_contract.py'
    spec = importlib.util.spec_from_file_location('r103_coverage', path)
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    names = set()
    for path in [TDOC/'src/tddt_tdoc/adapters/layer6.py', TDOC/'src/tddt_tdoc/layer6_bridge.py']:
        for name, caller, call in audit.client_calls(ast.parse(path.read_text())):
            names.add(name)
            assert any(len(pos)==len(call.args) and set(keys)=={k.arg for k in call.keywords}
                       for pos, keys in CLIENT_CALLS[name]), (name, caller, ast.unparse(call))
    assert names == set(CLIENT_CALLS)


def test_console_summary_and_opt_in_debug_log_keep_real_reasons(ready, tmp_path, monkeypatch):
    adapter, cls, root = ready
    del cls.cancel
    cls.gui_status = lambda self, timeout=.8: forbidden()
    monkeypatch.delenv('TDDT_DIAGNOSTIC_LOG', raising=False)
    path = tmp_path/'debug.log'
    cfg = SimpleNamespace(runtime_root=tmp_path, package_root=tmp_path)
    with diagnostic_session(cfg, SimpleNamespace(debug_log=str(path), cmd='nav-online')):
        report = adapter.preflight()
        summary = online_preflight_diagnostics({'adapters': {'6':report}})
        # This is also the workflow console/JSON payload.
        print(json.dumps(summary))
    assert summary['layer6_missing_api'] == ['cancel']
    assert summary['layer6_signature_mismatches'][0]['method'] == 'gui_status'
    assert summary['layer6_expected_version'] == summary['layer6_actual_version']
    text = path.read_text()
    assert 'layer6.preflight' in text and 'missing_api' in text and 'include_imu_samples' in text
    assert 'expected_version' in text and 'actual_version' in text


def test_workflow_failure_branch_preserves_details_and_returns_two(ready, tmp_path):
    adapter, cls, root = ready
    del cls.cancel
    cls.gui_status = lambda self, timeout=.8: forbidden()
    preflight = {'adapters':{'6':adapter.preflight()}, 'execution_modes':{'ONLINE_JSBSIM':False}}
    tree = ast.parse((TDOC/'scripts/run_training_workflows.py').read_text())
    branch = next(n for n in ast.walk(tree) if isinstance(n, ast.If) and
                  'need_offline and (not offline_ready)' in ast.unparse(n.test) and
                  'need_online and (not online_ready)' in ast.unparse(n.test))
    # Run the actual refusal branch in isolation; never construct an orchestrator.
    fn = ast.FunctionDef(name='refuse', args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
                         body=[branch], decorator_list=[])
    saved, events = [], []
    namespace = {'need_offline':False,'offline_ready':True,'need_online':True,'online_ready':False,
                 'overall':{},'pf':preflight,'report_root':tmp_path,'args':SimpleNamespace(online_training=True),
                 'time':__import__('time'),'json':json,'online_preflight_diagnostics':online_preflight_diagnostics,
                 'diagnostic_event':lambda name,**kw:events.append((name,kw)),
                 '_write_workflow_summary':lambda value,*args,**kw:saved.append(json.loads(json.dumps(value)))}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), '<workflow-refusal>', 'exec'), namespace)
    assert namespace['refuse']() == 2
    assert saved[0]['online_preflight_diagnostics']['layer6_missing_api'] == ['cancel']
    assert saved[0]['online_preflight_diagnostics']['layer6_signature_mismatches'][0]['method'] == 'gui_status'
    assert events[0][0] == 'online.preflight.failed'


def test_no_opt_in_log_does_not_create_files(ready, tmp_path, monkeypatch, capsys):
    adapter, cls, root = ready
    monkeypatch.delenv('TDDT_DIAGNOSTIC_LOG', raising=False)
    before = {p.relative_to(tmp_path) for p in tmp_path.rglob('*')}
    adapter.preflight()
    assert {p.relative_to(tmp_path) for p in tmp_path.rglob('*')} == before
    assert not capsys.readouterr().err
