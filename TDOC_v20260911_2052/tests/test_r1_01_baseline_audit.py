"""Identity checks must reject shadow imports, drift and invented versions."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


def load_audit():
    path = Path(__file__).resolve().parents[1] / "scripts/audit_r1_01_baseline.py"
    spec = importlib.util.spec_from_file_location("r101_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def minimal_bundle(m, root, monkeypatch):
    for _, key, folder, _ in m.COMPONENTS:
        package = root / folder
        package.mkdir(parents=True)
        (package / "owned.py").write_text("VALUE = 1\n")
        (package / "pyproject.toml").write_text('[project]\nversion = "1.0"\n')
        monkeypatch.setenv(key, str(package))
    monkeypatch.setenv("PYTHON", sys.executable)
    monkeypatch.setattr(m, "ASSETS", ())
    monkeypatch.setattr(m, "native_labels", lambda root: {
        "EnvironmentAdapter_project_declared": "1", "EnvironmentAdapter_module_literal": "1"})
    return root / "TDOC_v20260911_2052/reports/online/test-run"


def fake_probe(package, names):
    return {"ok": True, "modules": [{"module": name, "module_version": "1.0", "ok": True} for name in names]}


def test_wrong_environment_root_is_rejected_before_any_import(tmp_path, monkeypatch):
    m = load_audit()
    output = minimal_bundle(m, tmp_path, monkeypatch)
    monkeypatch.setenv("TDDT_LAYER6_ROOT", str(tmp_path / "unrelated"))
    monkeypatch.setattr(m, "run_probe", lambda *a: pytest.fail("no imports allowed after root mismatch"))
    assert m.main(["--bundle-root", str(tmp_path), "--output-dir", str(output)]) == 2
    assert json.loads((output / "BASELINE.json").read_text())["imports_attempted"] is False


def test_shadow_parent_is_not_executed_for_dotted_import(tmp_path, monkeypatch):
    m = load_audit()
    expected = tmp_path / "owned"
    expected.mkdir()
    shadow = tmp_path / "shadow"
    package = shadow / "r101_shadow_test"
    package.mkdir(parents=True)
    marker = tmp_path / "untrusted_import_executed"
    (package / "__init__.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    (package / "child.py").write_text("VALUE = 1\n")
    monkeypatch.syspath_prepend(str(shadow))
    rows = m.probe_modules(expected, ["r101_shadow_test.child"])
    assert rows[0]["ok"] is False
    assert not marker.exists()
    assert "r101_shadow_test" not in sys.modules


def test_real_import_origin_version_and_hash_are_recorded(tmp_path, monkeypatch):
    m = load_audit()
    path = tmp_path / "r101_owned_test.py"
    path.write_text('__version__ = "2.3.4"\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "r101_owned_test", raising=False)
    try:
        row = m.probe_modules(tmp_path, ["r101_owned_test"])[0]
        assert row["ok"] and row["origin"] == str(path)
        assert row["module_version"] == "2.3.4"
        assert row["source_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    finally:
        sys.modules.pop("r101_owned_test", None)


def test_source_fingerprint_ignores_generated_output_but_detects_owned_change(tmp_path):
    m = load_audit()
    owned = tmp_path / "owned.py"
    owned.write_text("VALUE = 1\n")
    before = m.digest(m.source_files(tmp_path))
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "mutable.json").write_text('{"cycle": 100}')
    assert m.digest(m.source_files(tmp_path)) == before
    owned.write_text("VALUE = 2\n")
    assert m.digest(m.source_files(tmp_path)) != before


def test_source_symlink_escape_is_rejected(tmp_path):
    m = load_audit()
    package = tmp_path / "owned"
    package.mkdir()
    external = tmp_path / "external.py"
    external.write_text("VALUE = 1\n")
    (package / "escape.py").symlink_to(external)
    with pytest.raises(ValueError):
        m.source_files(package)


def test_import_time_source_mutation_prevents_baseline_pass(tmp_path, monkeypatch):
    m = load_audit()
    output = minimal_bundle(m, tmp_path, monkeypatch)

    def mutating_probe(package, names):
        (package / "owned.py").write_text("VALUE = 99\n")
        return fake_probe(package, names)

    monkeypatch.setattr(m, "run_probe", mutating_probe)
    assert m.main(["--bundle-root", str(tmp_path), "--output-dir", str(output)]) == 2
    result = json.loads((output / "BASELINE.json").read_text())
    assert result["checks"]["audited_sources_and_assets_unchanged"] is False
    assert result["status"] == "FAIL"


def test_metadata_import_version_mismatch_prevents_pass(tmp_path, monkeypatch):
    m = load_audit()
    output = minimal_bundle(m, tmp_path, monkeypatch)
    (tmp_path / "layer6_v20261007_1032/pyproject.toml").write_text('[project]\nversion = "9.9"\n')
    monkeypatch.setattr(m, "run_probe", fake_probe)
    assert m.main(["--bundle-root", str(tmp_path), "--output-dir", str(output)]) == 2
    result = json.loads((output / "BASELINE.json").read_text())
    assert result["checks"]["declared_imported_versions_consistent"] is False
