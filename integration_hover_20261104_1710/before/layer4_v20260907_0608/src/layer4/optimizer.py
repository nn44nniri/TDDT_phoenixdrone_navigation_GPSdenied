from __future__ import annotations
import numpy as np


class Layer4MultiObjectiveOptimizer:
    """Bounded deterministic SPSA optimizer for the complete Layer-4 augmented objective.

    The solver is intentionally dependency-light and edge-bounded.  The caller supplies
    a projection operator that enforces the intersection of tube-tightened input sets,
    and an evaluator that returns the seven-objective scalarization together with
    robust/chance/terminal constraint penalties.  This keeps the optimization loop
    aligned with E4-39..E4-42, E4-58..E4-72 without moving actuation/commit into Layer 4.
    """

    def __init__(self, config):
        self.config = config

    @staticmethod
    def _direction(shape, iteration: int) -> np.ndarray:
        # Local, reproducible Rademacher draws. The old LCG low-bit expression
        # produced only d and -d (rank one), so all other action directions were
        # invisible to the gradient estimate. No global RNG state is consumed.
        if int(iteration) < 0:
            raise ValueError("iteration must be non-negative")
        rng = np.random.Generator(np.random.PCG64(np.random.SeedSequence([0x54444454, int(iteration)])))
        return 2.0 * rng.integers(0, 2, size=shape, dtype=np.int8) - 1.0

    @staticmethod
    def _rank_eval(e):
        # Hard feasibility/chance is prioritized before cost when choosing among probes.
        return (
            float(e.get("chance_excess", 0.0) > 1e-12),
            float(e.get("constraint_violation", 0.0) > 1e-12),
            float(e.get("augmented_cost", np.inf)),
        )

    def optimize(self, U0, project, evaluate, initial_evaluation=None):
        U = project(np.asarray(U0, dtype=float))
        # The engine ranks deterministic seeds with this exact robust evaluator
        # immediately before entering SPSA.  Reuse that immutable result instead
        # of evaluating the selected seed twice; solver iterations, probes,
        # constraints and acceptance ordering remain unchanged.
        cur = initial_evaluation if initial_evaluation is not None else evaluate(U)
        history = [{"iteration": 0, **{k: cur.get(k) for k in ("augmented_cost", "robust_cost", "expected_cost", "chance_probability", "chance_excess", "constraint_violation")}}]

        iters = max(1, int(self.config.get("optimization.iterations", 6)))
        a0 = float(self.config.get("optimization.step", 0.12))
        c0 = float(self.config.get("optimization.perturbation", 0.035))
        alpha = float(self.config.get("optimization.step_decay", 0.602))
        gamma = float(self.config.get("optimization.perturbation_decay", 0.101))
        gclip = max(1e-12, float(self.config.get("optimization.gradient_clip", 3.0)))
        last_grad = np.zeros_like(U)

        for it in range(iters):
            ak = a0 / ((it + 1.0) ** alpha)
            ck = c0 / ((it + 1.0) ** gamma)
            delta = self._direction(U.shape, it)
            Up = project(U + ck * delta)
            Um = project(U - ck * delta)
            ep = evaluate(Up)
            em = evaluate(Um)
            scale = (float(ep["augmented_cost"]) - float(em["augmented_cost"])) / max(2.0 * ck, 1e-12)
            grad = scale * delta
            rms = float(np.sqrt(np.mean(grad * grad)))
            if rms > gclip:
                grad *= gclip / rms
            last_grad = grad
            Ut = project(U - ak * grad)
            et = evaluate(Ut)

            probes = [(U, cur, "current"), (Up, ep, "plus"), (Um, em, "minus"), (Ut, et, "gradient")]
            U, cur, accepted = min(probes, key=lambda z: self._rank_eval(z[1]))
            history.append({
                "iteration": it + 1,
                "accepted_probe": accepted,
                "gradient_rms": rms,
                **{k: cur.get(k) for k in ("augmented_cost", "robust_cost", "expected_cost", "chance_probability", "chance_excess", "constraint_violation")},
            })

        # Projected stationarity residual: zero only when another bounded step is clipped/no-op.
        probe = project(U - min(a0, 0.05) * last_grad)
        stationarity = float(np.linalg.norm(U - probe))
        meta = {
            "solver": "bounded_deterministic_spsa_augmented_lagrangian",
            "iterations": iters,
            "history": history,
            "projected_stationarity_residual": stationarity,
            "bounded_compute": True,
            "objective_in_loop": True,
            "chance_constraint_in_loop": True,
            "robust_minmax_in_loop": True,
            "tube_input_constraints_in_loop": True,
            "terminal_constraint_in_loop": True,
        }
        return U, cur, meta
