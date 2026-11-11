from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any, Callable, Mapping, Optional
from .config import Layer7Config
from .models import (
    AdaptationHints, DecisionTrace, EvidenceType, ExecutionEvidence,
    NetworkSnapshot, PredictionEvidence, OfflineErrorPacket, TDOCInput,
)
from .pipeline import Layer7Pipeline


@dataclass(frozen=True, slots=True)
class TDOCModuleManifest:
    module_name: str = 'tddt_layer7'
    module_version: str = '0.3.0'
    contract_version: str = 'tdoc-layer7-v2'
    trigger_owner: str = 'TDOC'
    direct_actuation: bool = False
    side_module: bool = True
    inputs: tuple[str, ...] = (
        'ExecutionEvidence','PredictionEvidence','NetworkSnapshot','DecisionTrace','AdaptationHints','v'
    )
    outputs: tuple[str, ...] = (
        'DeltaX','DeltaB','DeltaTheta','DeltaU','DeltaQ','DeltaV','DeltaR','TEP_candidate',
        'DeltaW','DeltaK','Gcand','DeltaEpsilon','DeltaOmega','DeltaLambda_C','DeltaCyclePolicy',
        'DeltaGamma','CorrectedPrediction','CorrectedUncertainty','StateResidualEvidence','vcand'
    )
    validation_handshake: str = 'candidate_hash+base_version -> shadow/stability evidence'
    rollback_handshake: str = 'rollback target version -> TDOC acknowledgement'


