from __future__ import annotations
import os
from pathlib import Path
from typing import Any
import yaml

class Layer5Config:
    def __init__(self, data: dict[str, Any], source: str | Path | None = None):
        self.data = data
        self.source = Path(source).resolve() if source else None
        self.package_root = (self.source.parent.parent if self.source else Path(__file__).resolve().parents[2])

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Layer5Config":
        p = Path(path).resolve()
        with p.open("r", encoding="utf-8") as f:
            return cls(yaml.safe_load(f) or {}, p)

    @classmethod
    def default(cls) -> "Layer5Config":
        root = Path(__file__).resolve().parents[2]
        return cls.from_yaml(root / "configs/configuration_phoenix.yaml")

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur: return default
            cur = cur[part]
        return cur

    def resolve(self, configured: str | Path | None) -> Path | None:
        if configured in (None, ""): return None
        p = Path(os.path.expandvars(os.path.expanduser(str(configured))))
        return p.resolve() if p.is_absolute() else (self.package_root / p).resolve()

    def env_path(self, name: str) -> Path | None:
        value = os.environ.get(name) or self.get(f"paths.{name}")
        return self.resolve(value)

    @property
    def version(self) -> str:
        return str(self.get("package.version", "unknown"))
