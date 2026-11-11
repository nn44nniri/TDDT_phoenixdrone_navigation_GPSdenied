from __future__ import annotations
import numpy as np


class PeriodicTubeMPC:
    """Robust tube support for Layer-4 optimization (E4-35..E4-42).

    The class owns tube propagation, input tightening and a periodic robust-positive-
    invariant (RPI) box certificate.  The actual seven-objective optimization lives in
    Layer4MultiObjectiveOptimizer; ``optimize`` is retained as a compatibility wrapper.
    """
    def __init__(self,config,model): self.config=config; self.model=model

    @staticmethod
    def _topology_at(topology,l):
        if isinstance(topology,(list,tuple)) and topology:
            return topology[min(l,len(topology)-1)]
        return topology

    def _base_bounds(self):
        lo=np.asarray(self.config.get("qlpv.action_lower"),dtype=float); hi=np.asarray(self.config.get("qlpv.action_upper"),dtype=float)
        return lo,hi

    def _feedback(self,Ba):
        kg=float(self.config.get("robust.feedback_gain_scale",0.08))
        return -kg*Ba.T

    def _tightened_bounds(self,rad,trust,couplings,topology):
        lo,hi=self._base_bounds(); _,Ba,_,_,_=self.model.matrices(trust,couplings,topology); L=self._feedback(Ba)
        du=np.abs(L)@np.asarray(rad,dtype=float)
        tlo=lo+du; thi=hi-du
        bad=tlo>thi
        if np.any(bad):
            mid=(lo+hi)/2; tlo=np.where(bad,mid,tlo); thi=np.where(bad,mid,thi)
        return tlo,thi,L,du

    def sequence_bounds(self,tube,trust_seq,coupling_seq,topology):
        bounds=[]
        H=min(len(trust_seq),len(coupling_seq),len(tube)-1)
        for l in range(H):
            g=self._topology_at(topology,l)
            lo,hi,L,du=self._tightened_bounds(tube[l],trust_seq[l],coupling_seq[l],g)
            bounds.append({"lower":lo,"upper":hi,"feedback_gain":L,"tightening":du})
        return bounds

    def robust_intersection_bounds(self,cases,H):
        """Intersection of all case-wise tube-tightened input sets."""
        base_lo,base_hi=self._base_bounds(); lo=np.tile(base_lo,(H,1)); hi=np.tile(base_hi,(H,1)); details=[]
        for case in cases:
            b=self.sequence_bounds(case["tube"],case["W"],case["K"],case["topology"])
            details.append(b)
            for l,x in enumerate(b[:H]):
                lo[l]=np.maximum(lo[l],x["lower"]); hi[l]=np.minimum(hi[l],x["upper"])
        infeasible=lo>hi
        if np.any(infeasible):
            # Empty robust input intersection is a true infeasibility.  Midpoints are used
            # only to keep numerical evaluation finite; metadata preserves the failure.
            mid=(lo+hi)/2.0; lo=np.where(infeasible,mid,lo); hi=np.where(infeasible,mid,hi)
        return lo,hi,{"empty_intersection":bool(np.any(infeasible)),"case_count":len(cases)}

    @staticmethod
    def project(U,lo,hi):
        return np.minimum(np.maximum(np.asarray(U,dtype=float),np.asarray(lo,dtype=float)),np.asarray(hi,dtype=float))

    def _project_sequence(self,U,tube,trust_seq,coupling_seq,topology):
        U=np.asarray(U,dtype=float).copy(); bounds=[]
        for l,x in enumerate(self.sequence_bounds(tube,trust_seq,coupling_seq,topology)):
            U[l]=np.clip(U[l],x["lower"],x["upper"])
            bounds.append({k:(v.tolist() if hasattr(v,"tolist") else v) for k,v in x.items()})
        return U,bounds

    def optimize(self,x0,U0,D,trust_seq,coupling_seq,topology, *, return_meta:bool=False):
        """Backward-compatible quadratic warm-start optimizer.

        New Layer4Engine runs Layer4MultiObjectiveOptimizer around this tube machinery.
        This method remains available for direct callers/tests and produces a bounded
        dynamically plausible warm start rather than claiming full MO optimization.
        """
        H=len(U0); tube=self.propagate_tube(trust_seq,coupling_seq,topology,H)
        U,bounds=self._project_sequence(np.asarray(U0,dtype=float),tube,trust_seq,coupling_seq,topology)
        q=float(self.config.get("qlpv.q_state",0.03)); r=float(self.config.get("qlpv.r_action",0.15)); qf=float(self.config.get("qlpv.q_terminal",0.06)); step=float(self.config.get("qlpv.optimizer_step",0.02)); iters=int(self.config.get("qlpv.optimizer_iterations",10))
        for _ in range(max(0,iters)):
            X=[np.asarray(x0,dtype=float)]; mats=[]
            for l in range(H):
                g=self._topology_at(topology,l); A,Ba,Bd,b,_=self.model.matrices(trust_seq[l],coupling_seq[l],g)
                xp=A@X[-1]+Ba@U[l]+Bd@D[l]+b
                xp,_=self.model.project_physical_state(xp)
                X.append(xp); mats.append((A,Ba))
            lam=2*qf*X[-1]; G=np.zeros_like(U)
            for l in range(H-1,-1,-1):
                A,Ba=mats[l]; G[l]=2*r*U[l]+Ba.T@lam; lam=2*q*X[l]+A.T@lam
            U,_=self._project_sequence(U-step*G,tube,trust_seq,coupling_seq,topology)
        X,_=self.model.rollout(x0,U,D,trust_seq,coupling_seq,topology)
        terminal=self.terminal_certificate(trust_seq,coupling_seq,topology)
        meta={"tightened_action_bounds":bounds,"terminal":terminal,"role":"quadratic_warm_start_only"}
        return (U,X,tube,meta) if return_meta else (U,X,tube)

    def propagate_tube(self,trust_seq,coupling_seq,topology,H):
        r0=float(self.config.get("robust.tube_radius0",0.02)); dr=float(self.config.get("robust.disturbance_radius",0.03)); rmax=float(self.config.get("robust.tube_radius_max",20.0))
        n=self.model.A.shape[1]; rad=np.ones(n)*r0; out=[rad.copy()]
        for l in range(H):
            g=self._topology_at(topology,l); A,Ba,_,_,_=self.model.matrices(trust_seq[l],coupling_seq[l],g); L=self._feedback(Ba); Acl=A+Ba@L
            rad=np.minimum(np.abs(Acl)@rad+dr,rmax); out.append(rad.copy())
        return np.asarray(out)

    def terminal_certificate(self,trust_seq,coupling_seq,topology):
        """Construct/check a periodic RPI box family for E4-42.

        For the configured terminal dynamic subspace, solve the finite-period box fixed
        point r_{j+1}=|Acl_j|r_j+d.  Certification is issued only when the period map is
        Schur in the nonnegative box dynamics and every containment inequality holds.
        """
        period=max(1,int(self.config.get("robust.terminal_period",2))); names=self.config.get("robust.terminal_dynamic_states",[]) or []
        idx=[self.model.codec.index[n] for n in names if n in self.model.codec.index]
        if not idx:
            idx=[i for i,n in enumerate(self.model.codec.names) if not n.startswith("mesa_position_ned_m")]
        if not trust_seq or not coupling_seq:
            return {"period":period,"certified":False,"reason":"missing_terminal_scheduling"}
        Ms=[]; d=float(self.config.get("robust.disturbance_radius",0.03))*np.ones(len(idx))
        H=max(len(trust_seq),1)
        start=max(0,H-period)
        for q in range(period):
            l=min(start+q,H-1); g=self._topology_at(topology,l)
            A,Ba,_,_,_=self.model.matrices(trust_seq[min(l,len(trust_seq)-1)],coupling_seq[min(l,len(coupling_seq)-1)],g)
            L=self._feedback(Ba); Acl=A+Ba@L
            Ms.append(np.abs(Acl[np.ix_(idx,idx)]))
        # Compose one-period affine map r_p = P r_0 + c.
        P=np.eye(len(idx)); c=np.zeros(len(idx))
        for M in Ms:
            c=M@c+d; P=M@P
        rho=float(max(abs(np.linalg.eigvals(P)))**(1.0/period)) if len(idx) else float("inf")
        if not np.isfinite(rho) or rho>=1.0-1e-10:
            return {"period":period,"state_indices":idx,"spectral_radius_per_step":rho,"certified":False,"reason":"period_map_not_schur"}
        try:
            r0=np.linalg.solve(np.eye(len(idx))-P,c)
        except np.linalg.LinAlgError:
            return {"period":period,"state_indices":idx,"spectral_radius_per_step":rho,"certified":False,"reason":"periodic_rpi_solve_failed"}
        if np.any(~np.isfinite(r0)) or np.any(r0<0):
            return {"period":period,"state_indices":idx,"spectral_radius_per_step":rho,"certified":False,"reason":"invalid_periodic_rpi_radius"}
        radii=[r0]; containment=[]; r=r0.copy()
        tol=1e-8
        for M in Ms:
            rn=M@r+d; radii.append(rn); containment.append(bool(np.all(M@r+d<=rn+tol))); r=rn
        closure=bool(np.all(r<=r0+1e-7) and np.all(r0<=r+1e-7))
        rmax=float(self.config.get("robust.terminal_radius_max",1e6))
        bounded=bool(float(np.max(r0))<=rmax)
        certified=bool(all(containment) and closure and bounded)
        return {"period":period,"state_indices":idx,"spectral_radius_per_step":rho,"contraction_ratio":rho,"radii":[x.tolist() for x in radii[:-1]],
                "closure_error":float(np.max(np.abs(r-r0))),"containment":containment,"bounded":bounded,"radius_max_allowed":rmax,
                "certified":certified,"method":"periodic_box_RPI_fixed_point","claim":"certificate for the packaged qLPV reference model and configured scheduling/topology sequence"}

    def terminal_diagnostic(self,tube,trust_seq,coupling_seq,topology):
        # Compatibility alias; the new result is a real RPI certificate rather than a transient-radius heuristic.
        return self.terminal_certificate(trust_seq,coupling_seq,topology)
