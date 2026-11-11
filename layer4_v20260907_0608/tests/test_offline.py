import json
import numpy as np
from layer4.offline import fit_qlpv_npz,deim_indices


def test_fit_qlpv_npz(tmp_path):
    rng=np.random.default_rng(4); X=rng.normal(size=(50,3)); U=rng.normal(size=(50,1)); D=rng.normal(size=(50,1)); A=np.diag([.9,.8,.7]); B=np.ones((3,1))*.1; E=np.ones((3,1))*.05; Y=X@A.T+U@B.T+D@E.T
    ds=tmp_path/'d.npz'; np.savez(ds,X=X,X_next=Y,U=U,D=D,vertex=np.zeros(50,int)); out=tmp_path/'m.npz'; meta=tmp_path/'m.json'; r=fit_qlpv_npz(ds,out,meta)
    z=np.load(out); assert z['A'].shape==(1,3,3) and np.allclose(z['A'][0],A,atol=1e-5); assert json.loads(meta.read_text())['offline_only'] is True

def test_deim_indices_unique():
    U=np.eye(6,4); i=deim_indices(U,4); assert len(i)==4 and len(set(i.tolist()))==4
