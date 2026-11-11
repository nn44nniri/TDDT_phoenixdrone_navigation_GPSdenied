from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


class UnitAdapterBinding:
    """TDOC-owned binding to the external ISO/unit/frame/semantic adapter.

    TDOC owns *which* adapter package is admitted and routed to Layer 3.1.
    Layer 3.1 owns the actual canonicalization/alignment operation.  This split
    prevents both silent adapter drift and duplicate normalization in TDOC.
    """

    EXPECTED_BASENAME = "TDOC_ISO_Universal_Unit_Conversion_Standard20260824_0914"
    MIN_VERSION = (2, 2, 0)
    REQUIRED_FILES = (
        "pyproject.toml",
        "docs/TDOC_ISO_Universal_Unit_Conversion_Standard_v2.md",
        "src/tdoc_unit_converter/__init__.py",
        "src/tdoc_unit_converter/converter.py",
        "src/tdoc_unit_converter/adapters.py",
        "src/tdoc_unit_converter/errors.py",
        "src/tdoc_unit_converter/registry.py",
    )
    REQUIRED_ADAPTER_SYMBOLS = (
        "enu_to_ned", "ned_to_enu", "ned_enu_covariance",
        "frd_to_flu", "flu_to_frd", "frd_flu_covariance",
        "msl_to_agl", "agl_to_msl",
        "wgs84_to_ecef", "ecef_to_wgs84", "transform_crs",
        "common_time_to_elapsed", "elapsed_to_common_time",
        "convert_quaternion", "quaternion_covariance",
        "validate_data_semantics", "adapt_data_semantics",
        "propagate_covariance_numeric",
        "pressure_reference", "wind_from_to_vector",
        "cas_to_tas", "tas_to_cas",
    )

    def __init__(self, config):
        self.config = config
        self._cached: dict[str, Any] | None = None
        self._active_root: Path | None = None

    @staticmethod
    def _same_path(a: Path | None, b: Path | None) -> bool:
        if a is None or b is None:
            return True
        try:
            return a.resolve() == b.resolve()
        except Exception:
            return str(a) == str(b)

    def roots(self) -> dict[str, Any]:
        # TDDT_UNIT_ADAPTER_ROOT is the single authoritative external adapter path.
        # The TDOC orchestration package itself is self-located from its config/package root
        # and therefore does not require a TDDT_TDOC_ROOT environment variable.
        explicit = self.config.path_value("TDDT_UNIT_ADAPTER_ROOT")
        return {
            "root": explicit,
            "unit_adapter_root": explicit,
        }

    @property
    def root(self) -> Path | None:
        return self.roots()["root"]

    @staticmethod
    def _version_tuple(text: str) -> tuple[int, ...]:
        out = []
        for part in str(text).split("."):
            digits = "".join(ch for ch in part if ch.isdigit())
            if not digits:
                break
            out.append(int(digits))
        return tuple(out)

    def _probe_subprocess(self, root: Path) -> dict[str, Any]:
        code = r'''
import json
from tdoc_unit_converter import UnitConverter, convert, __version__
from tdoc_unit_converter import adapters as av
required = json.loads(__import__('os').environ['TDOC_REQUIRED_SYMBOLS'])
missing = [x for x in required if not hasattr(av, x)]
smoke = {}
try:
    smoke['unit_1000mm_to_m'] = float(convert(1000.0, 'mm', 'm'))
    smoke['enu_to_ned'] = list(av.enu_to_ned((1.0,2.0,3.0)))
    smoke['semantic_same_role'] = bool(av.validate_data_semantics(source_role='current_state', target_role='current_state'))
    smoke_ok = abs(smoke['unit_1000mm_to_m']-1.0) < 1e-12 and smoke['enu_to_ned'] == [2.0,1.0,-3.0] and smoke['semantic_same_role']
except Exception as exc:
    smoke_ok = False
    smoke['error'] = repr(exc)
print(json.dumps({'version':__version__,'missing_symbols':missing,'smoke':smoke,'smoke_ok':smoke_ok}))
'''
        env = dict(os.environ)
        src = str(root / "src")
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["TDOC_REQUIRED_SYMBOLS"] = json.dumps(list(self.REQUIRED_ADAPTER_SYMBOLS))
        p = subprocess.run([sys.executable, "-c", code], env=env, text=True, capture_output=True, timeout=20)
        if p.returncode != 0:
            return {"ok": False, "error": (p.stderr or p.stdout).strip(), "returncode": p.returncode}
        try:
            data = json.loads(p.stdout.strip().splitlines()[-1])
        except Exception as exc:
            return {"ok": False, "error": f"adapter probe output parse failed: {exc}", "stdout": p.stdout[-2000:]}
        vt = self._version_tuple(data.get("version", ""))
        data["version_ok"] = vt >= self.MIN_VERSION
        data["api_ok"] = not data.get("missing_symbols")
        data["ok"] = bool(data["version_ok"] and data["api_ok"] and data.get("smoke_ok"))
        return data

    def preflight(self, *, refresh: bool = False) -> dict[str, Any]:
        if self._cached is not None and not refresh:
            return dict(self._cached)
        roots = self.roots()
        root = roots["root"]
        report: dict[str, Any] = {
            "role": "external ISO unit/frame/time/semantic/covariance adapter",
            "binding_owner": "TDOC/RuntimeScheduler",
            "conversion_owner": "Layer3.1/TDOCAdapter",
            "expected_package": self.EXPECTED_BASENAME,
            "minimum_api_version": ".".join(map(str, self.MIN_VERSION)),
            "root": str(root) if root else None,
            "unit_adapter_root": str(roots["unit_adapter_root"]) if roots["unit_adapter_root"] else None,
        }
        if root is None or not root.exists() or not root.is_dir():
            report.update({"ok": False, "missing": ["root"], "api_probe": {"ok": False, "error": "root missing"}})
            self._cached = report
            return dict(report)
        missing = [rel for rel in self.REQUIRED_FILES if not (root / rel).exists()]
        basename_ok = root.name == self.EXPECTED_BASENAME
        probe = self._probe_subprocess(root) if not missing else {"ok": False, "error": "required files missing"}
        report.update({
            "basename_ok": basename_ok,
            "missing": missing,
            "api_probe": probe,
            "ok": bool(basename_ok and not missing and probe.get("ok")),
        })
        self._cached = report
        return dict(report)

    def activate(self) -> dict[str, Any]:
        """Bind the exact validated external package into this process.

        This does not transform data. It only makes the validated package the
        single source for Layer 3.1's strict TDOCAdapter implementation.
        """
        pf = self.preflight()
        if not pf.get("ok"):
            raise RuntimeError(f"ISO adapter preflight failed: {pf}")
        root = Path(pf["root"]).resolve()
        src = (root / "src").resolve()
        os.environ["TDDT_UNIT_ADAPTER_ROOT"] = str(root)
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        mod = importlib.import_module("tdoc_unit_converter")
        origin = Path(getattr(mod, "__file__", "")).resolve()
        if src not in origin.parents:
            raise RuntimeError(f"tdoc_unit_converter resolved from unexpected location: {origin}; expected under {src}")
        self._active_root = root
        return {
            "ok": True,
            "root": str(root),
            "module_origin": str(origin),
            "version": getattr(mod, "__version__", None),
            "binding_owner": "TDOC/RuntimeScheduler",
            "conversion_owner": "Layer3.1/TDOCAdapter",
        }

    def bind_layer3_config(self, cfg) -> dict[str, Any]:
        active = self.activate()
        # Layer3Config resolves this field directly, so this assignment makes
        # TDOC's configured adapter root authoritative even if Layer 3's YAML
        # contains a different fallback or the legacy environment name is absent.
        cfg.paths.tdoc_root = active["root"]
        return active
