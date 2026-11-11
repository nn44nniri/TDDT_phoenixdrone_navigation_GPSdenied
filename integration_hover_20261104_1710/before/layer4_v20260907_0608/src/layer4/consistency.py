from __future__ import annotations
import numpy as np


class WLSConsistency:
    """Velocity-triangle WLS CoC specialized from E4-50..E4-57 and Layer-3 CoC semantics."""
    def __init__(self,config,codec): self.config=config; self.codec=codec

    def evaluate(self,x,C,trust,topology):
        x=np.asarray(x,dtype=float); C=np.asarray(C,dtype=float); idx=self.codec.index
        groups={"MESA_NAV":[idx[f"Vg_{i}"] for i in range(3)],"WingMPET":[idx[f"Va_{i}"] for i in range(3)],"MicroMetTwin":[idx[f"W_{i}"] for i in range(3)]}
        offsets={"MESA_NAV":0,"WingMPET":3,"MicroMetTwin":6}
        reg=float(self.config.get("consistency.regularizer",1e-8)); M=np.eye(9)*reg; b=np.zeros(9); obs=[]
        raw=np.zeros(9)
        for twin,ii in groups.items():
            z=x[ii]; raw[offsets[twin]:offsets[twin]+3]=z
            cov=C[np.ix_(ii,ii)]; info=np.linalg.pinv(cov+np.eye(3)*1e-9); H=np.zeros((3,9)); H[:,offsets[twin]:offsets[twin]+3]=np.eye(3)
            w=max(float(trust.get(twin,0.0)),1e-6); M+=w*H.T@info@H; b+=w*H.T@info@z; obs.append((w,z,H,info))
        L=np.hstack([np.eye(3),-np.eye(3),-np.eye(3)])
        hard=bool(self.config.get("consistency.hard_velocity_triangle",False)); lam=float(self.config.get("consistency.constraint_lambda",20.0))
        if hard:
            # Exact constrained WLS solve through the KKT system; avoids treating a hard Layer-3 CoC relation as only a penalty.
            KKT=np.block([[M,L.T],[L,np.zeros((3,3))]])
            rhs=np.concatenate([b,np.zeros(3)])
            sol=np.linalg.lstsq(KKT,rhs,rcond=None)[0]; c=sol[:9]
        else:
            c=np.linalg.solve(M+lam*L.T@L,b)
        J=0.0
        for w,z,H,info in obs:
            r=z-H@c; J+=w*float(r.T@info@r)
        # Contractual CoC residual is evaluated on the reconciled common state, matching Layer-3 CoC semantics.
        # The raw velocity-triangle residual is retained as a diagnostic only and is not itself a certificate.
        r_raw=L@raw; r_contract=L@c
        active=set(topology.get("active_edges",[])); edges=topology.get("edges") or {}
        relevant={"MicroMetTwin->WingMPET","MicroMetTwin->MESA_NAV","MESA_NAV->WingMPET"}
        edge_residuals={}; rhos=[]
        for e in sorted(active & relevant):
            rec=edges.get(e,{}) or {}; tol=max(float(rec.get("tolerance",self.config.get("consistency.default_edge_tolerance",5.0))),1e-9)
            rho=float(np.linalg.norm(r_contract)/tol); edge_residuals[e]={"residual":r_contract.tolist(),"raw_residual_diagnostic":r_raw.tolist(),"tolerance":tol,"rho":rho}; rhos.append(rho)
        J+=lam*float(r_contract@r_contract)
        rho_max=max(rhos) if rhos else 0.0; mu=1.0-rho_max
        return {
            "dimensions":{"Vg":c[:3].tolist(),"Va":c[3:6].tolist(),"W":c[6:9].tolist()},
            "mismatch":float(J),"residual":r_contract.tolist(),"raw_residual_diagnostic":r_raw.tolist(),"normalized_residual":rho_max,"margin":float(mu),
            "edge_residuals":edge_residuals,"hard_constraint":hard,
        }
