from __future__ import annotations
from dataclasses import asdict
import time, uuid
from .models import TDOCInput, CandidateUpdate, AttributionResult, RouteDecision, ValidationResult, CommitResult


class ProvenanceManager:
    """Build the versioned adaptation/governance record required by Layer 7.

    The record deliberately contains both the compact legacy keys and the
    formal Metrics_pre/Metrics_shadow/Metrics_post / deltaTheta fields so a
    TDOC audit can reconstruct why a candidate was proposed and committed.
    Post-commit metrics are populated asynchronously in the persistent
    ``post_commit_latest.json`` record and are referenced here by hash/version.
    """

    def build(self, inp: TDOCInput, candidate: CandidateUpdate, attr: AttributionResult,
              route: RouteDecision, validation: ValidationResult, commit: CommitResult) -> dict:
        candidate_hash = commit.candidate_hash or validation.candidate_hash
        new_version = commit.committed_version or commit.candidate_version
        active_metrics = {
            'loss': float(validation.active_loss),
            'uncertainty': float(inp.network.uncertainty),
            'margin': float(inp.network.margin),
            'constraint_violations': int(inp.decision.constraint_violations),
        }
        shadow_metrics = {
            'loss': float(validation.shadow_loss),
            'improvement': float(validation.improvement),
            'stability_ok': bool(validation.stability_ok),
            'coverage_ok': bool(validation.coverage_ok),
            'source': validation.shadow_source,
        }
        return {
            'updateID': str(uuid.uuid4()),
            'transaction_id': inp.transaction_id,
            'time': time.time(),
            'old_version': inp.network.version,
            'new_version': new_version,
            'candidate_version': commit.candidate_version,
            'candidate_hash': candidate_hash,
            'evidence_type': inp.execution.evidence_type.value,
            'route': {
                'L2': route.layer2, 'L3.1': route.layer31, 'L3.2': route.layer32,
                'L3.3': route.layer33, 'POLICY': route.policy, 'EXECUTION': route.execution,
            },
            'cause_posterior': attr.posterior,
            'attribution_confidence': attr.confidence,
            # Formal provenance payload: full bounded delta, not merely a text reason.
            'deltaTheta': asdict(candidate),
            'candidate_reasons': candidate.reasons,
            'Metrics_pre': active_metrics,
            'Metrics_shadow': shadow_metrics,
            'Metrics_post': None,
            'post_metrics_ref': {
                'state_record': 'post_commit_latest.json',
                'candidate_hash': candidate_hash,
                'version': commit.committed_version,
            } if commit.status == 'COMMITTED' else None,
            'validation': {
                'commit_gate': validation.commit_gate,
                'reasons': validation.reasons,
                'active_loss': validation.active_loss,
                'shadow_loss': validation.shadow_loss,
                'improvement': validation.improvement,
                'candidate_hash': validation.candidate_hash,
                'stability_source': validation.stability_source,
                'shadow_source': validation.shadow_source,
            },
            'approver': inp.hints.approver,
            'commit_status': commit.status,
            'rollback_ref': commit.rollback_version,
            'trigger_owner': 'TDOC',
            'direct_actuation': False,
        }
