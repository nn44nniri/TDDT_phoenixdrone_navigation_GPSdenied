from __future__ import annotations
from typing import Any, Protocol
class LayerAdapter(Protocol):
    layer_id:int
    def preflight(self)->dict[str,Any]: ...
    def invoke(self,payload:dict[str,Any])->dict[str,Any]: ...
    def close(self)->None: ...
class FutureLayerAdapter:
    def __init__(self,layer_id:int): self.layer_id=layer_id
    def preflight(self): return {"ok":True,"implemented":False,"status":"NOT_IMPLEMENTED"}
    def invoke(self,payload): return {"ok":False,"status":"NOT_IMPLEMENTED","layer":self.layer_id,"input_preserved":True}
    def close(self): pass
