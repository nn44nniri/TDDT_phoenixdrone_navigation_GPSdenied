from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import os, re, yaml
from .utils import deep_get, expand_path

@dataclass
class TDOCConfig:
    path: Path
    package_root: Path
    data: dict[str,Any]
    @classmethod
    def from_yaml(cls, path: str|Path) -> "TDOCConfig":
        p=Path(path).resolve(); d=yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls(path=p, package_root=p.parent.parent.resolve(), data=d)
    def get(self, dotted: str, default: Any=None) -> Any: return deep_get(self.data,dotted,default)
    @staticmethod
    def _expand_env_defaults(text: str) -> str:
        pat=re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]*)\}")
        text=pat.sub(lambda m:os.environ.get(m.group(1),m.group(2)),text)
        return os.path.expandvars(text)
    def path_value(self,key:str,*,required:bool=False)->Path|None:
        """Resolve external/module addresses only from environment exports.

        `configs/environment.sh` is the single address authority. The YAML `paths:`
        block is intentionally ignored so stale/copied config files cannot redirect
        TDOC to another package tree.
        """
        raw=os.environ.get(key)
        if raw in (None,""):
            if required: raise FileNotFoundError(f"{key} is not exported; source configs/environment.sh")
            return None
        p=expand_path(self._expand_env_defaults(str(raw)),self.package_root)
        if required and not p.exists(): raise FileNotFoundError(f"{key}: {p}")
        return p
    def bind_layer3_external_paths(self,cfg,*,required:bool=True)->dict[str,str]:
        """Bind Layer-3 external dependency paths only from TDOC environment.sh.

        The external Layer-3 package remains usable stand-alone, but integrated TDOC
        execution must not inherit its YAML ``${VAR:-fallback}`` dependency paths.
        This method overwrites every external Layer-3 path from the authoritative
        exports and fails closed when a required export is absent.
        """
        mapping={
            "twins_root":"TDDT_TWINS_ROOT",
            "climate_root":"TDDT_CLIMATE_ROOT",
            "tdoc_root":"TDDT_UNIT_ADAPTER_ROOT",
            "sarg_package_root":"TDDT_SARG_PACKAGE_ROOT",
            "sarg_root":"TDDT_SARG_ROOT",
            "boulder_root":"TDDT_BOULDER_ROOT",
            "map_root":"TDDT_MAP_ROOT",
            "crp_root":"TDDT_CRP_ROOT",
        }
        resolved={}
        paths=getattr(cfg,"paths",None)
        if paths is None:
            raise TypeError("Layer3 config has no paths contract")
        for attr,env_key in mapping.items():
            if required and os.environ.get(env_key) in (None,""):
                raise FileNotFoundError(f"{env_key} is not exported; source configs/environment.sh")
            # Generated roots such as SARG/MAP/CRP may legitimately be absent before
            # reset --refresh-external; authority is about the exported address, not
            # about pre-reset materialization. Package-specific preflights validate
            # existence when the corresponding component is actually invoked.
            value=self.path_value(env_key,required=False)
            if value is None:
                continue
            setattr(paths,attr,str(value))
            resolved[attr]=str(value)
        return resolved
    @property
    def runtime_root(self)->Path:
        raw=self.get("runtime.root","runtime"); p=expand_path(raw,self.package_root); p.mkdir(parents=True,exist_ok=True); return p
    @property
    def orchestration_version(self)->str: return str(self.get("package.orchestration_version","TDOC-runtime-v1"))
    @property
    def strict(self)->bool: return bool(self.get("runtime.strict",True))
    def layer_enabled(self,n:int)->bool: return bool(self.get(f"layers.layer{n}.enabled",False))
