from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json, numpy as np
from .utils import get_path, as_vector, finite_float

@dataclass
class StateField:
    name:str; path:str; length:int=1; default:float=0.0

class StateCodec:
    def __init__(self, schema_path: str|Path):
        d=json.loads(Path(schema_path).read_text(encoding="utf-8")); self.fields=[StateField(**x) for x in d["fields"]]
        self.names=[]
        for f in self.fields:
            self.names += [f.name] if f.length==1 else [f"{f.name}_{i}" for i in range(f.length)]
        self.index={n:i for i,n in enumerate(self.names)}

    @property
    def dim(self): return len(self.names)

    def encode(self,snapshot)->np.ndarray:
        base={"std":snapshot.std,"context":snapshot.context,"coc":snapshot.center_of_consistency}
        vals=[]
        for f in self.fields:
            x=get_path(base,f.path,None); vals.extend(as_vector(x,f.length,f.default).tolist())
        return np.asarray(vals,dtype=float)

    def decode(self,x)->dict[str,Any]:
        a=np.asarray(x,dtype=float).reshape(-1); out={}; p=0
        for f in self.fields:
            seg=a[p:p+f.length]; out[f.name]=float(seg[0]) if f.length==1 else seg.tolist(); p+=f.length
        return out

    def slice(self,prefix:str)->np.ndarray:
        idx=[i for i,n in enumerate(self.names) if n==prefix or n.startswith(prefix+"_")]
        return np.asarray(idx,dtype=int)
