from __future__ import annotations
import importlib, sys
from pathlib import Path
from typing import Any
from .config import Layer5Config

class TDOCManager:
    """Layer-boundary owner/probe. TDOC remains the unit/frame/time/semantic authority."""
    def __init__(self, config: Layer5Config):
        self.config=config; self.root=config.env_path("TDDT_UNIT_ADAPTER_ROOT"); self.module=None; self.error=None
        self._load()

    def _load(self):
        if not self.root or not self.root.exists():
            self.error="TDDT_UNIT_ADAPTER_ROOT unavailable"; return
        src=self.root/"src"
        if src.exists() and str(src) not in sys.path: sys.path.insert(0,str(src))
        try: self.module=importlib.import_module("tdoc_unit_converter")
        except Exception as e: self.error=repr(e)

    def probe(self) -> dict[str,Any]:
        symbols=["UnitConverter","convert","enu_to_ned","ned_to_enu","validate_data_semantics","adapt_data_semantics"]
        missing=[] if self.module is None else [s for s in symbols if not hasattr(self.module,s)]
        return {"ok":self.module is not None and not missing,"root":str(self.root) if self.root else None,
                "version":getattr(self.module,"__version__",None) if self.module else None,"missing_symbols":missing,"error":self.error,
                "role":"layer boundary / unit-frame-time-semantic-covariance authority"}

    def validate_layer4_envelope(self, data: dict[str,Any]) -> dict[str,Any]:
        reasons=[]
        if self.config.get("contract.require_layer4_no_actuator_command",True) and data.get("actuator_command") is not False:
            reasons.append("layer4_actuator_command_boundary_violation")
        if self.config.get("contract.require_layer4_no_network_commit",True) and data.get("network_commit") is not False:
            reasons.append("layer4_network_commit_boundary_violation")
        if not data.get("snapshot_version"): reasons.append("missing_snapshot_version")
        if not data.get("snapshot_hash"): reasons.append("missing_snapshot_hash")
        return {"ok":not reasons,"reasons":reasons,"canonicalization_owner":"TDOC","network_write_authorized":False}

    def convert(self, value: Any, source_unit: str, target_unit: str) -> Any:
        if not self.module: raise RuntimeError("TDOC unavailable")
        r=self.module.convert(value,source_unit,target_unit)
        return getattr(r,"value",r)
