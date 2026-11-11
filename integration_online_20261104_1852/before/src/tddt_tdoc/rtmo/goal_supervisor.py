from __future__ import annotations
from dataclasses import dataclass
from typing import Any
import math

def haversine_m(lat1,lon1,lat2,lon2):
    r=6371008.8; a1=math.radians(lat1); a2=math.radians(lat2); da=a2-a1; dl=math.radians(lon2-lon1)
    h=math.sin(da/2)**2+math.cos(a1)*math.cos(a2)*math.sin(dl/2)**2
    return 2*r*math.asin(min(1.0,math.sqrt(max(0.0,h))))

@dataclass
class GoalSupervisor:
    goal_lat:float; goal_lon:float; goal_tolerance_m:float=25.0; margin_m:float=25.0
    min_distance_m:float|None=None; previous_distance_m:float|None=None; approach_armed:bool=False
    def update(self, *, lat:float|None,lon:float|None,groundspeed_m_s:float|None,latency_bound_s:float,transition_bound_s:float,ground_referenced:bool=False)->dict[str,Any]:
        if lat is None or lon is None:
            return {"valid":False,"approach_armed":self.approach_armed,"overshoot":False,"ground_referenced":ground_referenced,"source":"OPERATIONAL"}
        r=haversine_m(lat,lon,self.goal_lat,self.goal_lon)
        vg=max(0.0,float(groundspeed_m_s or 0.0)); arm=max(self.goal_tolerance_m,vg*(max(0.0,latency_bound_s)+max(0.0,transition_bound_s))+self.margin_m)
        if r<=arm:self.approach_armed=True
        if self.min_distance_m is None or r<self.min_distance_m:self.min_distance_m=r
        overshoot=bool(self.approach_armed and self.previous_distance_m is not None and r>self.previous_distance_m+max(2.0,0.02*arm) and self.min_distance_m<arm)
        self.previous_distance_m=r
        return {"valid":True,"goal_distance_m":r,"approach_arm_radius_m":arm,"approach_armed":self.approach_armed,"overshoot":overshoot,"ground_referenced":bool(ground_referenced),"source":"OPERATIONAL"}
