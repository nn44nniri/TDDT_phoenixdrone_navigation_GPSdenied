from __future__ import annotations

class ParetoEngine:
    def __init__(self,config): self.config=config
    def normalize(self,obj):
        out={}; eps=1e-12
        for k,v in obj.items():
            lo,hi=self.config.get(f"objectives.normalization.{k}",[0.0,1.0]); out[k]=(float(v)-float(lo))/(float(hi)-float(lo)+eps)
        return out
    @staticmethod
    def dominates(a,b,keys):
        av=[a[k] for k in keys]; bv=[b[k] for k in keys]
        return all(x<=y+1e-12 for x,y in zip(av,bv)) and any(x<y-1e-12 for x,y in zip(av,bv))
    def rank(self,candidates):
        eligible=[c for c in candidates if c.feasible and c.violation_probability<=float(self.config.get("feasibility.alpha_risk",0.1))]
        for c in candidates: c.normalized_objectives=self.normalize(c.objective_vector)
        keys=list(self.config.get("objectives.normalization",{}).keys())
        remaining=eligible[:]; rank=0; fronts=[]
        while remaining:
            front=[]
            for c in remaining:
                if not any(self.dominates(o.normalized_objectives,c.normalized_objectives,keys) for o in remaining if o is not c): front.append(c)
            for c in front: c.pareto_rank=rank
            fronts.append(front); remaining=[c for c in remaining if c not in front]; rank+=1
        return fronts
