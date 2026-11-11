from __future__ import annotations

"""Adapter to the public ``twins_fmi`` package shipped by twins_layer2.

SARG does not implement FMI loading, FMU lifecycle, formula dispatch, or
Phoenix configuration parsing. Those responsibilities are delegated to the
independent Layer-2 library supplied with ``--twins-library``.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
import importlib
import sys


class TwinsLibraryError(RuntimeError):
    pass


@dataclass(frozen=True)
class TwinsAssets:
    root: Path
    wingmpet_fmu: Path
    mesa_nav_fmu: Path
    drone_config: Path
    python_dir: Path
    package_dir: Path


def discover_assets(root: str | Path, *, wingmpet_fmu: str | Path | None = None,
                    mesa_nav_fmu: str | Path | None = None,
                    drone_config: str | Path | None = None) -> TwinsAssets:
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Twins library directory not found: {root}")
    python_dir = root / "python"
    package_dir = python_dir / "twins_fmi"
    required = [
        package_dir / "__init__.py",
        package_dir / "library.py",
        package_dir / "formula_fmi.py",
        package_dir / "native_fmu.py",
        package_dir / "drone_config.py",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise TwinsLibraryError(
            "Invalid twins library; the public python/twins_fmi package is incomplete: "
            + ", ".join(missing)
        )

    wing = Path(wingmpet_fmu).expanduser().resolve() if wingmpet_fmu else root / "fmus" / "WingMPET.fmu"
    mesa = Path(mesa_nav_fmu).expanduser().resolve() if mesa_nav_fmu else root / "fmus" / "MESA_NAV.fmu"
    config = Path(drone_config).expanduser().resolve() if drone_config else root / "config" / "phoenix_drone_v3.yaml"
    for label, path in (("WingMPET FMU", wing), ("MESA_NAV FMU", mesa), ("Phoenix configuration", config)):
        if not path.is_file():
            raise FileNotFoundError(f"{label} not found: {path}")
    return TwinsAssets(root, wing, mesa, config, python_dir, package_dir)


def _import_public_package(python_dir: Path):
    path = str(python_dir)
    if path not in sys.path:
        sys.path.insert(0, path)
    try:
        module = importlib.import_module("twins_fmi")
    except Exception as exc:
        raise TwinsLibraryError(f"Cannot import public twins_fmi package from {python_dir}: {exc}") from exc
    required = ("TwinsLayer2", "FormulaFMU", "DroneConfig")
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        raise TwinsLibraryError(f"twins_fmi does not export required API: {missing}")
    return module


class TwinsLibrary:
    """Single integration point between SARG and Layer-2 twins."""

    def __init__(self, assets: TwinsAssets):
        self.assets = assets
        self.api = _import_public_package(assets.python_dir)
        self.DroneConfig = self.api.DroneConfig
        self.FormulaFMU = self.api.FormulaFMU
        self.TwinsLayer2 = self.api.TwinsLayer2
        self.config = self.DroneConfig(assets.drone_config)

    def load_drone_config(self):
        return self.config

    @contextmanager
    def open_manager(self) -> Iterator[Any]:
        """Open both independent FMUs through the comprehensive manager."""
        with self.TwinsLayer2(
            mesa_fmu=self.assets.mesa_nav_fmu,
            wingmpet_fmu=self.assets.wingmpet_fmu,
            config=self.config,
        ) as manager:
            yield manager

    @staticmethod
    def _catalog_count(fmu: Any) -> int:
        operations = getattr(fmu, "operations", None)
        return len(operations) if isinstance(operations, dict) else 0

    @staticmethod
    def _identity(fmu: Any) -> dict[str, Any]:
        if hasattr(fmu, "evaluate_operation"):
            return dict(fmu.evaluate_operation(1, use_config=True))
        raise TwinsLibraryError(f"Managed FMU {type(fmu).__name__} has no evaluate_operation()")

    def probe_all(self, allow_mock: bool = False) -> dict[str, dict[str, Any]]:
        """Probe both twins in one managed lifecycle."""
        try:
            with self.open_manager() as manager:
                result = {}
                for name, fmu in (("WingMPET", manager.wingmpet), ("MESA_NAV", manager.mesa)):
                    result[name] = {
                        "mode": "twins_fmi.TwinsLayer2",
                        "identity": self._identity(fmu),
                        "catalog_operations": self._catalog_count(fmu),
                        "library_root": str(self.assets.root),
                        "library_api": f"{type(manager).__module__}.{type(manager).__name__}",
                        "fmu_api": f"{type(fmu).__module__}.{type(fmu).__name__}",
                        "configuration_attached": getattr(fmu, "config", None) is self.config,
                    }
                return result
        except Exception as exc:
            if not allow_mock:
                raise
            common = {
                "mode": "mock",
                "error": str(exc),
                "library_root": str(self.assets.root),
                "library_api": "twins_fmi.TwinsLayer2",
            }
            return {
                "WingMPET": {**common, "identity": {"kind": "WingMPET"}},
                "MESA_NAV": {**common, "identity": {"kind": "MESA_NAV"}},
            }

    def probe(self, model: str, allow_mock: bool = False) -> dict[str, Any]:
        key = "WingMPET" if model.strip().lower().replace("-", "_") in {"wingmpet", "wing_mpet"} else "MESA_NAV"
        return self.probe_all(allow_mock=allow_mock)[key]

    def provenance(self) -> dict[str, Any]:
        return {
            "integration": "public_twins_fmi_package",
            "management": "comprehensive_dual_twin_manager",
            "library_root": str(self.assets.root),
            "python_package": str(self.assets.package_dir),
            "public_api": {
                "manager": "twins_fmi.TwinsLayer2",
                "fmu": "twins_fmi.FormulaFMU",
                "configuration": "twins_fmi.DroneConfig",
            },
            "assets": {
                "wingmpet_fmu": str(self.assets.wingmpet_fmu),
                "mesa_nav_fmu": str(self.assets.mesa_nav_fmu),
                "drone_config": str(self.assets.drone_config),
            },
        }
