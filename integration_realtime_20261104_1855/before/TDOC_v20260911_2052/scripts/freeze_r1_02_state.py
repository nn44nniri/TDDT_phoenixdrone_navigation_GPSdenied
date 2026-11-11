#!/usr/bin/env python3
"""R1.02: capture and verify protected state; never materialize active KStore.

Only the new recovery_baselines output directory is written. Original files are
not chmod'ed, repaired, reset, recompressed or restored. No runtime is imported.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tarfile
from zoneinfo import ZoneInfo


TDOC = "TDOC_v20260911_2052"
L2 = "twins_layer2_v20260820_1233"
CLIMATE = "climate_twin_v20260911_0922"
L6 = "layer6_v20261007_1032"


def canonical_hash(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(data).hexdigest()


def stream_hash(stream):
    h = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        h.update(block)
    return h.hexdigest()


def file_hash(path):
    with Path(path).open("rb") as stream:
        return stream_hash(stream)


def checked_path(root, relative):
    rel = PurePosixPath(str(relative))
    if rel.is_absolute() or not rel.parts or any(p in {"..", "."} for p in rel.parts):
        raise ValueError(f"unsafe relative path: {relative}")
    candidate = root.joinpath(*rel.parts)
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"protected path contains symlink: {relative}")
    candidate.resolve().relative_to(root)
    return candidate


def logical_bytes(path):
    """Read the stored representation without creating its uncompressed alias."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("logical input is a symlink")
    if path.is_file():
        return gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    compressed = Path(str(path) + ".gz")
    if compressed.is_symlink():
        raise ValueError("compressed logical input is a symlink")
    if compressed.is_file():
        return gzip.decompress(compressed.read_bytes())
    raise FileNotFoundError(str(path))


def read_json(path):
    return json.loads(logical_bytes(path))


def scan(root, scopes):
    files, directories, presence, links = {}, {}, [], {}
    allowed_links = {}
    for scope in scopes:
        allowed_links.update(scope.get("allowed_file_symlinks", {}))

    def record_file(path, category):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            target = allowed_links.get(relative)
            if not target or path.resolve(strict=True) != checked_path(root, target):
                raise ValueError(f"unapproved protected symlink: {relative}")
            if not checked_path(root, target).is_file():
                raise ValueError("approved alias target is not a regular file")
            st = path.lstat()
            links[relative] = {"target": os.readlink(path), "resolved_path": target,
                               "mode": stat.S_IMODE(st.st_mode), "mtime_ns": st.st_mtime_ns}
            return
        checked_path(root, relative)
        before = path.stat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"not a regular protected file: {relative}")
        if relative not in files:
            digest = file_hash(path)
            after = path.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError(f"file changed while hashing: {relative}")
            files[relative] = {"sha256": digest, "bytes": before.st_size,
                               "mode": stat.S_IMODE(before.st_mode), "mtime_ns": before.st_mtime_ns,
                               "categories": []}
        if category not in files[relative]["categories"]:
            files[relative]["categories"].append(category)

    for scope in scopes:
        path = checked_path(root, scope["path"])
        exists = path.exists()
        if scope.get("required") and not exists:
            raise FileNotFoundError(f"required protection scope missing: {scope['path']}")
        presence.append({"id": scope["id"], "path": scope["path"], "exists": exists,
                         "kind": "directory" if path.is_dir() else "file" if exists else "absent"})
        if not exists:
            continue
        if path.is_file():
            record_file(path, scope["id"])
            continue
        for directory, dirs, names in os.walk(path, followlinks=False):
            directory = Path(directory)
            st = directory.stat()
            directories[directory.relative_to(root).as_posix()] = {
                "mode": stat.S_IMODE(st.st_mode), "mtime_ns": st.st_mtime_ns}
            for name in dirs:
                checked_path(root, (directory / name).relative_to(root).as_posix())
            dirs.sort()
            for name in sorted(names):
                record_file(directory / name, scope["id"])
    for row in files.values():
        row["categories"].sort()
    if any(link["resolved_path"] not in files for link in links.values()):
        raise ValueError("alias target must itself be protected")
    return {"files": dict(sorted(files.items())), "directories": dict(sorted(directories.items())),
            "symlinks": dict(sorted(links.items())), "scope_presence": presence}


def difference(before, after):
    old, new = before["files"], after["files"]
    return {"added": sorted(set(new) - set(old)), "removed": sorted(set(old) - set(new)),
            "changed": sorted(p for p in set(old) & set(new) if old[p] != new[p]),
            "directories_changed": before["directories"] != after["directories"],
            "symlinks_changed": before["symlinks"] != after["symlinks"],
            "scope_presence_changed": before["scope_presence"] != after["scope_presence"]}


