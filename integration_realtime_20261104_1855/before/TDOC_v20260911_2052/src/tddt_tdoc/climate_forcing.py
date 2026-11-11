from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ACTIVE_MANIFEST_NAME = "tdoc_active_forcing.json"


def forcing_filename(hours: int) -> str:
    h = int(hours)
    if h <= 0:
        raise ValueError("climate forcing horizon must be > 0 hours")
    return f"openmeteo_{h}h.csv"


def climate_dataset_dir(climate_root: str | Path) -> Path:
    return Path(climate_root).expanduser().resolve() / "climate_dataset"


def active_manifest_path(climate_root: str | Path) -> Path:
    return climate_dataset_dir(climate_root) / ACTIVE_MANIFEST_NAME


def write_active_manifest(climate_root: str | Path, forcing_path: str | Path, *, hours: int, source: str = "TDOC prepare_all") -> Path:
    root = Path(climate_root).expanduser().resolve()
    raw = Path(forcing_path).expanduser()
    # Keep a package-local symlink as the manifest identity; resolving it here would
    # point outside climate_dataset into the persistent internet cache and falsely
    # reject the single-copy runtime materialization.
    p = raw.absolute() if raw.is_symlink() else raw.resolve()
    ds = climate_dataset_dir(root)
    try:
        rel = p.relative_to(ds)
    except ValueError as exc:
        raise ValueError("active climate forcing must remain inside TDDT_CLIMATE_ROOT/climate_dataset") from exc
    ds.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "TDOC-active-climate-forcing-v1",
        "hours": int(hours),
        "file": rel.as_posix(),
        "source": source,
    }
    out = active_manifest_path(root)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out




def central_climate_dataset_dir() -> Path | None:
    raw = os.environ.get("TDDT_CLIMATE_INTERNET_DATASETS_ROOT", "").strip()
    return Path(raw).expanduser().resolve() if raw else None

def central_active_manifest_path() -> Path | None:
    ds = central_climate_dataset_dir()
    return (ds / ACTIVE_MANIFEST_NAME) if ds else None

def write_central_active_manifest(forcing_path: str | Path, *, hours: int, source: str = "TDOC prepare_all central cache") -> Path:
    ds = central_climate_dataset_dir()
    if ds is None:
        raise ValueError("TDDT_CLIMATE_INTERNET_DATASETS_ROOT is not configured")
    p = Path(forcing_path).expanduser().resolve()
    try:
        rel = p.relative_to(ds)
    except ValueError as exc:
        raise ValueError("central active climate forcing must remain inside TDDT_CLIMATE_INTERNET_DATASETS_ROOT") from exc
    ds.mkdir(parents=True, exist_ok=True)
    out = ds / ACTIVE_MANIFEST_NAME
    out.write_text(json.dumps({"schema":"TDOC-central-active-climate-forcing-v1","hours":int(hours),"file":rel.as_posix(),"source":source}, indent=2)+"\n", encoding="utf-8")
    return out

def _candidate_from_central_manifest() -> Path | None:
    mp = central_active_manifest_path()
    ds = central_climate_dataset_dir()
    if mp is None or ds is None or not mp.is_file():
        return None
    try:
        data=json.loads(mp.read_text(encoding="utf-8")); raw=str((data or {}).get("file") or "").strip()
        if not raw: return None
        p=Path(raw); return (p if p.is_absolute() else ds/p).resolve()
    except Exception:
        return None

def _candidate_from_env(climate_root: Path) -> Path | None:
    raw = os.environ.get("TDDT_CLIMATE_FORCING_FILE", "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = climate_dataset_dir(climate_root) / p
    return p.resolve()


def _candidate_from_manifest(climate_root: Path) -> Path | None:
    mp = active_manifest_path(climate_root)
    if not mp.is_file():
        return None
    try:
        data: Any = json.loads(mp.read_text(encoding="utf-8"))
        raw = str((data or {}).get("file") or "").strip()
        if not raw:
            return None
        p = Path(raw)
        if not p.is_absolute():
            p = climate_dataset_dir(climate_root) / p
        return p.resolve()
    except Exception:
        return None


def resolve_climate_forcing(climate_root: str | Path | None, *, explicit: str | Path | None = None, default_hours: int = 24, require_exists: bool = False) -> Path | None:
    """Resolve one authoritative forcing path with fail-closed override semantics.

    Priority is explicit request -> environment override -> generated active manifest ->
    default horizon. If a higher-priority authoritative selector exists but points to a
    missing file, ``require_exists=True`` returns ``None`` rather than silently falling
    back to an older horizon.
    """
    if climate_root is None:
        return None
    root = Path(climate_root).expanduser().resolve()
    ds = climate_dataset_dir(root)

    if explicit:
        p = Path(explicit).expanduser()
        p = (p if p.is_absolute() else ds / p).resolve()
        return p if (not require_exists or p.is_file()) else None

    raw_env = os.environ.get("TDDT_CLIMATE_FORCING_FILE", "").strip()
    if raw_env:
        p = Path(raw_env).expanduser()
        p = (p if p.is_absolute() else ds / p).resolve()
        return p if (not require_exists or p.is_file()) else None

    manifest = active_manifest_path(root)
    if manifest.is_file():
        p = _candidate_from_manifest(root)
        if p is None:
            return None if require_exists else (ds / forcing_filename(default_hours)).resolve()
        return p if (not require_exists or p.is_file()) else None

    central = _candidate_from_central_manifest()
    if central is not None:
        return central if (not require_exists or central.is_file()) else None
    cds = central_climate_dataset_dir()
    if cds is not None:
        cp=(cds/forcing_filename(default_hours)).resolve()
        if cp.is_file() or not require_exists:
            return cp

    p = (ds / forcing_filename(default_hours)).resolve()
    return p if (not require_exists or p.is_file()) else None


def generated_forcing_targets(climate_root: str | Path) -> list[Path]:
    """Return TDOC-managed forcing artifacts only; never return the climate package root."""
    ds = climate_dataset_dir(climate_root)
    out: list[Path] = []
    if ds.is_dir():
        # Preserve the package-local link identity.  A runtime materialization may be
        # a symlink into the integrated-root internet cache; reset must unlink the
        # link, never resolve and delete the persistent downloaded dataset.
        out.extend(sorted(p.absolute() for p in ds.glob("openmeteo_*h.csv") if p.is_file() or p.is_symlink()))
    mp = active_manifest_path(climate_root)
    if mp.exists() or mp.is_symlink():
        out.append(mp.absolute())
    return out
