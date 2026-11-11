from __future__ import annotations
from dataclasses import dataclass

@dataclass
class Boundary:
    fast_index:int; meso_index:int; slow_index:int; fast:bool; meso:bool; slow:bool
    def label(self)->str:
        if self.slow:return "slow"
        if self.meso:return "meso"
        return "fast"

class TDOCScheduler:
    def __init__(self,fast_dt_s:float=1.0,meso_fast_steps:int=3,slow_meso_windows:int=2):
        self.fast_dt_s=max(float(fast_dt_s),1e-9); self.M=max(int(meso_fast_steps),1); self.L=max(int(slow_meso_windows),1)
    def boundary(self,reference_time_s:float)->Boundary:
        k=max(0,int(round(float(reference_time_s)/self.fast_dt_s)))
        meso=(k>0 and k%self.M==0); m=k//self.M
        slow=(meso and m>0 and m%self.L==0); q=m//self.L
        return Boundary(k,m,q,True,meso,slow)
