from __future__ import annotations
import importlib, sys
from typing import Any

class ExternalLayerPluginAdapter:
    """Generic future-layer adapter loaded only from an externally configured package.

    The external factory must return an object exposing preflight(), invoke(payload),
    and optionally close(). TDOC remains the invocation owner.
    """
    def __init__(self,config,layer_id:int):
        self.config=config; self.layer_id=layer_id; self.instance=None
        self.root=config.path_value(f"TDDT_LAYER{layer_id}_ROOT")
        self.factory_spec=config.get(f"layers.layer{layer_id}.factory")
    def preflight(self):
        if not self.factory_spec:
            return {"ok":True,"implemented":False,"status":"NOT_IMPLEMENTED","root":str(self.root) if self.root else None}
        if not self.root or not self.root.exists(): return {"ok":False,"implemented":True,"status":"MISSING_ROOT","root":str(self.root) if self.root else None}
        return {"ok":True,"implemented":True,"status":"CONFIGURED","root":str(self.root),"factory":self.factory_spec}
    def _ensure(self):
        if self.instance is not None:return
        pf=self.preflight()
        if not pf.get("implemented"): raise RuntimeError(f"Layer {self.layer_id} is not implemented")
        if not pf.get("ok"): raise RuntimeError(f"Layer {self.layer_id} plugin preflight failed")
        src=self.root/"src"
        for p in (src,self.root):
            if p.exists() and str(p) not in sys.path: sys.path.insert(0,str(p))
        module_name,sep,symbol=str(self.factory_spec).partition(":")
        if not sep: raise ValueError("future layer factory must use module:factory syntax")
        factory=getattr(importlib.import_module(module_name),symbol)
        self.instance=factory(self.config) if callable(factory) else factory
    def invoke(self,payload:dict[str,Any])->dict[str,Any]:
        self._ensure(); out=self.instance.invoke(payload)
        if hasattr(out,"to_dict"): out=out.to_dict()
        d=dict(out); d.setdefault("provenance",{})["invocation_owner"]="TDOC/RuntimeScheduler"; return d
    def close(self):
        try:
            if self.instance is not None and hasattr(self.instance,"close"): self.instance.close()
        finally:self.instance=None
