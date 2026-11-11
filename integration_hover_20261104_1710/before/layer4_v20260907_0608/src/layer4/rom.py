from __future__ import annotations
from pathlib import Path
import json, numpy as np


class PODDEIMSurrogate:
    """POD-DEIM interpolation surrogate with explicit validity envelope (E4-73..E4-78)."""
    def __init__(self,config):
        self.config=config; self.enabled=bool(config.get("rom.enabled",True))
        p=config.package_root/config.get("rom.basis_file"); m=config.package_root/config.get("rom.metadata_file")
        self.V=None; self.mean=None; self.indices=None
        if p.exists():
            z=np.load(p); self.V=np.asarray(z["V"],dtype=float); self.mean=np.asarray(z.get("mean",np.zeros(self.V.shape[0])),dtype=float)
            if "deim_indices" in z: self.indices=np.asarray(z["deim_indices"],dtype=int)
        self.meta=json.loads(m.read_text(encoding="utf-8")) if m.exists() else {}
        if self.V is not None and self.indices is None:
            self.indices=self.deim_indices(self.V,min(int(config.get("edge.deim_points_max",8)),self.V.shape[1]))

    @staticmethod
    def deim_indices(U,m):
        U=np.asarray(U,dtype=float); m=min(int(m),U.shape[1],U.shape[0])
        if m<=0:return np.asarray([],dtype=int)
        idx=[int(np.argmax(np.abs(U[:,0])))]
        for k in range(1,m):
            Uk=U[:,:k]; rhs=U[idx,k]; A=Uk[idx,:]
            c=np.linalg.lstsq(A,rhs,rcond=None)[0] if A.size else np.zeros(0)
            r=U[:,k]-Uk@c; order=np.argsort(-np.abs(r)); j=next((int(q) for q in order if int(q) not in idx),int(order[0])); idx.append(j)
        return np.asarray(idx,dtype=int)

    def reconstruct(self,X):
        X=np.asarray(X,dtype=float); Z=X-self.mean
        if self.V is None: return X.copy(),False
        if self.indices is None or len(self.indices)==0:
            return (Z@self.V)@self.V.T+self.mean,False
        PTV=self.V[self.indices,:]
        coeff=np.linalg.lstsq(PTV,Z[:,self.indices].T,rcond=None)[0].T
        rec=coeff@self.V.T+self.mean
        return rec,True

    def screen(self,trajectory):
        if not self.enabled or self.V is None: return {"valid":None,"error":None,"reason":"ROM disabled/unavailable","deim_used":False}
        X=np.asarray(trajectory,dtype=float); rec,deim_used=self.reconstruct(X); Z=X-self.mean
        err=float(np.linalg.norm(X-rec)/max(len(X),1)**0.5)
        radius=float(np.max(np.linalg.norm(Z,axis=1))); tol=float(self.config.get("rom.error_tolerance",5)); vr=float(self.config.get("rom.validity_radius",1e9))
        valid=bool(err<=tol and radius<=vr)
        return {"valid":valid,"error":err,"radius":radius,"reason":"inside" if valid else "outside_rom_validity","deim_used":deim_used,
                "deim_indices":self.indices.tolist() if self.indices is not None else []}

    @staticmethod
    def fit(snapshot_matrix,rank,deim_points=None):
        X=np.asarray(snapshot_matrix,dtype=float); mean=X.mean(axis=0); _,S,Vt=np.linalg.svd(X-mean,full_matrices=False); V=Vt[:rank].T
        idx=PODDEIMSurrogate.deim_indices(V,deim_points if deim_points is not None else min(rank,V.shape[0]))
        return mean,V,S,idx