class Layer7SideModule:
    """TDOC-managed Layer 7 side-module.

    TDOC owns invocation, external shadow/stability execution, commit and final
    rollback application.  Layer 7 constructs/validates candidate updates and
    performs only the side of those handshakes defined by its contract.
    """
    manifest = TDOCModuleManifest()

    def __init__(self, config: Layer7Config | None = None,
                 tdoc_commit_callback: Optional[Callable[[dict], str | dict]] = None,
                 tdoc_validation_callback: Optional[Callable[[dict], Mapping[str, Any]]] = None,
                 tdoc_rollback_callback: Optional[Callable[[dict], str | dict]] = None,
                 state_root=None, native_diagnostic_validator=None):
        self.pipeline = Layer7Pipeline(config=config, tdoc_commit_callback=tdoc_commit_callback,
                                       tdoc_validation_callback=tdoc_validation_callback,
                                       tdoc_rollback_callback=tdoc_rollback_callback,
                                       state_root=state_root,
                                       native_diagnostic_validator=native_diagnostic_validator)

    @staticmethod
    def _num(d): return {str(k): float(v) for k, v in (d or {}).items()}

    @staticmethod
    def _sem(d):
        return {str(k): dict(v or {}) for k, v in (d or {}).items()}

    @staticmethod
    def _evidence_type(value: Any) -> EvidenceType:
        try:
            return EvidenceType(str(value))
        except (ValueError, TypeError) as exc:
            # Never reinterpret unknown evidence as FIELD.
            allowed = ','.join(x.value for x in EvidenceType)
            raise ValueError(f'invalid evidence_type={value!r}; allowed: {allowed}') from exc

    @classmethod
    def input_from_dict(cls, d: Mapping[str, Any]) -> TDOCInput:
        ex = d.get('ExecutionEvidence') or d.get('execution') or {}
        pr = d.get('PredictionEvidence') or d.get('prediction') or {}
        nw = d.get('NetworkSnapshot') or d.get('network') or {}
        de = d.get('DecisionTrace') or d.get('decision') or {}
        hi = d.get('AdaptationHints') or d.get('hints') or {}
        et = cls._evidence_type(ex.get('evidence_type', 'field'))
        execution = ExecutionEvidence(
            y_real=cls._num(ex.get('y_real')), u_cmd=cls._num(ex.get('u_cmd')), u_applied=cls._num(ex.get('u_applied')),
            x_obs=cls._num(ex.get('x_obs')), d_real=cls._num(ex.get('d_real')),
            actuator_status=dict(ex.get('actuator_status') or {}), timestamp=float(ex.get('timestamp', 0.0)),
            planned_timestamp=ex.get('planned_timestamp'), provenance=dict(ex.get('provenance') or {}),
            version=str(ex.get('version', d.get('v', '0'))), evidence_type=et,
            observation_id=ex.get('observation_id'), decision_id=ex.get('decision_id'), command_id=ex.get('command_id'),
            input_observation_id=ex.get('input_observation_id'),
            correlates_to_decision_id=ex.get('correlates_to_decision_id'),
            correlates_to_actuator_command_id=ex.get('correlates_to_actuator_command_id'),
            actuator_feedback_id=ex.get('actuator_feedback_id'), output_semantics=cls._sem(ex.get('output_semantics')),
        )
        prediction = PredictionEvidence(
            y_hat_td=cls._num(pr.get('y_hat_td')), x_hat_td=cls._num(pr.get('x_hat_td')),
            xi_hat=cls._num(pr.get('xi_hat')), sigma_pred=cls._num(pr.get('sigma_pred')),
            jc_pred=float(pr.get('jc_pred', 0.0)), margin_pred=float(pr.get('margin_pred', 0.0)),
            timestamp=float(pr.get('timestamp', 0.0)), provenance=dict(pr.get('provenance') or {}),
            version=str(pr.get('version', d.get('v', '0'))), output_semantics=cls._sem(pr.get('output_semantics')),
        )
        topology = [tuple(x) for x in nw.get('topology', [])]
        network = NetworkSnapshot(
            std=cls._num(nw.get('std')), weights=cls._num(nw.get('weights')),
            couplings=cls._num(nw.get('couplings')), topology=topology,
            coc_cost=float(nw.get('coc_cost', 0.0)), margin=float(nw.get('margin', 0.0)),
            uncertainty=float(nw.get('uncertainty', 0.0)), guidance=dict(nw.get('guidance') or {}),
            constraints=dict(nw.get('constraints') or {}), epsilon=cls._num(nw.get('epsilon')),
            omega=cls._num(nw.get('omega')), hidden_loop_lci=cls._num(nw.get('hidden_loop_lci')),
            twin_validity=cls._num(nw.get('twin_validity')), twin_quality=cls._num(nw.get('twin_quality')),
            twin_freshness=cls._num(nw.get('twin_freshness')), twin_reliability=cls._num(nw.get('twin_reliability')),
            timestamp=float(nw.get('timestamp', 0.0)), version=str(nw.get('version', d.get('v', '0'))),
            content_hash=str(nw.get('content_hash', '')), boundary_safe=bool(nw.get('boundary_safe', True)),
            metadata=dict(nw.get('metadata') or {}),
        )
        decision = DecisionTrace(real_cost=de.get('real_cost'), feasible_reference_cost=de.get('feasible_reference_cost'),
                                 constraint_violations=int(de.get('constraint_violations', 0)),
                                 decision_confidence=de.get('decision_confidence'), timestamp=float(de.get('timestamp', 0.0)),
                                 version=str(de.get('version', d.get('v', '0'))), provenance=dict(de.get('provenance') or {}))
        allowed = set(AdaptationHints.__dataclass_fields__); unknown = sorted(set(hi) - allowed)
        if unknown:
            raise ValueError(f'unknown AdaptationHints fields: {unknown}')
        hints = AdaptationHints(**dict(hi))
        return TDOCInput(execution=execution, prediction=prediction, network=network, decision=decision, hints=hints,
                         transaction_id=str(d.get('transaction_id', '')))

    def process(self, payload: Mapping[str, Any]) -> dict:
        return self.pipeline.process(self.input_from_dict(payload)).to_dict()

    def process_offline(self, payload: Mapping[str, Any]) -> dict:
        et = self._evidence_type(payload.get('evidence_type', 'simulation'))
        packet = OfflineErrorPacket(delta_sarg=float(payload.get('delta_sarg', 0.0)), coc_cost=float(payload.get('coc_cost', 0.0)),
                                    constraint_violations=int(payload.get('constraint_violations', 0)),
                                    simulation_residual=float(payload.get('simulation_residual', 0.0)), evidence_type=et,
                                    version=str(payload.get('version', '0')), context=dict(payload.get('context') or {}),
                                    provenance=dict(payload.get('provenance') or {}))
        return self.pipeline.process_offline(packet)

    @classmethod
    def registration_descriptor(cls) -> dict:
        return asdict(cls.manifest)
