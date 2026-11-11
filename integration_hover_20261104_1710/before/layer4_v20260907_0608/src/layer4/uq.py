from __future__ import annotations
import numpy as np


class SquareRootCubatureUQ:
    """Spherical-radial square-root cubature propagation, E4-43..E4-49."""
    def __init__(self,config,codec,model): self.config=config; self.codec=codec; self.model=model

    @staticmethod
    def _sqrt_psd(C:np.ndarray)->np.ndarray:
        C=(np.asarray(C,dtype=float)+np.asarray(C,dtype=float).T)/2
        try:
            return np.linalg.cholesky(C+np.eye(C.shape[0])*1e-12)
        except np.linalg.LinAlgError:
            vals,vecs=np.linalg.eigh(C); vals=np.maximum(vals,1e-12)
            return vecs@np.diag(np.sqrt(vals))

    def initial_covariance(self,snapshot)->np.ndarray:
        n=self.codec.dim; C=np.eye(n)*1e-6
        utd=snapshot.uncertainty_td or {}; cov=np.asarray(utd.get("covariance") or [],dtype=float); ordering=utd.get("ordering") or []
        name_map={"Vg_N":"Vg_0","Vg_E":"Vg_1","Vg_D":"Vg_2","Va_N":"Va_0","Va_E":"Va_1","Va_D":"Va_2","W_N":"W_0","W_E":"W_1","W_D":"W_2"}
        if cov.ndim==2 and cov.shape[0]==len(ordering):
            for i,a in enumerate(ordering):
                for j,b in enumerate(ordering):
                    ia=self.codec.index.get(name_map.get(a,"")); ib=self.codec.index.get(name_map.get(b,""))
                    if ia is not None and ib is not None: C[ia,ib]=cov[i,j]
        C=(C+C.T)/2; vals,vecs=np.linalg.eigh(C); vals=np.maximum(vals,1e-9); return (vecs*vals)@vecs.T

    def initial_sqrt(self,snapshot)->np.ndarray:
        return self._sqrt_psd(self.initial_covariance(snapshot))

    def _active_indices(self):
        active=[]; mp={"Vg_N":"Vg_0","Vg_E":"Vg_1","Vg_D":"Vg_2","Va_N":"Va_0","Va_E":"Va_1","Va_D":"Va_2","W_N":"W_0","W_E":"W_1","W_D":"W_2"}
        for key in self.config.get("uq.active_ordering",[]):
            if mp.get(key) in self.codec.index: active.append(self.codec.index[mp[key]])
        return active[:self.config.max_uq_dim]

    def _affine_step_sqrt(self,m,S,a,d,trust,couplings,topology):
        """E4-v2 default: exact affine/qLPV moment propagation with conservative model noise."""
        m=np.asarray(m,dtype=float); S=np.asarray(S,dtype=float); nfull=len(m)
        A,Ba,Bd,b,_=self.model.matrices(trust,couplings,topology)
        # The mean follows the complete nominal predictor, including any
        # source-derived deterministic feed-forward term.  Covariance/error
        # propagation intentionally retains the packaged affine A matrix.
        mp,_=self.model.step(m,a,d,trust,couplings,topology)
        # P0-6: the propagated mean must obey intrinsic state-domain invariants.
        # Keep covariance propagation conservative; only the physical scalar mean is
        # projected, so uncertainty is not artificially collapsed at the boundary.
        mp,_=self.model.project_physical_state(mp)
        q=float(self.config.get("uq.process_noise_diag",0.0004))
        q*=float(self.config.get("uq.model_inflation",1.25))
        Sq=np.eye(nfull)*np.sqrt(max(q,0.0))
        M=np.hstack([A@S,Sq]); _,R=np.linalg.qr(M.T,mode="reduced"); Sp=R.T
        signs=np.where(np.diag(Sp)<0,-1.0,1.0); Sp=Sp@np.diag(signs)
        return mp,Sp

    def propagate_step_sqrt(self,m,S,a,d,trust,couplings,topology, *, force_cubature=False):
        if bool(self.config.get("uq.affine_default",True)) and not force_cubature:
            return self._affine_step_sqrt(m,S,a,d,trust,couplings,topology)
        m=np.asarray(m,dtype=float); S=np.asarray(S,dtype=float); nfull=len(m); active=self._active_indices()
        if not active:
            y,_=self.model.step(m,a,d,trust,couplings,topology); return y,S
        C=S@S.T; Ca=C[np.ix_(active,active)]; Sa=self._sqrt_psd(Ca)
        nu=len(active); Y=[]
        for j in range(nu):
            delta=np.sqrt(nu)*Sa[:,j]
            for sign in (1.0,-1.0):
                z=m.copy(); z[active]+=sign*delta
                y,_=self.model.step(z,a,d,trust,couplings,topology); Y.append(y)
        Y=np.asarray(Y); mp=Y.mean(axis=0); dev=(Y-mp).T/np.sqrt(2*nu)
        q=float(self.config.get("uq.process_noise_diag",0.0004)); Sq=np.eye(nfull)*np.sqrt(max(q,0.0))
        # QR on the transposed stacked deviation/process matrix gives a numerically stable square root.
        A=np.hstack([dev,Sq])
        _,R=np.linalg.qr(A.T,mode="reduced")
        Sp=R.T
        # Normalize signs for deterministic serialization/tests.
        diag=np.diag(Sp); signs=np.where(diag<0,-1.0,1.0); Sp=Sp@np.diag(signs)
        return mp,Sp

    def propagate_step(self,m,C,a,d,trust,couplings,topology):
        S=self._sqrt_psd(C); mp,Sp=self.propagate_step_sqrt(m,S,a,d,trust,couplings,topology)
        Cp=Sp@Sp.T; Cp=(Cp+Cp.T)/2
        return mp,Cp

    def rollout(self,m0,C0,actions,disturbances,trust_seq,coupling_seq,topology, *, return_roots:bool=False):
        m=np.asarray(m0,dtype=float); S=self._sqrt_psd(C0); ms=[m.copy()]; Ss=[S.copy()]; Cs=[S@S.T]
        for l,(a,d) in enumerate(zip(actions,disturbances)):
            g=topology[min(l,len(topology)-1)] if isinstance(topology,(list,tuple)) and topology else topology
            m,S=self.propagate_step_sqrt(m,S,a,d,trust_seq[l],coupling_seq[l],g)
            ms.append(m.copy()); Ss.append(S.copy()); Cs.append(S@S.T)
        if return_roots: return np.asarray(ms),np.asarray(Cs),np.asarray(Ss)
        return np.asarray(ms),np.asarray(Cs)
