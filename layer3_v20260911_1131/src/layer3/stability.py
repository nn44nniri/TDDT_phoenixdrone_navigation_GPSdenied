from __future__ import annotations
from typing import Any, Mapping
import numpy as np
from .config import Layer3Config

class StabilityMonitor:
    """Registry/topology-driven dynamic stability monitor for arbitrary N."""
    def __init__(self, config: Layer3Config): self.config=config
    def evaluate(self, context:Mapping[str,Any], topology:Mapping[str,Any], couplings:Mapping[str,float])->dict[str,Any]:
        if context.get('network_jacobian_discrete') is not None:
            A=np.asarray(context['network_jacobian_discrete'],dtype=float); eig=np.linalg.eigvals(A); rho=float(np.max(np.abs(eig))) if eig.size else 0.
            return {'certified':True,'domain':'discrete','stable':bool(rho<1.),'spectral_radius':rho,'eigenvalues':[[float(z.real),float(z.imag)] for z in eig],'criterion':'rho(A)<1'}
        if context.get('network_jacobian_continuous') is not None:
            A=np.asarray(context['network_jacobian_continuous'],dtype=float); eig=np.linalg.eigvals(A); alpha=float(np.max(np.real(eig))) if eig.size else -float('inf')
            return {'certified':True,'domain':'continuous','stable':bool(alpha<0.),'spectral_abscissa':alpha,'eigenvalues':[[float(z.real),float(z.imag)] for z in eig],'criterion':'max Re(lambda(A))<0'}
        nodes=list(topology.get('nodes') or [])
        idx={n:i for i,n in enumerate(nodes)}; A=np.eye(len(nodes))*float(self.config.network.stability_decay)
        active=set(topology.get('active_edges') or [])
        for key,rec in (topology.get('edges') or {}).items():
            if key not in active: continue
            s,t=rec.get('source'),rec.get('target')
            if s in idx and t in idx: A[idx[t],idx[s]]+=.10*float(couplings.get(key,0.))
        eig=np.linalg.eigvals(A) if len(nodes) else np.asarray([]); rho=float(np.max(np.abs(eig))) if eig.size else 0.
        return {'certified':False,'stable':None,'diagnostic_proxy_stable':bool(rho<1.),'diagnostic_spectral_radius':rho,
                'diagnostic_matrix':A.tolist(),'node_order':nodes,
                'claim':'diagnostic only; no dynamic stability certificate without an actual network Jacobian'}
