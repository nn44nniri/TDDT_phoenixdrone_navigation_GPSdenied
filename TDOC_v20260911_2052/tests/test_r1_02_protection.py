"""Protection must detect drift and validate recoverable bytes without active writes."""
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile

import pytest


@pytest.fixture
def m():
    path = Path(__file__).resolve().parents[1] / "scripts/freeze_r1_02_state.py"
    spec = importlib.util.spec_from_file_location("r102_protection", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_state(root):
    data = root / "runtime"
    data.mkdir()
    (data / "knowledge.gz").write_bytes(gzip.compress(b'{"prior":true}', mtime=0))
    (data / "forcing.csv").write_bytes(b"wind,1\n")
    (data / "empty").mkdir()
    return [{"id": "runtime", "path": "runtime", "required": True},
            {"id": "optional", "path": "optional", "required": False}]


@pytest.mark.parametrize("change", ["same_size", "added", "removed", "mode", "optional_created"])
def test_snapshot_rejects_drift(m, tmp_path, change):
    scopes = fixture_state(tmp_path)
    before = m.scan(tmp_path, scopes)
    if change == "same_size":
        (tmp_path / "runtime/forcing.csv").write_bytes(b"wind,2\n")
    elif change == "added":
        (tmp_path / "runtime/new").touch()
    elif change == "removed":
        (tmp_path / "runtime/forcing.csv").unlink()
    elif change == "mode":
        p = tmp_path / "runtime/forcing.csv"
        p.chmod(p.stat().st_mode ^ 0o100)
    else:
        (tmp_path / "optional").mkdir()
    assert not m.unchanged(m.difference(before, m.scan(tmp_path, scopes)))


def test_overlapping_scopes_deduplicate_physical_files(m, tmp_path):
    scopes = fixture_state(tmp_path)
    scopes.append({"id": "forcing", "path": "runtime/forcing.csv", "required": True})
    snapshot = m.scan(tmp_path, scopes)
    assert len(snapshot["files"]) == 2
    assert snapshot["files"]["runtime/forcing.csv"]["categories"] == ["forcing", "runtime"]


@pytest.mark.parametrize("relative", ["../escape", "/absolute"])
def test_path_escape_rejected(m, tmp_path, relative):
    with pytest.raises(ValueError):
        m.checked_path(tmp_path, relative)


def test_symlink_scope_and_compressed_alias_rejected(m, tmp_path):
    scopes = fixture_state(tmp_path)
    (tmp_path / "runtime/escape").symlink_to(tmp_path / "runtime/forcing.csv")
    with pytest.raises(ValueError):
        m.scan(tmp_path, scopes)
    (tmp_path / "logical.json.gz").symlink_to(tmp_path / "runtime/knowledge.gz")
    with pytest.raises(ValueError):
        m.logical_bytes(tmp_path / "logical.json")


@pytest.mark.parametrize("relative", ["runtime/backup", "TDOC/reports/backup", "recovery_baselines"])
def test_recovery_output_cannot_overlap_protected_or_reset_targets(m, tmp_path, relative):
    scopes = fixture_state(tmp_path)
    if relative == "recovery_baselines":
        scopes.append({"id": "bad", "path": relative, "required": False})
    with pytest.raises(ValueError):
        m.assert_output_location(tmp_path, tmp_path / relative, scopes)


def test_symlink_recovery_output_rejected(m, tmp_path):
    (tmp_path / "runtime").mkdir()
    (tmp_path / "recovery_baselines").symlink_to(tmp_path / "runtime", target_is_directory=True)
    with pytest.raises(ValueError):
        m.assert_output_location(tmp_path, tmp_path / "recovery_baselines/new", [])


def test_gzip_logical_identity_does_not_materialize_active_alias(m, tmp_path):
    raw = b'{"value":[1,2,3]}'
    path = tmp_path / "component.json"
    physical = Path(str(path) + ".gz")
    physical.write_bytes(gzip.compress(raw, mtime=0))
    before = physical.read_bytes()
    assert m.logical_bytes(path) == raw
    assert m.file_hash(physical) != hashlib.sha256(raw).hexdigest()
    assert physical.read_bytes() == before and not path.exists()
    physical.write_bytes(b"invalid gzip")
    with pytest.raises(gzip.BadGzipFile):
        m.logical_bytes(path)


def test_component_hash_mismatch_fails_closed(m, tmp_path):
    value = {"policy": "v1"}
    raw = json.dumps({"value": value}).encode()
    (tmp_path / "part.json.gz").write_bytes(gzip.compress(raw))
    descriptor = {"kind": "single", "logical_sha256": m.canonical_hash(value),
                  "files": [{"file": "part.json", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}]}
    assert m.component_value(tmp_path, descriptor) == value
    descriptor["files"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="byte hash"):
        m.component_value(tmp_path, descriptor)


def test_archive_roundtrip_and_isolated_restore_preserve_active_state(m, tmp_path):
    scopes = fixture_state(tmp_path)
    before = m.scan(tmp_path, scopes)
    archive = tmp_path / "recovery.tar.gz"
    m.make_archive(tmp_path, archive, before)
    restore = tmp_path / "restore"
    restore.mkdir()
    result = m.verify_archive(archive, before, restore, ["runtime/knowledge.gz"])
    assert result["verified_files"] == 2
    assert m.read_json(restore / "runtime/knowledge.gz") == {"prior": True}
    assert m.unchanged(m.difference(before, m.scan(tmp_path, scopes)))
    with pytest.raises(FileExistsError):
        m.verify_archive(archive, before, restore, ["runtime/knowledge.gz"])


@pytest.mark.parametrize("attack", ["traversal", "symlink", "duplicate", "missing", "corrupt"])
def test_archive_rejects_unsafe_or_invalid_payload(m, tmp_path, attack):
    scopes = fixture_state(tmp_path)
    snapshot = m.scan(tmp_path, scopes)
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        if attack in {"duplicate", "corrupt"}:
            for relative in snapshot["directories"]:
                tar.addfile(tar.gettarinfo(str(tmp_path / relative), "payload/" + relative))
            for relative in snapshot["files"]:
                raw = (tmp_path / relative).read_bytes()
                info = tar.gettarinfo(str(tmp_path / relative), "payload/" + relative)
                tar.addfile(info, io.BytesIO(b"x" * len(raw) if attack == "corrupt" else raw))
                if attack == "duplicate":
                    tar.addfile(info, io.BytesIO(raw))
        elif attack != "missing":
            info = tarfile.TarInfo("payload/../escape" if attack == "traversal" else "payload/runtime/link")
            if attack == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
            tar.addfile(info)
    with pytest.raises(ValueError):
        m.verify_archive(archive, snapshot)


def test_manifest_integrity_and_drift_are_separate_checks(m, tmp_path):
    scopes = fixture_state(tmp_path)
    manifest = {"bundle_root": str(tmp_path), "snapshot": m.scan(tmp_path, scopes), "scopes": scopes}
    manifest["manifest_sha256"] = m.canonical_hash(manifest)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    assert m.verify_manifest(path)["status"] == "PASS"
    (tmp_path / "runtime/forcing.csv").write_text("wind,9\n")
    assert m.verify_manifest(path)["status"] == "FAIL"
    manifest["bundle_root"] = str(tmp_path / "other")
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest was modified"):
        m.verify_manifest(path)


def test_archive_rejects_source_change_after_snapshot(m, tmp_path):
    scopes = fixture_state(tmp_path)
    before = m.scan(tmp_path, scopes)
    (tmp_path / "runtime/forcing.csv").write_bytes(b"wind,2\n")
    archive = tmp_path / "changed.tar.gz"
    m.make_archive(tmp_path, archive, before)
    with pytest.raises(ValueError, match="content mismatch"):
        m.verify_archive(archive, before)


def test_declared_forcing_alias_preserved_and_retargeting_rejected(m, tmp_path):
    scopes = fixture_state(tmp_path)
    alias = tmp_path / "runtime/forcing_alias.csv"
    alias.symlink_to("forcing.csv")
    scopes[0]["allowed_file_symlinks"] = {"runtime/forcing_alias.csv": "runtime/forcing.csv"}
    before = m.scan(tmp_path, scopes)
    archive = tmp_path / "aliased.tar.gz"
    m.make_archive(tmp_path, archive, before)
    assert m.verify_archive(archive, before)["verified_symlinks"] == 1
    assert alias.is_symlink() and alias.readlink() == Path("forcing.csv")
    alias.unlink()
    alias.symlink_to("knowledge.gz")
    with pytest.raises(ValueError, match="unapproved protected symlink"):
        m.scan(tmp_path, scopes)


def test_alias_target_must_be_in_protection_scope(m, tmp_path):
    fixture_state(tmp_path)
    (tmp_path / "outside.csv").write_bytes(b"external")
    (tmp_path / "runtime/alias").symlink_to("../outside.csv")
    scopes = [{"id": "runtime", "path": "runtime", "required": True,
               "allowed_file_symlinks": {"runtime/alias": "outside.csv"}}]
    with pytest.raises(ValueError, match="target must itself be protected"):
        m.scan(tmp_path, scopes)
