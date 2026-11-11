"""Audit the real boundary statically; never execute Client, GUI or a command."""
import ast
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TDOC = 'TDOC_v20260911_2052'
CONTRACT = Path(TDOC) / 'docs/contracts/TDOC_LAYER6_API_STATUS_CONTRACT_v1.json'


@pytest.fixture
def m():
    path = ROOT / TDOC / 'scripts/audit_r1_03_contract.py'
    spec = importlib.util.spec_from_file_location('r103_audit', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bundle(tmp_path):
    contract = json.loads((ROOT / CONTRACT).read_text())
    for relative in [str(CONTRACT), *contract['sources'].values()]:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    return tmp_path


def test_current_api_surface_is_compatible_without_flight_release(m):
    result = m.audit(ROOT, ROOT / CONTRACT)
    rows = {r['method']: r for r in result['api']}
    assert result['audit_status'] == 'PASS'
    assert result['api_signature_compatibility'] == 'PASS'
    assert rows['cancel']['status'] == 'CALL_SHAPE_COMPATIBLE'
    assert rows['gui_status']['status'] == 'CALL_SHAPE_COMPATIBLE'
    bad = [r for r in rows['gui_status']['calls'] if r['status'] == 'SIGNATURE_MISMATCH']
    assert bad == []
    assert result['flight_ready'] is False and result['runtime_started'] is False


@pytest.mark.parametrize('definition,call,expected', [
    ('def f(self, value, /, *, timeout=1): pass', 'client.f(value=3)', 'SIGNATURE_MISMATCH'),
    ('def f(self, value, /, *, timeout=1): pass', 'client.f(3, timeout=2)', 'CALL_SHAPE_COMPATIBLE'),
    ('def f(self, *, required): pass', 'client.f()', 'SIGNATURE_MISMATCH'),
    ('def f(self, *, timeout=1): pass', 'client.f(1)', 'SIGNATURE_MISMATCH'),
    ('def f(self, **kwargs): pass', 'client.f(include_imu_samples=True)', 'CALL_SHAPE_COMPATIBLE'),
    ('def f(self, **kwargs): pass', 'client.f(**unknown)', 'UNRESOLVED_CALL'),
])
def test_signature_rules_are_bound_without_execution(m, definition, call, expected):
    node = ast.parse(definition).body[0]
    invocation = ast.parse(call).body[0].value
    assert m.check_call(node, invocation)['status'] == expected


def test_signature_repair_alone_does_not_make_status_or_flight_valid(m, bundle):
    path = bundle / 'layer6_v20261007_1032/src/layer6/client.py'
    text = path.read_text().replace('class Layer6Client:\n',
        'class Layer6Client:\n    def cancel(self, *, transaction_id=None, reason="TDOC_HOLD"):\n        raise NotImplementedError\n')
    text = text.replace('def gui_status(self, timeout: float = 0.8)',
                        'def gui_status(self, timeout: float = 0.8, *, include_imu_samples=False)')
    path.write_text(text)
    result = m.audit(bundle, bundle / CONTRACT)
    assert result['api_signature_compatibility'] == 'PASS'
    assert next(r for r in result['status_fields'] if r['field'] == 'plant_time_s')['actual_availability'] == 'PUBLISHED_KEY_VALUES_MAY_BE_UNAVAILABLE'
    assert not result['flight_ready']


def test_undocumented_consumer_method_prevents_complete_capture(m, bundle):
    path = bundle / TDOC / 'src/tddt_tdoc/layer6_bridge.py'
    path.write_text(path.read_text() + '\ndef future_contract(client):\n    client.new_required_api()\n')
    with pytest.raises(ValueError, match='undocumented API'):
        m.audit(bundle, bundle / CONTRACT)


def test_undocumented_status_key_requires_explicit_contract_review(m, bundle):
    path = bundle / 'layer6_v20261007_1032/src/gui/phoenix_f310_gui.py'
    text = path.read_text().replace('"type": "status",', '"type": "status", "new_channel": None,')
    path.write_text(text)
    with pytest.raises(ValueError, match='undocumented keys'):
        m.audit(bundle, bundle / CONTRACT)


def test_status_presence_never_implies_quality_or_flight_release(m):
    result = m.audit(ROOT, ROOT / CONTRACT)
    rows = {r['field']: r for r in result['status_fields']}
    assert rows['attitude']['actual_availability'] == 'PUBLISHED_KEY_VALUES_MAY_BE_UNAVAILABLE'
    assert rows['imu']['actual_availability'] == 'PUBLISHED_KEY_VALUES_MAY_BE_UNAVAILABLE'
    assert rows['imu_samples']['actual_availability'] == 'PUBLISHED_KEY_VALUES_MAY_BE_UNAVAILABLE'
    assert rows['timestamp_monotonic']['actual_availability'] == rows['plant_time_s']['actual_availability']  # Key presence says nothing about values.


def test_no_source_code_import_or_execution(m, bundle):
    marker = bundle / 'UNTRUSTED_SOURCE_EXECUTED'
    path = bundle / 'layer6_v20261007_1032/src/layer6/client.py'
    path.write_text(path.read_text() + f'\nraise RuntimeError({str(marker)!r})\n')
    # AST inspection must succeed despite a module that cannot be imported.
    assert m.audit(bundle, bundle / CONTRACT)['audit_status'] == 'PASS'
    assert not marker.exists()


def test_changed_source_during_capture_prevents_pass(m, bundle, monkeypatch):
    original = m.source_ref
    path = bundle / 'layer6_v20261007_1032/src/layer6/client.py'
    mutated = False

    def mutate_once(*args):
        nonlocal mutated
        if not mutated:
            path.write_text(path.read_text() + '\n# concurrent source change\n')
            mutated = True
        return original(*args)

    monkeypatch.setattr(m, 'source_ref', mutate_once)
    with pytest.raises(ValueError, match='source drift'):
        m.audit(bundle, bundle / CONTRACT)


def test_missing_evidence_anchor_cannot_produce_pass(m, bundle):
    path = bundle / CONTRACT
    value = json.loads(path.read_text())
    value['api'][0]['evidence'].append('client:nonexistent_anchor')
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='missing source anchor'):
        m.audit(bundle, path)


def test_source_symlink_escape_is_rejected(m, bundle):
    path = bundle / 'layer6_v20261007_1032/src/layer6/client.py'
    copied = bundle / 'external.py'
    copied.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(copied)
    with pytest.raises(ValueError, match='symlink'):
        m.audit(bundle, bundle / CONTRACT)


def test_matrix_escapes_union_signature_without_losing_frame_time_or_tx(m):
    text = m.matrix(m.audit(ROOT, ROOT / CONTRACT))
    assert 'str \\| None' in text
    assert 'BODY_FRD' in text and 'tau_deadline' in text and 'transaction_id' in text
    assert 'CALL_SHAPE_COMPATIBLE' in text
    assert 'run_demo_sequence' in text


def test_missing_api_matrix_is_still_reported_for_an_actually_missing_method(m,bundle):
    path=bundle/'layer6_v20261007_1032/src/layer6/client.py'
    tree=ast.parse(path.read_text());cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Layer6Client')
    cls.body=[n for n in cls.body if not isinstance(n,ast.FunctionDef) or n.name!='cancel']
    path.write_text(ast.unparse(tree)+'\n')
    result=m.audit(bundle,bundle/CONTRACT)
    assert next(r for r in result['api'] if r['method']=='cancel')['status']=='MISSING_API'
    assert 'MISSING_API' in m.matrix(result)
