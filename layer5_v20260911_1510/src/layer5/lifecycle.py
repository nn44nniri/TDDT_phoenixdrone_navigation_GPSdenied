from __future__ import annotations
from typing import Any
from .config import Layer5Config

class EnvironmentRouter:
    MODES={"OFFLINE_SE","ONLINE_JSBSIM"}
    def __init__(self,config:Layer5Config): self.config=config
    def route(self,learning_mode:str)->dict[str,Any]:
        m=learning_mode.upper()
        if m not in self.MODES: raise ValueError(f"invalid learning mode {m}")
        if m=="OFFLINE_SE": return {"mode":m,"backend":"Layer2:{WingMPET,MESA_NAV,Climate}","layer6_call":False,"jsbsim_call":False}
        return {"mode":m,"backend":"PTLIB_Adapter∘JSBSim_L6","layer6_call":True,"jsbsim_call":True,"online_retraining":False}
