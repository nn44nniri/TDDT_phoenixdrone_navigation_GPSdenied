from __future__ import annotations


class RobustScenarioAggregator:
    """Summarize the robust min-max/chance optimization already executed per candidate.

    The previous implementation performed a post-hoc family aggregation.  In the
    current implementation the shared action sequence is evaluated against an
    uncertainty pool *inside* the optimizer; this class only exposes that evidence.
    """
    def __init__(self,config): self.config=config

    def evaluate(self,candidates):
        rows=[]
        for c in candidates:
            opt=((c.provenance or {}).get("optimization") or {}).get("final_robust_evaluation") or {}
            rows.append({
                "candidate_id":c.candidate_id,
                "action_name":(c.provenance or {}).get("action_name"),
                "objective_profile":((c.provenance or {}).get("scenario_parameters") or {}).get("objective_profile"),
                "robust_feasible":bool(c.robust_feasible),
                "robust_score":float(c.robust_score if c.robust_score is not None else opt.get("robust_cost",1e30)),
                "expected_score":float(opt.get("expected_cost",c.scalar_cost if c.scalar_cost is not None else 1e30)),
                "chance_probability":float(c.violation_probability),
                "constraint_violation":float(opt.get("constraint_violation",0.0)),
                "case_costs":opt.get("case_costs",[]),
                "case_feasible":opt.get("case_feasible",[]),
                "worst_case_scenario_id":opt.get("worst_case_scenario_id"),
                "worst_case_disturbance":opt.get("worst_case_disturbance"),
            })
        feasible=[r for r in rows if r["robust_feasible"]]
        best=min(feasible,key=lambda r:(r["robust_score"],r["candidate_id"])) if feasible else None
        worst=max(rows,key=lambda r:(r["robust_score"],r["candidate_id"])) if rows else None
        return {"families":rows,"robust_best":best,"worst_case":worst,"method":"in-loop finite reduced-scenario min-max + chance-constrained optimization"}
