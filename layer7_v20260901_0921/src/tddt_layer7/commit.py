from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional
import os, json, time
from .models import CandidateUpdate, CommitResult, TDOCInput, ValidationResult
from .utils import atomic_write_json, sha256_json, dataclass_hash


class CommitManager:
    """TDOC-managed two-phase commit + rollback handshake with persistent recovery metadata."""
    def __init__(self, root: str | Path | None = None,
                 callback: Optional[Callable[[dict], str | dict]] = None,
                 rollback_callback: Optional[Callable[[dict], str | dict]] = None):
        env = os.getenv('TDDT_LAYER7_STATE_ROOT')
        self.root = Path(root or env or Path.cwd()/'.layer7_state')
        self.root.mkdir(parents=True, exist_ok=True); (self.root/'versions').mkdir(exist_ok=True)
        self.callback = callback; self.rollback_callback = rollback_callback
        self.last_safe_version: Optional[str] = None
        self._load_state()

    def _load_state(self):
        p = self.root/'last_safe.json'
        if p.exists():
            try: self.last_safe_version = str(json.loads(p.read_text()).get('version'))
            except Exception: self.last_safe_version = None

    @staticmethod
    def _ack_version(ack, default: str) -> str:
        if isinstance(ack, dict):
            if ack.get('accepted') is False: raise RuntimeError(str(ack.get('reason', 'TDOC rejected operation')))
            return str(ack.get('version', ack.get('committed_version', default)))
        return str(ack if ack is not None else default)

    def prepare(self, inp: TDOCInput, candidate: CandidateUpdate, validation: ValidationResult) -> CommitResult:
        current = inp.network.version
        try: cand = str(int(current) + 1)
        except Exception: cand = f"{current}+1"
        chash = dataclass_hash(candidate)
        if candidate.is_empty():
            return CommitResult('NO_CHANGE', current, cand, reason='empty candidate', candidate_hash=chash)
        if candidate.persistent and not validation.commit_gate:
            return CommitResult('REJECTED', current, cand, reason='safe-commit gate failed', candidate_hash=chash)
        if not candidate.persistent:
            return CommitResult('TRANSIENT_ONLY', current, cand, reason='no persistent commit required', candidate_hash=chash)
        payload = {'operation': 'commit', 'transaction_id': inp.transaction_id, 'base_version': current,
                   'candidate_version': cand, 'candidate_hash': chash, 'candidate': asdict(candidate),
                   'validation': asdict(validation), 'prepared_at': time.time()}
        payload['hash'] = sha256_json(payload)
        path = self.root/'versions'/f'candidate_{cand}.json'; atomic_write_json(path, payload)
        atomic_write_json(self.root/'prepared.json', {'path': str(path), 'hash': payload['hash'], 'version': cand,
                                                       'candidate_hash': chash})
        if self.callback is None:
            return CommitResult('STAGED_NOT_COMMITTED', current, cand, reason='TDOC commit callback not configured', candidate_hash=chash)
        try:
            committed = self._ack_version(self.callback(payload), cand)
        except Exception as exc:
            return CommitResult('TDOC_COMMIT_REJECTED', current, cand, reason=str(exc), candidate_hash=chash)
        self.last_safe_version = current
        atomic_write_json(self.root/'last_safe.json', {'version': current, 'saved_at': time.time()})
        atomic_write_json(self.root/'active_pointer.json', {'version': committed, 'candidate_hash': chash, 'commit_hash': payload['hash']})
        # Atomic next-fast snapshot required by the Layer-7 commit formalism.
        atomic_write_json(self.root/'active_snapshot.json', {
            'version': committed, 'STD': inp.network.std,
            'G_base': inp.network.topology, 'W_base': inp.network.weights, 'K_base': inp.network.couplings,
            'CoC_cost': inp.network.coc_cost, 'candidate': asdict(candidate),
            'guidance': inp.network.guidance, 'candidate_hash': chash, 'commit_hash': payload['hash']
        })
        return CommitResult('COMMITTED', current, cand, committed_version=committed, candidate_hash=chash)

    def rollback(self, current_version: str, reason: str = 'post-commit degradation') -> CommitResult:
        target = self.last_safe_version
        if target is None:
            return CommitResult('ROLLBACK_UNAVAILABLE', current_version, current_version, reason='no last-safe version')
        payload = {'operation': 'rollback', 'current_version': current_version, 'target_version': target,
                   'reason': reason, 'requested_at': time.time()}
        payload['hash'] = sha256_json(payload)
        atomic_write_json(self.root/'rollback_prepared.json', payload)
        cb = self.rollback_callback or self.callback
        if cb is None:
            return CommitResult('ROLLBACK_STAGED_NOT_APPLIED', current_version, current_version,
                                rollback_version=target, reason='TDOC rollback callback not configured')
        try:
            restored = self._ack_version(cb(payload), target)
        except Exception as exc:
            return CommitResult('TDOC_ROLLBACK_REJECTED', current_version, current_version,
                                rollback_version=target, reason=str(exc))
        atomic_write_json(self.root/'active_pointer.json', {'version': restored, 'rollback_hash': payload['hash']})
        atomic_write_json(self.root/'rollback_applied.json', {**payload, 'restored_version': restored})
        self.clear_post_commit_watch()
        return CommitResult('ROLLED_BACK', current_version, current_version,
                            committed_version=restored, rollback_version=target, reason=reason)

    def begin_post_commit_watch(self, version: str, candidate_hash: str, baseline_loss: float,
                                baseline_uncertainty: float, baseline_margin: float, ticks: int) -> None:
        atomic_write_json(self.root/'post_commit_watch.json', {
            'version': str(version), 'candidate_hash': candidate_hash, 'baseline_loss': float(baseline_loss),
            'baseline_uncertainty': float(baseline_uncertainty), 'baseline_margin': float(baseline_margin),
            'remaining_ticks': int(ticks), 'started_at': time.time(),
        })

    def post_commit_watch(self) -> dict | None:
        p = self.root/'post_commit_watch.json'
        if not p.exists(): return None
        try: return json.loads(p.read_text())
        except Exception: return None

    def update_post_commit_watch(self, watch: dict) -> None:
        atomic_write_json(self.root/'post_commit_watch.json', watch)

    def record_post_commit_metrics(self, watch: dict, metrics: dict, status: str = 'MONITORING') -> None:
        # One bounded, atomically replaced record: post-commit governance evidence
        # without unbounded per-tick files on the Edge device.
        atomic_write_json(self.root/'post_commit_latest.json', {
            'version': str(watch.get('version', '')),
            'candidate_hash': str(watch.get('candidate_hash', '')),
            'status': status, 'metrics': dict(metrics), 'recorded_at': time.time(),
        })

    def clear_post_commit_watch(self) -> None:
        (self.root/'post_commit_watch.json').unlink(missing_ok=True)

    @property
    def rollback_capable(self) -> bool:
        # The implementation always stages a deterministic rollback request and
        # can apply it atomically when TDOC provides a callback.
        return True
