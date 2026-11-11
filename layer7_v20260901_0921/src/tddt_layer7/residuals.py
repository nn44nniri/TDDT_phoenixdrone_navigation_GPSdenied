from __future__ import annotations
import math
from .models import TDOCInput, ResidualBundle
from .utils import diff_maps, l2_map
from .graph import bounded_cycle_lci


class ResidualEngine:
    """Structured nine-way residual decomposition (L7-11..13, L7-124..137)."""
    _ANGULAR_STATE_KEYS = frozenset({"roll_rad", "pitch_rad", "yaw_rad", "air_heading_rad"})

    def __init__(self, max_cycle_candidates: int = 8):
        self.max_cycle_candidates = max_cycle_candidates

    @staticmethod
    def _explicit_or_diff(explicit, a, b, name: str):
        if explicit:
            return dict(explicit), "explicit"
        if a and b:
            return diff_maps(a, b), "computed"
        return {}, "unavailable"

    def compute(self, inp: TDOCInput) -> ResidualBundle:
        out = diff_maps(inp.execution.y_real, inp.prediction.y_hat_td)
        state = diff_maps(inp.execution.x_obs, inp.prediction.x_hat_td)
        # Attitude and heading live on S1, not on the real line.  Values on
        # opposite sides of +/-pi are physically adjacent; preserving the raw
        # 2*pi jump would falsely dominate attribution and bounded adaptation.
        # Angular rates deliberately remain ordinary linear residuals.
        for key in self._ANGULAR_STATE_KEYS.intersection(state):
            value = float(state[key])
            state[key] = (value + math.pi) % (2.0 * math.pi) - math.pi
        exec_r = diff_maps(inp.execution.u_applied, inp.execution.u_cmd)
        h = inp.hints

        obs_map, obs_src = self._explicit_or_diff(h.observation_error, h.observation_measurements, h.observation_reference, "obs")
        model_map, model_src = self._explicit_or_diff(h.model_error, h.model_reference, h.model_prediction, "model")
        align_map, align_src = self._explicit_or_diff(h.alignment_error, h.alignment_actual, h.alignment_expected, "align")
        cpl_map, cpl_src = self._explicit_or_diff(h.coupling_error, h.coupling_observed, h.coupling_predicted, "cpl")
        if h.synchronization_error:
            sync_map, sync_src = dict(h.synchronization_error), "explicit"
        elif h.synchronization_source_times and h.synchronization_canonical_time is not None:
            sync_map = {k: float(v) - float(h.synchronization_canonical_time) for k, v in h.synchronization_source_times.items()}
            sync_src = "computed"
        else:
            sync_map, sync_src = {}, "unavailable"

        obs = l2_map(obs_map); model = l2_map(model_map); st = l2_map(state)
        align = l2_map(align_map); cpl = l2_map(cpl_map); sync = l2_map(sync_map)
        pred = l2_map(out)
        dec = 0.0
        if inp.decision.real_cost is not None and inp.decision.feasible_reference_cost is not None:
            dec = float(inp.decision.real_cost - inp.decision.feasible_reference_cost)
        ex = l2_map(exec_r)
        lat = float(inp.execution.timestamp - inp.execution.planned_timestamp) if inp.execution.planned_timestamp is not None else 0.0
        coc = abs(float(inp.network.coc_cost))
        bounded_lci = bounded_cycle_lci(inp.network.topology, inp.network.hidden_loop_lci, self.max_cycle_candidates)
        lci = max([abs(float(v)) for v in bounded_lci.values()] or [0.0])
        if h.emergent_error:
            emerg = abs(float(h.emergent_error)); emerg_src = "explicit"
        elif h.emergent_observed and h.emergent_baseline:
            emerg = l2_map(diff_maps(h.emergent_observed, h.emergent_baseline)); emerg_src = "computed"
        else:
            emerg = 0.0; emerg_src = "unavailable"
        domain = obs + model + st
        network = align + cpl + sync + coc + emerg

        norm2 = 0.0
        for k, e in out.items():
            var = max(float(inp.prediction.sigma_pred.get(k, 1.0)), 1e-12)
            norm2 += (float(e) ** 2) / var
        normalized = math.sqrt(norm2)
        vector = {"obs": obs, "model": model, "state": st, "align": align,
                  "cpl": cpl, "sync": sync, "pred": pred, "dec": abs(dec), "exec": ex}
        sources = {"obs": obs_src, "model": model_src, "state": "computed",
                   "align": align_src, "cpl": cpl_src, "sync": sync_src,
                   "pred": "computed", "dec": "computed" if inp.decision.real_cost is not None else "unavailable",
                   "exec": "computed", "emergent": emerg_src}
        return ResidualBundle(
            vector=vector, output_residual=out, state_residual=state, execution_residual=exec_r,
            observation_error_map=obs_map, model_error_map=model_map, alignment_error_map=align_map,
            coupling_error_map=cpl_map, synchronization_error_map=sync_map, residual_sources=sources,
            observation=obs, model=model, state=st, alignment=align, coupling=cpl,
            synchronization=sync, prediction=pred, decision=dec, execution=ex, latency=lat,
            domain=domain, network=network, emergent=emerg, coc=coc, lci=lci,
            normalized=normalized, selected_cycles=list(bounded_lci),
        )