def unchanged(diff):
    return not any(diff.values())


def assert_output_location(root, output, scopes):
    output.relative_to(root / "recovery_baselines")
    checked_path(root, output.relative_to(root).as_posix())
    output.resolve().relative_to(root / "recovery_baselines")
    for scope in scopes:
        path = checked_path(root, scope["path"])
        if output == path or output.is_relative_to(path) or path.is_relative_to(output):
            raise ValueError("recovery output overlaps protected data")


def verify_archive(path, snapshot, restore_dir=None, selected=()):
    """Validate every tar member; restore only selected files into a new drill tree."""
    expected = snapshot["files"]
    seen, seen_dirs, seen_links, restored = set(), set(), set(), []
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != "payload":
                raise ValueError("unsafe archive member")
            relative = PurePosixPath(*name.parts[1:]).as_posix()
            if member.issym():
                expected_link = snapshot["symlinks"].get(relative)
                if (expected_link is None or relative in seen_links or member.linkname != expected_link["target"]
                        or member.mode != expected_link["mode"]):
                    raise ValueError("unexpected or changed archive symlink")
                seen_links.add(relative)
                continue
            if member.isdir():
                if relative not in snapshot["directories"] or relative in seen_dirs:
                    raise ValueError("unrecorded archive directory")
                seen_dirs.add(relative)
                if member.mode != snapshot["directories"][relative]["mode"]:
                    raise ValueError("archive directory permission mismatch")
                continue
            if not member.isfile() or relative not in expected or relative in seen:
                raise ValueError("unexpected, duplicate or non-regular archive member")
            seen.add(relative)
            if member.mode != expected[relative]["mode"]:
                raise ValueError("archive file permission mismatch")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("archive file unreadable")
            h = hashlib.sha256()
            count = 0
            target = None
            if restore_dir is not None and relative in selected:
                target = checked_path(restore_dir, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                restored.append(relative)
            with (target.open("xb") if target else open(os.devnull, "wb")) as dst:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    count += len(block)
                    h.update(block)
                    if target:
                        dst.write(block)
            if count != expected[relative]["bytes"] or h.hexdigest() != expected[relative]["sha256"]:
                raise ValueError(f"archive content mismatch: {relative}")
    if (seen != set(expected) or seen_dirs != set(snapshot["directories"])
            or seen_links != set(snapshot["symlinks"])):
        raise ValueError("archive is incomplete")
    if restore_dir is not None and set(restored) != set(selected):
        raise ValueError("restore drill incomplete")
    return {"ok": True, "verified_files": len(seen), "verified_symlinks": len(seen_links), "restored_files": restored}


def make_archive(root, target, snapshot):
    with tarfile.open(target, "w:gz", compresslevel=1) as archive:
        for relative in snapshot["directories"]:
            info = archive.gettarinfo(str(checked_path(root, relative)), "payload/" + relative)
            archive.addfile(info)
        for relative in snapshot["files"]:
            path = checked_path(root, relative)
            info = archive.gettarinfo(str(path), "payload/" + relative)
            with path.open("rb") as src:
                archive.addfile(info, src)
        for relative, link in snapshot["symlinks"].items():
            path = checked_path(root, str(PurePosixPath(relative).parent)) / PurePosixPath(relative).name
            if not path.is_symlink() or os.readlink(path) != link["target"]:
                raise ValueError("protected alias changed during archive")
            archive.addfile(archive.gettarinfo(str(path), "payload/" + relative))


def component_value(package, descriptor):
    parts = []
    for entry in descriptor.get("files", []):
        path = checked_path(package, entry["file"])
        data = logical_bytes(path)
        if hashlib.sha256(data).hexdigest() != entry["sha256"] or len(data) != entry["bytes"]:
            raise ValueError("SETD component byte hash mismatch")
        parts.append(data)
    kind = descriptor["kind"]
    if kind == "json_bytes_sharded":
        value = json.loads(b"".join(parts))
    else:
        values = [json.loads(b) for b in parts]
        if kind == "single" and len(values) == 1:
            value = values[0]["value"]
        elif kind == "list_sharded":
            value = [item for part in values for item in part["items"]]
        elif kind == "dict_sharded":
            value = {}
            for part in values:
                if set(value) & set(part["entries"]):
                    raise ValueError("duplicate SETD dictionary key")
                value.update(part["entries"])
        else:
            raise ValueError("unsupported SETD component shape")
    if canonical_hash(value) != descriptor["logical_sha256"]:
        raise ValueError("SETD logical component hash mismatch")
    return value


def binding_checks(root, handoff, snapshot):
    stripped = {k: v for k, v in handoff.items() if k not in {"handoff_hash", "knowledge_id", "knowledge_version", "ok"}}
    if canonical_hash(stripped) != handoff.get("handoff_hash"):
        raise ValueError("handoff content hash mismatch")
    kstore = Path(handoff["kstore_fingerprint"]["root"])
    kstore.relative_to(root)
    rows = [(p, v) for p, v in snapshot["files"].items() if checked_path(root, p).is_relative_to(kstore)]
    rows.sort()
    fingerprint = hashlib.sha256("\n".join(
        f"{checked_path(root, p).relative_to(kstore).as_posix()}|{v['bytes']}|{v['sha256']}" for p, v in rows).encode()).hexdigest()
    expected = handoff["kstore_fingerprint"]
    if (len(rows), sum(v["bytes"] for _, v in rows), fingerprint) != (expected["file_count"], expected["total_bytes"], expected["sha256"]):
        raise ValueError("accepted KStore fingerprint mismatch")
    manifest_path = checked_path(kstore, handoff["setd"]["package_manifest"])
    raw = logical_bytes(manifest_path)
    if hashlib.sha256(raw).hexdigest() != handoff["setd"]["package_manifest_sha256"]:
        raise ValueError("SETD manifest logical byte hash mismatch")
    manifest = json.loads(raw)
    if manifest.get("state") != "COMMITTED" or manifest.get("package_id") != handoff["setd"]["package_id"]:
        raise ValueError("SETD identity/commit mismatch")
    if canonical_hash({k: v for k, v in manifest.items() if k != "manifest_content_sha256"}) != manifest["manifest_content_sha256"]:
        raise ValueError("SETD manifest content mismatch")
    package = manifest_path.parent
    header = logical_bytes(checked_path(package, manifest["header_file"]))
    if hashlib.sha256(header).hexdigest() != manifest["header_sha256"]:
        raise ValueError("SETD header mismatch")
    stage = json.loads(header)
    stage["knowledge"] = {d["key"]: component_value(package, d) for d in manifest["components"]}
    if canonical_hash(stage) != manifest["logical_content_sha256"]:
        raise ValueError("SETD reconstructed logical content mismatch")
    alignment = handoff["reuse_contract"]["climate_time_alignment"]
    forcing = Path(alignment["forcing_file"])
    forcing.relative_to(root)
    checked_path(root, forcing.relative_to(root).as_posix())
    if file_hash(forcing) != alignment["forcing_file_sha256"]:
        raise ValueError("accepted forcing hash mismatch")
    pointer = read_json(root / CLIMATE / "climate_dataset/tdoc_active_forcing.json")
    materialized_relative = CLIMATE + "/climate_dataset/" + pointer["file"]
    if materialized_relative in snapshot["symlinks"]:
        materialized = root / materialized_relative
        if materialized.resolve(strict=True) != forcing:
            raise ValueError("materialized forcing alias points elsewhere")
    else:
        materialized = checked_path(root, materialized_relative)
    if file_hash(materialized) != alignment["forcing_file_sha256"]:
        raise ValueError("materialized forcing differs from accepted forcing")
    return {"ok": True, "handoff_hash": handoff["handoff_hash"], "source_transaction_id": handoff["source_transaction_id"],
            "setd_package_id": manifest["package_id"], "setd_components_verified": len(manifest["components"]),
            "experience_count": manifest["experience_count"], "kstore_file_count": len(rows), "kstore_sha256": fingerprint,
            "forcing_sha256": alignment["forcing_file_sha256"], "climate_time_alignment": alignment,
            "legacy_valid_setd": handoff.get("valid_setd"), "setd_integrity_valid": handoff.get("setd_integrity_valid"),
            "control_knowledge_valid": handoff.get("control_knowledge_valid"),
            "authority": "immutable prior identity only; no control or flight authorization"}


def r1_reference(root, baseline_path):
    baseline = read_json(baseline_path)
    if baseline.get("status") != "PASS" or Path(baseline["bundle_root"]) != root:
        raise ValueError("R1.01 baseline is not the current bundle's PASS record")
    inventory = read_json(baseline_path.parent / "SOURCE_INVENTORY.json")
    checked = {}
    for component in baseline["components"]:
        package = Path(component["root"])
        package.relative_to(root)
        for name, row in inventory["packages"][component["component"]]["files"].items():
            p = checked_path(package, name)
            checked[str(p.relative_to(root))] = row["sha256"]
    checked.update({r["path"]: r["sha256"] for r in baseline["selected_artifacts"] if r["exists"]})
    changed = [p for p, expected in checked.items() if not checked_path(root, p).is_file() or file_hash(checked_path(root, p)) != expected]
    if changed:
        raise ValueError("existing R1.01 files changed: " + repr(changed))
    return {"ok": True, "baseline_path": str(baseline_path), "baseline_sha256": file_hash(baseline_path),
            "existing_files_verified": len(checked), "new_R1_02_tool_files_are_outside_prior_inventory": True}


def protection_scopes(root, baseline, handoff):
    scopes = []

    def add(name, path, required=True):
        relative = Path(path).relative_to(root).as_posix() if Path(path).is_absolute() else str(path)
        checked_path(root, relative)
        scopes.append({"id": name, "path": relative, "required": required})

    for name, path in (
        ("tdoc_runtime", TDOC + "/runtime"),
        ("offline_reports", TDOC + "/reports/offline"),
        ("R1_01_evidence", str(baseline.parent)),
        ("map_CRP", "boulder_multilayer_map_builder_v20260814_1215/outputs/region_v1_map"),
        ("SARG", "SARG_v20260821_0945/generated/SARG"),
        ("L2_contracts_TEP", L2 + "/resources/tddt_layer2"),
        ("Climate_contracts_TEP", CLIMATE + "/resources/tddt_layer2"),
        ("climate_materialization", CLIMATE + "/climate_dataset"),
        ("L2_airframe", L2 + "/config"),
        ("L5_model_resources", "layer5_v20260911_1510/resources"),
        ("mission_definition", TDOC + "/region"),
        ("runtime_configuration", TDOC + "/configs"),
        ("L5_configuration", "layer5_v20260911_1510/configs/configuration_phoenix.yaml"),
        ("Phoenix_source_model_tree", L6 + "/FlightStack/models/phoenix_v3"),
        ("Phoenix_installed_aircraft_tree", L6 + "/FlightStack/ardupilot/Tools/autotest/aircraft/phoenix_v3"),
        ("capture_procedure", TDOC + "/scripts/freeze_r1_02_state.py"),
        ("capture_tests", TDOC + "/tests/test_r1_02_protection.py"),
    ):
        add(name, path)
    add("accepted_forcing", handoff["reuse_contract"]["climate_time_alignment"]["forcing_file"])
    forcing = Path(handoff["reuse_contract"]["climate_time_alignment"]["forcing_file"])
    add("accepted_forcing_metadata", str(forcing) + ".meta.json", False)
    climate_scope = next(s for s in scopes if s["id"] == "climate_materialization")
    climate_scope["allowed_file_symlinks"] = {
        CLIMATE + "/climate_dataset/" + forcing.name + suffix:
            forcing.relative_to(root).as_posix() + suffix for suffix in ("", ".meta.json")}
    add("accepted_kstore", handoff["kstore_fingerprint"]["root"])
    previous = read_json(baseline)
    for i, asset in enumerate(previous["selected_artifacts"]):
        add(f"model_native_asset_{i:02d}", asset["path"])
    for key in ("TDDT_LAYER5_KSTORE_ROOT", "TDDT_KSTORE_ROOT", "TDDT_LAYER7_KSTORE_BASE", "TDDT_LAYER7_STATE_ROOT"):
        raw = os.environ.get(key)
        if raw:
            add(key, raw, required=key in {"TDDT_LAYER5_KSTORE_ROOT", "TDDT_KSTORE_ROOT"})
    for name, path in (
        ("optional_compiled_policy", TDOC + "/runtime/knowledge/policy/online_policy.json.gz"),
        ("optional_legacy_L5_state", "layer5_v20260911_1510/runtime"),
        ("optional_external_L7_state", "layer7_v20260901_0921/runtime"),
        ("optional_native_binding", L6 + "/runtime/tdoc_environment_binding.json"),
    ):
        add(name, path, False)
    return scopes


def verify_manifest(path):
    manifest = json.loads(Path(path).read_text())
    expected = manifest.pop("manifest_sha256")
    if canonical_hash(manifest) != expected:
        raise ValueError("protection manifest was modified")
    root = Path(manifest["bundle_root"]).resolve()
    diff = difference(manifest["snapshot"], scan(root, manifest["scopes"]))
    return {"step": "R1.02", "status": "PASS" if unchanged(diff) else "FAIL", "difference": diff}


def capture(root, output, baseline, roadmap):
    handoff_path = root / TDOC / "runtime/knowledge/handoff/offline_latest.json.gz"
    handoff = read_json(handoff_path)
    reference = r1_reference(root, baseline)
    scopes = protection_scopes(root, baseline, handoff)
    assert_output_location(root, output, scopes)
    output.mkdir(parents=True, exist_ok=False)
    print("[R1.02] hashing protected inputs", flush=True)
    before = scan(root, scopes)
    bindings = binding_checks(root, handoff, before)
    manifest = {"schema": "TDDT-R1.02-protected-state-v1", "step": "R1.02", "bundle_root": str(root),
                "created_at_utc": datetime.now(timezone.utc).isoformat(), "scopes": scopes, "snapshot": before,
                "R1_01_reference": reference, "bindings": bindings, "roadmap_sha256": file_hash(roadmap),
                "roadmap_drive_id": "1pwuHG4nalz0XbhfUVwxW0MHJCKQ4mocj",
                "protection_semantics": "versioned backup and drift detection; original permissions and data unchanged"}
    manifest["manifest_sha256"] = canonical_hash(manifest)
    (output / "PROTECTED_MANIFEST.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    (output / "ROADMAP_SOURCE.md").write_bytes(roadmap.read_bytes())
    archive = output / "PROTECTED_STATE.tar.gz"
    print("[R1.02] creating independent recovery archive", flush=True)
    make_archive(root, archive, before)
    selected = [handoff_path.relative_to(root).as_posix(),
                handoff["setd"]["package_manifest"] + ".gz"]
    selected[1] = str(Path(handoff["kstore_fingerprint"]["root"]).relative_to(root) / selected[1])
    selected += [str(Path(bindings["climate_time_alignment"]["forcing_file"]).relative_to(root)),
                 L2 + "/fmus/WingMPET.fmu",
                 L6 + "/FlightStack/models/phoenix_v3/aircraft/phoenix_v3/phoenix_v3.xml"]
    restore = output / "restore_probe"
    restore.mkdir()
    print("[R1.02] verifying all archived bytes and isolated restore samples", flush=True)
    archived = verify_archive(archive, before, restore, selected)
    restored_handoff = read_json(restore / selected[0])
    if restored_handoff != handoff:
        raise ValueError("restored handoff differs")
    print("[R1.02] rechecking active protected inputs", flush=True)
    diff = difference(before, scan(root, scopes))
    if not unchanged(diff):
        raise ValueError("protected inputs changed during capture: " + repr(diff))
    result = {"schema": "TDDT-R1.02-capture-result-v1", "step": "R1.02", "status": "PASS", "output_dir": str(output),
              "generated_at_toronto": datetime.now(ZoneInfo("America/Toronto")).isoformat(),
              "protected_file_count": len(before["files"]), "protected_bytes": sum(v["bytes"] for v in before["files"].values()),
              "protected_symlink_count": len(before["symlinks"]),
              "manifest_file_sha256": file_hash(output / "PROTECTED_MANIFEST.json"), "manifest_content_sha256": manifest["manifest_sha256"],
              "archive": {"path": str(archive), "bytes": archive.stat().st_size, "sha256": file_hash(archive)},
              "archive_verification": archived, "source_unchanged": diff, "bindings": bindings,
              "R1_01_reference": reference, "outside_current_reset_targets": True,
              "reset_performed": False, "training_performed": False, "native_started": False, "flight_authority": False}
    (output / "CAPTURE_RESULT.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({k: result[k] for k in ("status", "output_dir", "protected_file_count", "protected_bytes")}), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("capture")
    p.add_argument("--bundle-root", type=Path, default=Path(__file__).resolve().parents[2])
    p.add_argument("--baseline", type=Path, required=True)
    p.add_argument("--roadmap", type=Path, required=True)
    p.add_argument("--output-dir", type=Path)
    p = sub.add_parser("verify")
    p.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    if args.command == "verify":
        result = verify_manifest(args.manifest)
        print(json.dumps(result))
        return 0 if result["status"] == "PASS" else 2
    root = args.bundle_root.resolve()
    output = (args.output_dir or root / "recovery_baselines" / ("R1_02_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ"))).absolute()
    capture(root, output, args.baseline.resolve(), args.roadmap.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
