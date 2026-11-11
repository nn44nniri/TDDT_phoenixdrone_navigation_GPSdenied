from __future__ import annotations
from dataclasses import dataclass
import math
from .native_pair import check_pair
from .models import TDOCInput, EvidenceType
from .config import Layer7Config


@dataclass(slots=True)
class EvidenceGateResult:
    ok: bool
    version_ok: bool
    correlation_ok: bool
    freshness_ok: bool
    semantic_ok: bool
    reasons: list[str]
    native_pair: dict | None = None


class EvidenceGateway:
    """Version/correlation/freshness/semantic gates (L7-7/8), fail-closed."""
    def __init__(self, config: Layer7Config, native_diagnostic_validator=None):
        self.config = config
        # A process-local TDOC consumer may admit simulation diagnostics. A
        # packet flag cannot install this validator or grant persistent authority.
        self.native_diagnostic_validator = native_diagnostic_validator

    @staticmethod
    def _nonempty(x) -> bool:
        return isinstance(x, str) and bool(x.strip())

    def _correlation_ok(self, inp: TDOCInput) -> tuple[bool, str]:
        e = inp.execution
        pairs = [
            ("observation_id", e.observation_id, "input_observation_id", e.input_observation_id),
            ("decision_id", e.decision_id, "correlates_to_decision_id", e.correlates_to_decision_id),
            ("command_id", e.command_id, "correlates_to_actuator_command_id", e.correlates_to_actuator_command_id),
        ]
        # FIELD/HIL execution is expected to originate from the PT_LIB correlation
        # chain.  The normalized transaction must contain all three links.
        if self.config.edge.require_ptlib_correlation and e.evidence_type in (EvidenceType.FIELD, EvidenceType.HIL):
            for a, av, b, bv in pairs:
                if not self._nonempty(av) or not self._nonempty(bv):
                    return False, f"missing PT_LIB correlation link: {a}/{b}"
        for a, av, b, bv in pairs:
            if av is None and bv is None:
                continue
            if not self._nonempty(av) or not self._nonempty(bv):
                return False, f"incomplete PT_LIB correlation link: {a}/{b}"
            if av != bv:
                return False, f"PT_LIB correlation mismatch: {a}!={b}"
        return True, ""

    @staticmethod
    def _canonical_semantic(meta: dict) -> tuple:
        # Compare only interoperability-critical dimensions; additional metadata
        # is provenance, not a compatibility requirement.
        return (
            str(meta.get("quantity", meta.get("semantic", ""))).strip(),
            str(meta.get("unit", "")).strip(),
            str(meta.get("frame", "")).strip(),
        )

    def _semantic_ok(self, inp: TDOCInput) -> tuple[bool, str]:
        real, pred = inp.execution.y_real, inp.prediction.y_hat_td
        common = sorted(set(real) & set(pred))
        if not common:
            return False, "no comparable real/predicted output keys"
        es, ps = inp.execution.output_semantics, inp.prediction.output_semantics
        if self.config.edge.require_semantic_metadata:
            for key in common:
                if key not in es or key not in ps:
                    return False, f"missing TDOC semantic metadata for output: {key}"
                ce, cp = self._canonical_semantic(es[key]), self._canonical_semantic(ps[key])
                if not all(ce) or not all(cp):
                    return False, f"incomplete quantity/unit/frame metadata for output: {key}"
                if ce != cp:
                    return False, f"semantic/unit/frame mismatch for output: {key}"
        elif es or ps:
            for key in common:
                if key in es and key in ps and self._canonical_semantic(es[key]) != self._canonical_semantic(ps[key]):
                    return False, f"semantic/unit/frame mismatch for output: {key}"
        return True, ""

    def check(self, inp: TDOCInput) -> EvidenceGateResult:
        reasons: list[str] = []
        versions = [inp.execution.version, inp.prediction.version, inp.network.version]
        if inp.decision.version not in ("", "0", None):
            versions.append(inp.decision.version)
        version_ok = len(set(versions)) == 1
        if not version_ok:
            reasons.append(f"version mismatch: {versions}")

        correlation_ok, corr_reason = self._correlation_ok(inp)
        if not correlation_ok:
            reasons.append(corr_reason)

        ts = [inp.execution.timestamp, inp.prediction.timestamp, inp.network.timestamp]
        values = [float(x) for x in ts if x is not None]
        freshness_ok = (len(values)==3 and all(math.isfinite(t) for t in values) and
                        max(values) - min(values) <= self.config.edge.freshness_max_s)
        if not freshness_ok:
            reasons.append("freshness/time-boundary mismatch")

        semantic_ok, semantic_reason = self._semantic_ok(inp)
        if not semantic_ok:
            reasons.append(semantic_reason)
        # A non-finite residual/covariance cannot legitimately enter Bayes,
        # GP, drift, route, feedback or persistence (also for legacy packets).
        finite_ok = True
        for name, vals in (
            ('y_real',inp.execution.y_real),('y_hat_td',inp.prediction.y_hat_td),
            ('x_obs',inp.execution.x_obs),('x_hat_td',inp.prediction.x_hat_td),
            ('u_cmd',inp.execution.u_cmd),('u_applied',inp.execution.u_applied),
            ('sigma_pred',inp.prediction.sigma_pred),
        ):
            for key, value in vals.items():
                try:
                    good=not isinstance(value,bool) and math.isfinite(float(value))
                    if name=='sigma_pred':good=good and float(value)>0
                except (ValueError,TypeError,OverflowError):good=False
                if not good:
                    reasons.append(f'invalid_numeric:{name}.{key}')
                    finite_ok=False
        for name, value in (('coc_cost',inp.network.coc_cost),('network_margin',inp.network.margin),
                            ('network_uncertainty',inp.network.uncertainty)):
            if not isinstance(value,(int,float)) or isinstance(value,bool) or not math.isfinite(float(value)):
                finite_ok=False;reasons.append('invalid_numeric:'+name)

        # Native physical-controller residuals need provenance-bound pairing.
        # Even source-labelled matching values are *diagnostic only*: this
        # library has no independent native controller replay/certification.
        diagnostic = False
        if callable(self.native_diagnostic_validator) and inp.execution.evidence_type == EvidenceType.SIMULATION:
            try: diagnostic = self.native_diagnostic_validator(inp) is True
            except Exception: diagnostic = False
        pair=check_pair(inp, model_diagnostic=diagnostic)
        if pair.required:
            if not pair.comparable:
                reasons.extend('native_pair:'+r for r in pair.reasons)
            elif not diagnostic:
                reasons.append('native_pair_matched_diagnostic_only_not_certified')
        return EvidenceGateResult(
            ok=version_ok and correlation_ok and freshness_ok and semantic_ok and finite_ok
               and (not pair.required or (diagnostic and pair.comparable)),
            version_ok=version_ok,
            correlation_ok=correlation_ok,
            freshness_ok=freshness_ok,
            semantic_ok=semantic_ok,
            reasons=reasons,
            native_pair=pair.to_dict(),
        )

    @staticmethod
    def persistent_field_allowed(inp: TDOCInput) -> bool:
        return inp.execution.evidence_type == EvidenceType.FIELD
