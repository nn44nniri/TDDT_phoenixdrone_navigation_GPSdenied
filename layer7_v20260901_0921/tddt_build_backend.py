"""Dependency-free PEP 517/660 backend for TDDT Layer 7.

The package is intentionally installable with ``pip install -e .`` without
requiring pip to download setuptools/wheel merely to create an isolated build
environment.  Runtime dependencies are still declared in project metadata.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
from pathlib import Path
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parent
NAME = "tddt-layer7"
DIST = "tddt_layer7"
VERSION = "0.3.0"
DIST_INFO = f"{DIST}-{VERSION}.dist-info"


def _metadata_text() -> str:
    return (
        "Metadata-Version: 2.1\n"
        f"Name: {NAME}\n"
        f"Version: {VERSION}\n"
        "Summary: Edge-oriented Gaussian/Bayesian error-feedback and safe-adaptation side module for TDDT Layer 7\n"
        "Requires-Python: >=3.10\n"
        "Requires-Dist: numpy>=1.24\n"
        "Requires-Dist: pytest>=7.0\n"
        "\n"
    )


def _wheel_text() -> str:
    return (
        "Wheel-Version: 1.0\n"
        "Generator: tddt-build-backend 1\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    )


def _entry_points_text() -> str:
    return "[console_scripts]\ntddt-layer7 = tddt_layer7.cli:main\n"


def _write_metadata_dir(metadata_directory: str) -> str:
    md = Path(metadata_directory) / DIST_INFO
    md.mkdir(parents=True, exist_ok=True)
    (md / "METADATA").write_text(_metadata_text(), encoding="utf-8")
    (md / "WHEEL").write_text(_wheel_text(), encoding="utf-8")
    (md / "entry_points.txt").write_text(_entry_points_text(), encoding="utf-8")
    return DIST_INFO


def get_requires_for_build_wheel(config_settings=None):
    return []


def get_requires_for_build_editable(config_settings=None):
    return []


def get_requires_for_build_sdist(config_settings=None):
    return []


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    return _write_metadata_dir(metadata_directory)


def prepare_metadata_for_build_editable(metadata_directory, config_settings=None):
    return _write_metadata_dir(metadata_directory)


def _record_row(path: str, data: bytes) -> tuple[str, str, str]:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
    return path, f"sha256={digest}", str(len(data))


def _build_zip(wheel_directory: str, editable: bool) -> str:
    filename = f"{DIST}-{VERSION}-py3-none-any.whl"
    out = Path(wheel_directory) / filename
    files: dict[str, bytes] = {}

    if editable:
        # PEP 660 editable wheel: site adds the live source tree to sys.path.
        pth = f"__editable__.{DIST}-{VERSION}.pth"
        files[pth] = (str((ROOT / "src").resolve()) + os.linesep).encode("utf-8")
    else:
        pkg_root = ROOT / "src" / "tddt_layer7"
        for src in sorted(pkg_root.rglob("*")):
            if src.is_file() and "__pycache__" not in src.parts and not src.name.endswith((".pyc", ".pyo")):
                rel = Path("tddt_layer7") / src.relative_to(pkg_root)
                files[rel.as_posix()] = src.read_bytes()

    files[f"{DIST_INFO}/METADATA"] = _metadata_text().encode("utf-8")
    files[f"{DIST_INFO}/WHEEL"] = _wheel_text().encode("utf-8")
    files[f"{DIST_INFO}/entry_points.txt"] = _entry_points_text().encode("utf-8")

    record_path = f"{DIST_INFO}/RECORD"
    rows = [_record_row(path, data) for path, data in files.items()]
    rows.append((record_path, "", ""))
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerows(rows)
    files[record_path] = buf.getvalue().encode("utf-8")

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, data in files.items():
            zf.writestr(path, data)
    return filename


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    return _build_zip(wheel_directory, editable=False)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    return _build_zip(wheel_directory, editable=True)


def build_sdist(sdist_directory, config_settings=None):
    filename = f"{DIST}-{VERSION}.tar.gz"
    out = Path(sdist_directory) / filename
    prefix = f"{DIST}-{VERSION}"
    out.parent.mkdir(parents=True, exist_ok=True)
    include = [
        "pyproject.toml", "setup.py", "tddt_build_backend.py", "README.md", "HOW_USAGE.md",
        "requirements.txt", "src", "configs", "schemas", "examples", "docs", "tests", "scripts",
    ]
    with tarfile.open(out, "w:gz") as tf:
        for name in include:
            p = ROOT / name
            if p.exists():
                tf.add(p, arcname=f"{prefix}/{name}")
    return filename
