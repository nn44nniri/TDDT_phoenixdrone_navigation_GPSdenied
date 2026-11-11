from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json

@dataclass
class TopologyMode:
    mode_id:str; topology:dict[str,Any]; event:str; probability:float
    predecessor:str="G_nominal"
    transition_valid:bool=True
    def to_dict(self): return {"mode_id":self.mode_id,"topology":self.topology,"event":self.event,"probability":self.probability,"predecessor":self.predecessor,"transition_valid":self.transition_valid}

class DFATopologyManager:
    """File-authorized bounded DFA topology enumerator implementing E4-11..E4-13."""
    def __init__(self,config):
        self.config=config
        p=config.package_root/config.get("scenario.topology_file","resources/topology_dfa.json")
        self.spec=json.loads(Path(p).read_text(encoding="utf-8"))
        self.allowed_events=set(self.spec.get("events") or ["nominal","drop_nonmandatory_edge"])

    def _event_allowed(self,event_class:str)->bool:
        return event_class in self.allowed_events

    def modes(self,snapshot, max_modes:int|None=None)->list[TopologyMode]:
        max_modes=max_modes or self.config.max_topology_modes
        base=deepcopy(snapshot.topology)
        out=[]
        if self._event_allowed("nominal"):
            out.append(TopologyMode("G_nominal",base,"nominal",float(self.spec.get("nominal_probability",0.70))))
        edges=base.get("edges") or {}; removable=[]
        for key,rec in edges.items():
            if rec.get("active_effective",rec.get("active",1)) and not rec.get("mandatory",False):
                removable.append((float(rec.get("relevance",0.5)),key))
        removable.sort()
        if self._event_allowed("drop_nonmandatory_edge"):
            room=max(0,max_modes-len(out)); selected=removable[:room]
            mass=max(0.0,1.0-sum(x.probability for x in out)); each=mass/max(1,len(selected))
            for _,key in selected:
                g=deepcopy(base)
                if key in g.get("edges",{}):
                    g["edges"][key]["active_effective"]=0
                    g["edges"][key]["scenario_disabled"]=True
                g["active_edges"]=[e for e in g.get("active_edges",[]) if e!=key]
                out.append(TopologyMode(f"G_drop_{key.replace('->','_to_')}",g,f"drop:{key}",each,
                                        predecessor="G_nominal",transition_valid=True))
        if not out:
            out=[TopologyMode("G_nominal",base,"nominal",1.0)]
        s=sum(x.probability for x in out) or 1.0
        for x in out: x.probability/=s
        return out[:max_modes]

    def transition(self,current_mode:str,event:str,topology:dict[str,Any])->TopologyMode|None:
        """Explicit DFA transition helper used by tests/offline scenario tooling."""
        if event=="nominal" and self._event_allowed("nominal"):
            return TopologyMode("G_nominal",deepcopy(topology),event,1.0,current_mode,True)
        if event.startswith("drop:") and self._event_allowed("drop_nonmandatory_edge"):
            edge=event.split(":",1)[1]; rec=(topology.get("edges") or {}).get(edge)
            if not rec or rec.get("mandatory",False): return None
            g=deepcopy(topology); g["edges"][edge]["active_effective"]=0; g["edges"][edge]["scenario_disabled"]=True
            g["active_edges"]=[e for e in g.get("active_edges",[]) if e!=edge]
            return TopologyMode(f"G_drop_{edge.replace('->','_to_')}",g,event,1.0,current_mode,True)
        return None
