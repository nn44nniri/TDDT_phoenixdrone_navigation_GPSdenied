from __future__ import annotations
import json
from pathlib import Path
import numpy as np


def fit_qlpv_npz(dataset_path, output_npz, output_json, ridge=1e-8):
    """Fit per-vertex affine x+ = A x + Ba a + Bd d + b from an offline NPZ dataset.

    Required arrays: X[n_samples,nx], X_next[n_samples,nx]. Optional: U, D, vertex.
    This is an offline identification utility; it is never invoked by the online Layer-4 path.
    """
    z=np.load(dataset_path,allow_pickle=True); X=np.asarray(z['X'],float); Y=np.asarray(z['X_next'],float)
    U=np.asarray(z['U'],float) if 'U' in z else np.zeros((len(X),0)); D=np.asarray(z['D'],float) if 'D' in z else np.zeros((len(X),0)); labels=np.asarray(z['vertex'],int) if 'vertex' in z else np.zeros(len(X),int)
    mats=[]
    for lab in sorted(set(labels.tolist())):
        mask=labels==lab; Z=np.hstack([X[mask],U[mask],D[mask],np.ones((mask.sum(),1))]); G=Z.T@Z+ridge*np.eye(Z.shape[1]); Theta=np.linalg.solve(G,Z.T@Y[mask])
        nx=X.shape[1]; nu=U.shape[1]; nd=D.shape[1]; mats.append((lab,Theta[:nx].T,Theta[nx:nx+nu].T,Theta[nx+nu:nx+nu+nd].T,Theta[-1]))
    A=np.stack([m[1] for m in mats]); Ba=np.stack([m[2] for m in mats]); Bd=np.stack([m[3] for m in mats]); b=np.stack([m[4] for m in mats])
    np.savez_compressed(output_npz,A=A,Ba=Ba,Bd=Bd,b=b)
    meta={"schema":"layer4-fitted-qlpv-v1","vertices":[int(m[0]) for m in mats],"samples":len(X),"ridge":ridge,"source_dataset":str(dataset_path),"offline_only":True}
    Path(output_json).write_text(json.dumps(meta,indent=2)+"\n",encoding='utf-8'); return meta


def deim_indices(U, m):
    """Greedy DEIM interpolation indices for a supplied nonlinear basis U."""
    U=np.asarray(U,float); m=min(int(m),U.shape[1]); inds=[]
    if m<=0:return np.asarray([],int)
    inds=[int(np.argmax(np.abs(U[:,0])))]
    for j in range(1,m):
        P=U[inds,:j]; rhs=U[inds,j]; c=np.linalg.lstsq(P,rhs,rcond=None)[0]; r=U[:,j]-U[:,:j]@c; inds.append(int(np.argmax(np.abs(r))))
    return np.asarray(inds,int)
