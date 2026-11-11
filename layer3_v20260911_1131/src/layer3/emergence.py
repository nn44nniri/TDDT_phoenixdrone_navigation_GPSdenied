from __future__ import annotations
import math
from typing import Any, Mapping
from .config import Layer3Config


def _flatten_numeric(state:Mapping[str,Any],prefix:str='')->dict[str,float]:
    out={}
    for k,v in state.items():
        name=f'{prefix}.{k}' if prefix else str(k)
        if isinstance(v,(int,float)) and math.isfinite(float(v)): out[name]=float(v)
        elif isinstance(v,(list,tuple)):
            for i,x in enumerate(v):
                if isinstance(x,(int,float)) and math.isfinite(float(x)): out[f'{name}[{i}]']=float(x)
    return out

class EmergenceMonitor:
    """Network-level emergence with actual runtime counterfactual/ablation evidence when available."""
    def __init__(self): self._baseline=None
    def evaluate(self,weights:Mapping[str,float],couplings:Mapping[str,float],mismatch:float|None,topology:Mapping[str,Any],*,counterfactuals:Mapping[str,Any]|None=None)->dict[str,Any]:
        ws=[max(float(v),1e-12) for v in weights.values()]; entropy=-sum(v*math.log(v) for v in ws)
        ks=[float(v) for v in couplings.values()]; kmean=sum(ks)/max(len(ks),1); active=len(topology.get('active_edges') or [])
        mm=float(mismatch) if mismatch is not None and math.isfinite(float(mismatch)) else 0.
        current={'weight_entropy':entropy,'mean_coupling':kmean,'mismatch':mm,'active_edges':float(active)}
        if self._baseline is None:self._baseline=dict(current)
        delta={k:float(current[k]-self._baseline.get(k,current[k])) for k in current}
        score=abs(delta['weight_entropy'])+abs(delta['mean_coupling'])+.01*abs(delta['mismatch'])
        cf=dict(counterfactuals or {}); actual={k:v for k,v in cf.items() if isinstance(v,Mapping) and v.get('actual_fmu_evaluation')}
        if actual:
            ablation={k:{**dict(v),'interpretation':'actual destination-FMU coupled-vs-direct counterfactual for this tick'} for k,v in actual.items()}
            method='actual_runtime_counterfactual'; available=True
        else:
            total=sum(abs(v) for v in ks) or 1.; ablation={k:{'removed_coupling':float(v),'relative_coupling_mass':abs(float(v))/total,'interpretation':'fallback sensitivity proxy'} for k,v in couplings.items() if k in set(topology.get('active_edges') or [])}
            method='fallback_first_order_proxy'; available=False
        return {'state':current,'delta_from_baseline':delta,'emergence_score':score,'ablation_sensitivity':ablation,
                'counterfactual_method':method,'actual_counterfactual_available':available,'certified_causal':False,
                'provenance':'3.2-29..31; actual runtime intervention evidence when adapter can execute it'}

class HiddenLoopMonitor:
    def __init__(self,config:Layer3Config|None=None): self.config=config
    @staticmethod
    def _cycles(topology:Mapping[str,Any],max_len:int=8)->list[list[str]]:
        nodes=list(topology.get('nodes') or []); adj={n:[] for n in nodes}
        for key in topology.get('active_edges') or []:
            if '->' in key:
                s,t=key.split('->',1); adj.setdefault(s,[]).append(t)
        found=set(); cycles=[]
        def canon(c):
            core=c[:-1]; rots=[tuple(core[i:]+core[:i]) for i in range(len(core))]; return min(rots)
        def dfs(start,cur,path):
            if len(path)>max_len:return
            for nxt in adj.get(cur,[]):
                if nxt==start and len(path)>=2:
                    key=canon(path+[start])
                    if key not in found: found.add(key); cycles.append(list(key)+[key[0]])
                elif nxt not in path: dfs(start,nxt,path+[nxt])
        for n in nodes: dfs(n,n,[n])
        return cycles
    @staticmethod
    def _distance(a:Mapping[str,Any],b:Mapping[str,Any])->float|None:
        fa,fb=_flatten_numeric(a),_flatten_numeric(b); keys=sorted(set(fa)&set(fb))
        if not keys:return None
        return math.sqrt(sum((fa[k]-fb[k])**2 for k in keys))
    def evaluate(self,couplings:Mapping[str,float],edge_normalized_residuals:Mapping[str,float],topology:Mapping[str,Any],*,counterfactual_states:Mapping[str,Any]|None=None)->dict[str,Any]:
        threshold=float(self.config.network.cycle_lci_threshold) if self.config else .5; maxlen=int(self.config.network.hidden_loop_max_cycle_length) if self.config else 8
        cycles=self._cycles(topology,maxlen); states=dict(counterfactual_states or {}); reports=[]
        for cyc in cycles:
            edges=[f'{cyc[i]}->{cyc[i+1]}' for i in range(len(cyc)-1)]; gain=1.; delay=0; residual=0.
            for e in edges:
                gain*=max(0.,float(couplings.get(e,0.))); residual=max(residual,float(edge_normalized_residuals.get(e,0.) or 0.)); delay+=int(((topology.get('edges') or {}).get(e) or {}).get('delay_ticks',0) or 0)
            lcis={}
            for node in set(cyc[:-1]):
                rec=states.get(node) or {}; d=self._distance(dict(rec.get('observed') or {}),dict(rec.get('direct') or {}))
                if d is not None: lcis[node]=d
            observed=max(lcis.values()) if lcis else None; proxy=gain*min(2.,residual)
            value=observed if observed is not None else proxy
            reports.append({'cycle':cyc,'edges':edges,'cycle_gain':gain,'cycle_delay_ticks':delay,'normalized_residual_factor':residual,
                            'lci_observed_vs_direct':observed,'lci_by_node':lcis,'lci_proxy':proxy,'lci':value,'threshold':threshold,'flagged':bool(value>threshold),
                            'observed_lci_available':observed is not None})
        max_lci=max((float(r['lci']) for r in reports),default=0.)
        return {'cycles':reports,'cycle_count':len(reports),'lci':max_lci,'lci_proxy':max_lci,'threshold':threshold,'flagged':bool(max_lci>threshold),
                'observed_lci_available':any(r['observed_lci_available'] for r in reports),
                'formula':'3.2-32..34; generic cycle enumeration + observed-vs-direct runtime counterfactual'}
