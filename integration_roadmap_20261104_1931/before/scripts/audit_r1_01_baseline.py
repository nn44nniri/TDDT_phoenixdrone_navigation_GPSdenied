#!/usr/bin/env python3
"""Record R1.01 identities without constructing a runtime or changing knowledge.

Source configs/environment_fixed.sh and run this file with $PYTHON -B.
Imports run in fresh processes; builders are imported, never invoked. Source
fingerprints deliberately exclude generated data and FlightStack dependencies.
Selected native artifacts are pinned separately, without claiming a live load.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib
from zoneinfo import ZoneInfo
import zipfile
import xml.etree.ElementTree as ET


COMPONENTS = (
    ("TDOC", "TDDT_ORCHESTRATOR_ROOT", "TDOC_v20260911_2052", ("tddt_tdoc",)),
    ("Layer2", "TDDT_TWINS_ROOT", "twins_layer2_v20260820_1233", ("twins_fmi",)),
    ("MicroMet", "TDDT_CLIMATE_ROOT", "climate_twin_v20260911_0922", ("climate_binding",)),
    ("Units", "TDDT_UNIT_ADAPTER_ROOT", "TDOC_ISO_Universal_Unit_Conversion_Standard20260824_0914", ("tdoc_unit_converter",)),
    ("Boulder", "TDDT_BOULDER_ROOT", "boulder_multilayer_map_builder_v20260814_1215", ("build_regional_map", "build_crp_dataset")),
    ("SARG", "TDDT_SARG_PACKAGE_ROOT", "SARG_v20260821_0945", ("sarg_builder", "sarg_builder.cli")),
    ("Layer3", "TDDT_LAYER3_ROOT", "layer3_v20260911_1131", ("layer3",)),
    ("Layer4", "TDDT_LAYER4_ROOT", "layer4_v20260907_0608", ("layer4",)),
    ("Layer5", "TDDT_LAYER5_ROOT", "layer5_v20260911_1510", ("layer5",)),
    ("Layer6", "TDDT_LAYER6_ROOT", "layer6_v20261007_1032", ("layer6",)),
    ("Layer7", "TDDT_LAYER7_ROOT", "layer7_v20260901_0921", ("tddt_layer7",)),
)
SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache",
    "FlightStack", "build", "dist", "runtime", "reports", "outputs", "generated",
    "datasets", "internet_datasets", "climate_dataset", "tep", "kstore", "data",
    "docs", "doc", "node_modules", "site-packages", "CMakeFiles", ".eggs",
})
SUFFIXES = frozenset({".py", ".sh", ".c", ".cpp", ".h", ".hpp", ".f", ".f90",
                      ".toml", ".cfg", ".ini", ".yaml", ".yml", ".json", ".xml", ".cmake"})
SPECIAL_NAMES = frozenset({"requirements.txt", "CMakeLists.txt", "Makefile", "VERSION"})
L6 = "layer6_v20261007_1032"
FS = L6 + "/FlightStack/"
ASSETS = (
    "twins_layer2_v20260820_1233/fmus/MESA_NAV.fmu",
    "twins_layer2_v20260820_1233/fmus/WingMPET.fmu",
    "climate_twin_v20260911_0922/fmu/MicroMetTwin.fmu",
    FS + "ardupilot/build/sitl/bin/arduplane",
    FS + "ardupilot/ArduPlane/version.h",
    FS + "ardupilot/build/sitl/ap_version.h",
    FS + "ardupilot/libraries/SITL/SIM_JSBSim.cpp",
    FS + "jsbsim/build/src/JSBSim",
    FS + "jsbsim/CMakeLists.txt",
    FS + "jsbsim/build/src/JSBSimConfigVersion.cmake",
    FS + "models/phoenix_v3/VERSION",
    FS + "models/phoenix_v3/aircraft/phoenix_v3/phoenix_v3.xml",
    FS + "models/phoenix_v3/aircraft/phoenix_v3/reset.xml",
    FS + "ardupilot/Tools/autotest/aircraft/phoenix_v3/phoenix_v3.xml",
    FS + "ardupilot/Tools/autotest/aircraft/phoenix_v3/reset.xml",
    FS + "ardupilot/Tools/autotest/default_params/phoenix_v3.parm",
    FS + "phoenix_environment_adapter_v2/pyproject.toml",
    FS + "phoenix_environment_adapter_v2/phoenix_env_adapter/__init__.py",
    FS + "phoenix_environment_adapter_v2/phoenix_env_adapter/runtime.py",
    L6 + "/gui/scripts/run_gui.sh",
)


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def roots_check(root: Path, environment: dict) -> list[dict]:
    rows = []
    for name, key, folder, _ in COMPONENTS:
        expected = root / folder
        raw = environment.get(key)
        ok = bool(raw and expected.is_dir() and Path(raw).resolve() == expected
                  and expected.resolve().is_relative_to(root))
        rows.append({"component": name, "variable": key, "actual": raw,
                     "expected": str(expected), "ok": ok})
    return rows


def source_files(package: Path) -> dict:
    files = {}
    for directory, dirs, names in os.walk(package, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (Path(directory) / d).is_symlink())
        for name in sorted(names):
            p = Path(directory) / name
            if p.suffix.lower() not in SUFFIXES and name not in SPECIAL_NAMES:
                continue
            if not p.is_file():
                continue
            p.resolve().relative_to(package)
            files[p.relative_to(package).as_posix()] = {"sha256": sha(p), "bytes": p.stat().st_size}
    return files


def source_snapshot(root: Path) -> dict:
    packages = {}
    for name, _, folder, _ in COMPONENTS:
        files = source_files(root / folder)
        packages[name] = {"files": files, "file_count": len(files), "sha256": digest(files)}
    return {"packages": packages, "sha256": digest(packages),
            "scope": "selected source/config files; excluded directories and extensions recorded explicitly",
            "excluded_directories": sorted(SKIP_DIRS), "included_suffixes": sorted(SUFFIXES),
            "included_special_names": sorted(SPECIAL_NAMES)}


def probe_modules(expected: Path, names: list[str]) -> list[dict]:
    rows = []
    for name in names:
        row = {"module": name}
        try:
            # Check every prefix before import; find_spec for a dotted name can
            # otherwise execute an untrusted shadow package's __init__ first.
            parts = name.split(".")
            for count in range(1, len(parts) + 1):
                prefix = ".".join(parts[:count])
                spec = importlib.util.find_spec(prefix)
                if spec is None or not spec.origin:
                    raise ImportError(f"no file-backed module: {prefix}")
                Path(spec.origin).resolve().relative_to(expected)
                imported = importlib.import_module(prefix)
                Path(imported.__file__).resolve().relative_to(expected)
            origin = Path(imported.__file__).resolve()
            row.update(ok=True, origin=str(origin), source_sha256=sha(origin),
                       module_version=getattr(imported, "__version__", None))
        except Exception as exc:
            row.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        rows.append(row)
    return rows


def run_probe(package: Path, names: tuple[str, ...], timeout: float = 45) -> dict:
    command = [sys.executable, "-B", str(Path(__file__).resolve()),
               "--probe-root", str(package), "--probe-modules", *names]
    try:
        p = subprocess.run(command, cwd=package, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                           capture_output=True, text=True, timeout=timeout)
        rows = json.loads(p.stdout)
        return {"ok": p.returncode == 0 and all(r["ok"] for r in rows), "modules": rows,
                "cwd": str(package), "stderr": p.stderr, "returncode": p.returncode,
                "context": "fresh import process with canonical PYTHONPATH; package CWD matches preparation entrypoints"}
    except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
        return {"ok": False, "modules": [], "error": repr(exc), "cwd": str(package)}


def asset_records(root: Path) -> list[dict]:
    rows = []
    for relative in ASSETS:
        path = root / relative
        row = {"path": relative, "exists": path.is_file()}
        if path.is_file():
            path.resolve().relative_to(root)
            row.update(sha256=sha(path), bytes=path.stat().st_size)
            if path.suffix == ".fmu":
                with zipfile.ZipFile(path) as archive:
                    xml = archive.read("modelDescription.xml")
                description = ET.fromstring(xml)
                row["fmi_description"] = {k: description.get(k) for k in
                                          ("modelName", "fmiVersion", "guid", "version", "generationTool")}
                row["model_description_sha256"] = hashlib.sha256(xml).hexdigest()
            elif path.name == "phoenix_v3.xml":
                row["ground_contacts"] = [e.attrib for e in ET.parse(path).findall(".//ground_reactions/contact")]
        rows.append(row)
    return rows


def native_labels(root: Path) -> dict:
    def text(relative):
        p = root / FS / relative
        return p.read_text() if p.is_file() else ""
    plane = re.search(r'#define THISFIRMWARE "([^"]+)"', text("ardupilot/ArduPlane/version.h"))
    js = re.search(r'set\(PACKAGE_VERSION "([^"]+)"\)', text("jsbsim/build/src/JSBSimConfigVersion.cmake"))
    env_project = tomllib.loads(text("phoenix_environment_adapter_v2/pyproject.toml")).get("project", {})
    env_module = re.search(r'__version__\s*=\s*[\'"]([^\'"]+)', text("phoenix_environment_adapter_v2/phoenix_env_adapter/__init__.py"))
    return {"ArduPilot_source_declared": plane.group(1) if plane else None,
            "JSBSim_build_metadata_declared": js.group(1) if js else None,
            "Phoenix_model_VERSION": text("models/phoenix_v3/VERSION").strip(),
            "EnvironmentAdapter_project_declared": env_project.get("version"),
            "EnvironmentAdapter_module_literal": env_module.group(1) if env_module else None,
            "scope": "file metadata and binary hashes only; native programs were not started and binary/source correspondence is not certified"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--probe-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--probe-modules", nargs="+", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    sys.dont_write_bytecode = True
    if args.probe_root is not None:
        # Match python -m / script execution from the component's working
        # directory, rather than exposing this audit script's directory first.
        sys.path[0] = str(args.probe_root.resolve())
        rows = probe_modules(args.probe_root.resolve(), args.probe_modules or [])
        print(json.dumps(rows))
        return 0 if rows and all(r["ok"] for r in rows) else 2
    root = args.bundle_root.resolve()
    stamp = datetime.now(timezone.utc)
    output = (args.output_dir or root / "TDOC_v20260911_2052/reports/online" /
              ("R1_01_baseline_" + stamp.strftime("%Y%m%dT%H%M%S_%fZ"))).resolve()
    output.relative_to(root / "TDOC_v20260911_2052/reports/online")
    output.mkdir(parents=True, exist_ok=False)
    roots = roots_check(root, os.environ)
    if not all(r["ok"] for r in roots):
        result = {"status": "FAIL", "step": "R1.01", "roots": roots, "imports_attempted": False}
        (output / "BASELINE.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"status": "FAIL", "reason": "environment_root_mismatch", "output_dir": str(output)}))
        return 2
    before = source_snapshot(root)
    assets = asset_records(root)
    components = []
    for name, key, folder, modules in COMPONENTS:
        package = root / folder
        probe = run_probe(package, modules)
        pp = package / "pyproject.toml"
        project = tomllib.loads(pp.read_text()).get("project", {}) if pp.is_file() else {}
        imported_version = next((r.get("module_version") for r in probe["modules"] if r.get("module_version")), None)
        declared_version = project.get("version")
        tree = before["packages"][name]
        components.append({"component": name, "root_variable": key, "root": str(package),
                           "release_folder_label": folder, "project_name": project.get("name"),
                           "declared_version": declared_version, "imported_version": imported_version,
                           "version_consistent": not (declared_version and imported_version and declared_version != imported_version),
                           "identity_version": imported_version or declared_version or "unversioned-source:" + tree["sha256"],
                           "version_kind": "declared/imported" if imported_version or declared_version else "content-addressed; no independent version declaration",
                           "source_sha256": tree["sha256"], "source_file_count": tree["file_count"], "import_probe": probe})
    after = source_snapshot(root)
    after_assets = asset_records(root)
    configured_python = os.environ.get("PYTHON")
    checks = {
        "environment_roots_match": all(r["ok"] for r in roots),
        "configured_python_matches": bool(configured_python and os.path.abspath(configured_python) == os.path.abspath(sys.executable)),
        "imports_from_expected_roots": all(c["import_probe"]["ok"] for c in components),
        "declared_imported_versions_consistent": all(c["version_consistent"] for c in components),
        "all_components_fingerprinted": all(c["source_file_count"] > 0 for c in components),
        "selected_native_and_fmu_artifacts_present": all(a["exists"] for a in assets),
        "audited_sources_and_assets_unchanged": before == after and assets == after_assets,
    }
    native = native_labels(root)
    native_warnings = []
    if native["EnvironmentAdapter_project_declared"] != native["EnvironmentAdapter_module_literal"]:
        native_warnings.append("EnvironmentAdapter project version and module literal differ; file hashes remain authoritative for this baseline")
    result = {
        "schema": "TDDT-R1.01-baseline-v1", "step": "R1.01", "status": "PASS" if all(checks.values()) else "FAIL",
        "generated_at_utc": stamp.isoformat(), "generated_at_toronto": stamp.astimezone(ZoneInfo("America/Toronto")).isoformat(),
        "bundle_root": str(root), "checks": checks, "environment_roots": roots, "components": components,
        "python": {"executable": sys.executable, "resolved_executable": str(Path(sys.executable).resolve()),
                   "version": sys.version, "prefix": sys.prefix, "configured_python": configured_python},
        "selected_artifacts": assets, "native_version_labels": native,
        "native_metadata_warnings": native_warnings,
        "source_inventory_sha256": before["sha256"], "source_file_count": sum(c["source_file_count"] for c in components),
        "component_count": len(components), "imported_module_count": sum(len(c["import_probe"]["modules"]) for c in components),
        "git_commit_status": "NOT_VERIFIABLE_NO_HEAD" if not (root / ".git/HEAD").is_file() else "NOT_VERIFIED_BY_THIS_AUDIT",
        "online_ready_claimed": False, "native_process_started": False, "arm_performed": False,
        "scope": "R1.01 package/source/import identity only; not R1.02 protected-data freeze or R1.03 API conformance",
    }
    for filename, value in (("BASELINE.json", result), ("SOURCE_INVENTORY.json", before)):
        (output / filename).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"status": result["status"], "output_dir": str(output), "checks": checks,
                      "component_count": len(components), "imported_module_count": result["imported_module_count"]}))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
