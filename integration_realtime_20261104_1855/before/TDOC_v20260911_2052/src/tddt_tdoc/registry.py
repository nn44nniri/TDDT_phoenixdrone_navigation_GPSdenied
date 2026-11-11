from __future__ import annotations
from .models import LayerRegistration

ROLES={1:"Physical Multi-Domain System",2:"Domain Digital Twins",3:"Fused Trans-Domain Model",4:"Prediction / What-if / Optimization",5:"Decision",6:"Execution Environment",7:"Error Feedback + Online Adaptation"}
class LayerRegistry:
    def __init__(self,config): self.config=config; self.layers={}
    def build(self):
        self.layers[1]=LayerRegistration(1,"Layer 1",ROLES[1],"physical/event ingress",True,True,None,None,"EXTERNAL")
        specs={
            2:("TDDT_TWINS_ROOT","external direct API"),
            3:("TDDT_LAYER3_ROOT","external Layer3Engine"),
            4:("TDDT_LAYER4_ROOT","external Layer4Engine"),
            5:("TDDT_LAYER5_ROOT","external Layer5Engine exact-root adapter"),
            6:("TDDT_LAYER6_ROOT","External Layer6 formal contract + TDOC ONLINE_JSBSIM gateway"),
            7:("TDDT_LAYER7_ROOT","external Layer7SideModule adaptation adapter"),
        }
        for n,(key,implementation) in specs.items():
            p=self.config.path_value(key); enabled=self.config.layer_enabled(n); implemented=n in (2,3,4,5,6,7)
            if n==6: health="AVAILABLE"
            else: health="AVAILABLE" if p and p.exists() else "MISSING"
            self.layers[n]=LayerRegistration(n,f"Layer {n}",ROLES[n],implementation,enabled,implemented,str(p) if p else None,None,health,{"path_key":key,"invocation_owner":"TDOC/RuntimeScheduler"})
        return self.snapshot()
    def snapshot(self): return {str(k):v.to_dict() for k,v in self.layers.items()}
