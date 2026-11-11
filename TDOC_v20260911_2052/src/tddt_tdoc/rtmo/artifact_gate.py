from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable


def _vec3(value: Any) -> tuple[float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None
    try:
        out = tuple(float(value[i]) for i in range(3))
    except Exception:
        return None
    return out if all(math.isfinite(v) for v in out) else None


def _distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def _snapshot_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    reps = (((snapshot or {}).get("std") or {}).get("representations") or {})
    mesa = (reps.get("MESA_NAV") or {}).get("canonical_state") or {}
    pos = _vec3(mesa.get("position_ned_m"))
    vel = _vec3(mesa.get("ground_velocity_ned_m_s"))
    return {"position_ned_m": pos, "ground_velocity_ned_m_s": vel}


def _verified_corridor(layer4_result: dict[str, Any]) -> list[dict[str, tuple[float, float, float]]]:
    points: list[dict[str, tuple[float, float, float]]] = []
    candidates = layer4_result.get("verified_candidates") or []
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        traj = cand.get("predicted_trajectory") or cand.get("trajectory") or []
        for row in traj:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            pos = _vec3(row[0:3]); vel = _vec3(row[3:6])
            if pos is not None and vel is not None:
                points.append({"position_ned_m": pos, "ground_velocity_ned_m_s": vel})
    return points


def _version_key(request: dict[str, Any], orchestration_version: str) -> tuple[str, str, str]:
    ctx = dict((request or {}).get("context") or {})
    handoff = dict(ctx.get("offline_knowledge_handoff") or {})
    climate = dict(ctx.get("online_climate_time_alignment") or ctx.get("offline_climate_time_alignment") or {})
    return (
        str(orchestration_version or ""),
        str(handoff.get("knowledge_version") or handoff.get("setd_package_id") or ""),
        str(climate.get("forcing_fingerprint") or climate.get("effective_climate_epoch") or ""),
    )


@dataclass
class ArtifactDecision:
    ok: bool
    reason: str
    result: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    prediction_snapshot: dict[str, Any] | None = None


class RTMOL4ArtifactGate:
    """Fast-path gate for verified Layer-4 artifacts.

    Heavy Layer-4 computation is scheduled on one daemon refresh lane.  The fast
    transaction never waits for that computation.  It may consume only the last
    complete, verified artifact whose phase/version/freshness and predicted-state
    corridor still cover the current Layer-3 snapshot.  Otherwise it fails closed.

    This component intentionally does *not* implement the certificate-bank policy
    addressed by later problem items; it only removes synchronous full Layer-4 work
    from the >=10 Hz control transaction.
    """

    def __init__(self, *, max_age_s: float = 30.0, position_tolerance_m: float = 20.0,
                 velocity_tolerance_m_s: float = 4.0,model_dependencies_provider=None):
        self.model_dependencies_provider=model_dependencies_provider
        self.max_age_s = max(float(max_age_s), 0.0)
        self.position_tolerance_m = max(float(position_tolerance_m), 0.0)
        self.velocity_tolerance_m_s = max(float(velocity_tolerance_m_s), 0.0)
        self._lock = threading.RLock()
        self._artifact: dict[str, Any] | None = None
        self._refresh_thread: threading.Thread | None = None
        self._refresh_status: dict[str, Any] = {"state": "IDLE"}
        self._closed = False
        self._generation = 0
        # Each fast transaction may commit only its freshly produced first move.
        # The verified artifact is a reusable policy/envelope, not that move and not
        # an open-loop command schedule.  Therefore execution consumes a transaction
        # grant, not the whole artifact (RTMO-33..36).  Duplicate acknowledgements for
        # the same transaction remain fail-closed.
        self._executed_transaction_ids: set[str] = set()
        self._consumption_record: dict[str, Any] | None = None

    @staticmethod
    def _artifact_key(metadata: dict[str, Any]) -> tuple[Any, ...]:
        return (
            str(metadata.get("phase") or ""),
            float(metadata.get("created_reference_time_s", float("nan"))),
            tuple(metadata.get("version_key") or ()),
            str(metadata.get("source_snapshot_policy_version") or ""),
        )

    def _model_compatible(self,result):
        if self.model_dependencies_provider is None:return True
        try:
            from ..model_compatibility import certificate_compatibility
            current=self.model_dependencies_provider()
            ids={str(c.get('candidate_id')) for c in (result.get('verified_candidates') or [])}
            rows={str(c.get('candidate_id')):c for c in (result.get('candidates') or [])}
            return bool(ids and all(k in rows and certificate_compatibility(
                rows[k].get('verification') or {},current)['ok'] for k in ids))
        except Exception:return False

    def _is_verified(self,result: dict[str, Any]) -> bool:
        verified = list((result or {}).get("verified_candidates") or [])
        conf = dict((result or {}).get("conformance") or {})
        ff_clean = conf.get("C_FF_runtime_clean") is not False
        return bool(verified and ff_clean and self._model_compatible(result))

    @staticmethod
    def _execution_projection(result: dict[str, Any]) -> dict[str, Any]:
        """Keep only Layer-5 execution inputs in the reusable fast artifact.

        Full WingMPET rows remain in the authoritative pre-release cycle/audit
        record.  Copying those rows again inside every fast transaction has no
        safety value: Layer5's contract consumes the candidate, the explicit FF
        authority fields and the durable certificate identity.  The projection
        is therefore lossless for admission while bounded for edge execution.
        """
        out=copy.deepcopy(dict(result or {}))
        heavy_verification_keys={
            "steps","commit_segment_steps","bridge","forecast_horizon_bridge",
            "wingmpet_physical_verdict","full_feasibility","prior_verification",
        }
        compact_candidates=[]
        for candidate in list(out.get("candidates") or []):
            if not isinstance(candidate,dict):
                continue
            item=dict(candidate)
            verification=dict(item.get("verification") or {})
            first=(verification.get("commit_segment_steps") or verification.get("steps") or [])[:1]
            if first and isinstance(first[0],dict):
                replay=first[0].get("physical_replay") or {}
                outputs=replay.get("outputs") or {}
                keys=("next_vn","next_ve","next_vd","next_qw","next_qx","next_qy","next_qz",
                      "next_p_rate","next_q_rate","next_r_rate","airspeed")
                verification["first_move_prediction"]={"outputs":{k:outputs[k] for k in keys if k in outputs},
                    "timing_contract":copy.deepcopy(replay.get("timing_contract") or verification.get("timing_contract") or {}),
                    "source":"Layer4/WingMPET/commit_segment", "source_snapshot_hash":out.get("snapshot_hash")}
            for key in heavy_verification_keys:
                verification.pop(key,None)
            transition=dict(verification.get("transition_ff_evidence") or {})
            if transition:
                # Transition reuse needs this certificate chain, but never its
                # embedded physical replay rows on the actuation path.
                transition.pop("rows",None)
                transition.pop("commit_segment_steps",None)
                verification["transition_ff_evidence"]=transition
            item["verification"]=verification
            compact_candidates.append(item)
        out["candidates"]=compact_candidates
        out["verified_candidates"]=[
            {"candidate_id":str(candidate.get("candidate_id") or "")}
            for candidate in list(out.get("verified_candidates") or [])
            if isinstance(candidate,dict) and candidate.get("candidate_id")
        ]
        if isinstance(out.get("pareto_front"),list):
            out["pareto_front"]=[
                {"candidate_id":str(candidate.get("candidate_id") or "")}
                for candidate in out["pareto_front"]
                if isinstance(candidate,dict) and candidate.get("candidate_id")
            ]
        provenance=dict(out.get("provenance") or {})
        ff=dict(provenance.pop("full_fidelity_verification",{}) or {})
        if ff:
            provenance["full_fidelity_verification_summary"]={
                key:copy.deepcopy(ff.get(key)) for key in (
                    "owner","internal_reverse_invocation","phase_contract",
                    "runtime_error_count","outcome_counts","selected_candidate_ids",
                    "audit_complete","elapsed_s",
                ) if key in ff
            }
        out["provenance"]=provenance
        encoded=json.dumps(out,sort_keys=True,separators=(",",":"),default=str,allow_nan=False).encode("utf-8")
        provenance["rtmo_execution_projection"]={
            "schema":"TDOC-RTMO-L4-execution-projection-v1",
            "sha256":hashlib.sha256(encoded).hexdigest(),
            "full_replay_evidence_location":"authoritative pre-release cycle/audit record",
            "full_replay_rows_in_fast_path":False,
        }
        return out

    def _artifact_metadata(self, result: dict[str, Any], snapshot: dict[str, Any], request: dict[str, Any],
                           orchestration_version: str, reference_time_s: float) -> dict[str, Any]:
        return {
            "schema": "TDOC-RTMO-L4-validated-artifact-v1",
            "phase": str((request or {}).get("stage") or ""),
            "created_reference_time_s": float(reference_time_s),
            "valid_until_reference_time_s": float(reference_time_s) + self.max_age_s,
            "version_key": list(_version_key(request, orchestration_version)),
            "source_snapshot_policy_version": str((snapshot or {}).get("policy_version") or ""),
            "state_at_verification": _snapshot_state(snapshot),
            "corridor": _verified_corridor(result),
            "verified_candidate_count": len((result or {}).get("verified_candidates") or []),
            "safety_semantics": "verified artifact reuse only; no synthetic candidate/final; fail closed outside phase/version/freshness/envelope",
        }

    def acquire(self, *, snapshot: dict[str, Any], request: dict[str, Any], orchestration_version: str,
                reference_time_s: float) -> ArtifactDecision:
        t0 = time.perf_counter()
        with self._lock:
            art = copy.deepcopy(self._artifact)
            refresh = dict(self._refresh_status)
        if not art:
            return ArtifactDecision(False, "no_verified_l4_artifact", metadata={"refresh": refresh, "gate_elapsed_s": time.perf_counter()-t0})
        meta = dict(art.get("metadata") or {})
        result = dict(art.get("result") or {})
        if not self._model_compatible(result):
            return ArtifactDecision(False,'artifact_model_dependencies_missing_or_changed',
                metadata={**meta,'actuation_authority':False})
        # A refresh that is still RUNNING must not stall the fast lane.  Once a
        # newer refresh has completed as UNVERIFIED, however, continuing to use
        # the older artifact would discard current-state safety evidence.  This
        # happened in the Online take-off path: a previously verified command was
        # reused for four more plant steps after fresh Layer-4 evaluations had no
        # verified candidate.  Keep the last artifact for audit/recovery, but make
        # it non-executable until a subsequent verified refresh replaces it.
        refresh_state = str(refresh.get("state") or "")
        try:
            refresh_reference = float(refresh.get("reference_time_s"))
            artifact_reference = float(meta.get("created_reference_time_s"))
        except (TypeError, ValueError):
            refresh_reference = artifact_reference = float("nan")
        if (
            refresh_state == "UNVERIFIED"
            and math.isfinite(refresh_reference)
            and math.isfinite(artifact_reference)
            and refresh_reference > artifact_reference + 1e-9
        ):
            return ArtifactDecision(False, "newer_refresh_unverified", metadata={
                **meta,
                "refresh": refresh,
                "newer_refresh_invalidates_execution": True,
                "gate_elapsed_s": time.perf_counter() - t0,
            })
        phase = str((request or {}).get("stage") or "")
        if meta.get("phase") != phase:
            return ArtifactDecision(False, "artifact_phase_mismatch", metadata={**meta, "refresh": refresh, "gate_elapsed_s": time.perf_counter()-t0})
        if tuple(meta.get("version_key") or ()) != _version_key(request, orchestration_version):
            return ArtifactDecision(False, "artifact_version_mismatch", metadata={**meta, "refresh": refresh, "gate_elapsed_s": time.perf_counter()-t0})
        created = float(meta.get("created_reference_time_s", reference_time_s))
        age = float(reference_time_s) - created
        if age < -1e-9 or age > self.max_age_s:
            return ArtifactDecision(False, "artifact_not_fresh", metadata={**meta, "artifact_age_s": age, "refresh": refresh, "gate_elapsed_s": time.perf_counter()-t0})
        current = _snapshot_state(snapshot)
        pos = current.get("position_ned_m"); vel = current.get("ground_velocity_ned_m_s")
        corridor = list(meta.get("corridor") or [])
        if pos is None or vel is None or not corridor:
            return ArtifactDecision(False, "artifact_envelope_unassessed", metadata={**meta, "artifact_age_s": age, "refresh": refresh, "gate_elapsed_s": time.perf_counter()-t0})
        best = None
        for point in corridor:
            pp = _vec3(point.get("position_ned_m")); vv = _vec3(point.get("ground_velocity_ned_m_s"))
            if pp is None or vv is None:
                continue
            pd = _distance(pos, pp); vd = _distance(vel, vv)
            score = pd / max(self.position_tolerance_m, 1e-9) + vd / max(self.velocity_tolerance_m_s, 1e-9)
            if best is None or score < best[0]:
                best = (score, pd, vd)
        if best is None or best[1] > self.position_tolerance_m or best[2] > self.velocity_tolerance_m_s:
            return ArtifactDecision(False, "artifact_envelope_mismatch", metadata={**meta, "artifact_age_s": age,
                "nearest_position_delta_m": None if best is None else best[1], "nearest_velocity_delta_m_s": None if best is None else best[2],
                "refresh": refresh, "gate_elapsed_s": time.perf_counter()-t0})
        out_meta = {**meta, "artifact_age_s": age, "nearest_position_delta_m": best[1], "nearest_velocity_delta_m_s": best[2],
                    "refresh": refresh, "gate_elapsed_s": time.perf_counter()-t0, "reuse": True,
                    "first_move_only_committable": True,
                    "remaining_horizon_is_execution_authority": False}
        result.setdefault("provenance", {})["rtmo_l4_artifact_reuse"] = copy.deepcopy(out_meta)
        return ArtifactDecision(True, "verified_artifact_reuse", result=result, metadata=out_meta,
                                prediction_snapshot=copy.deepcopy(art.get("prediction_snapshot")))

    def mark_first_move_executed(self, *, metadata: dict[str, Any], transaction_id: str,
                                 execution_status: str, actuation_executed: bool) -> dict[str, Any]:
        """Consume one transaction-scoped first-move grant after confirmed execution.

        Acquisition alone cannot consume a grant because Layer5/L6 may still hold or
        fail.  A late acknowledgement for an older artifact cannot be accepted after
        an atomic refresh.  The artifact itself remains reusable while all admission
        gates pass; it never becomes an open-loop action schedule.
        """
        supplied = self._artifact_key(dict(metadata or {}))
        with self._lock:
            current_meta = dict((self._artifact or {}).get("metadata") or {})
            current = self._artifact_key(current_meta) if current_meta else None
            if execution_status != "EXECUTED" or actuation_executed is not True:
                return {"ok": False, "reason": "execution_not_confirmed", "consumed": False}
            if current is None or supplied != current:
                return {"ok": False, "reason": "artifact_identity_mismatch", "consumed": False,
                        "supplied_key": list(supplied), "current_key": list(current) if current else None}
            txid = str(transaction_id or "")
            if not txid:
                return {"ok": False, "reason": "transaction_id_missing", "consumed": False}
            if txid in self._executed_transaction_ids:
                return {"ok": False, "reason": "transaction_first_move_already_consumed", "consumed": False,
                        "transaction_id": txid}
            self._executed_transaction_ids.add(txid)
            self._consumption_record = {
                "schema": "TDOC-RTMO-L4-transaction-first-move-consumption-v2",
                "transaction_id": txid,
                "execution_status": str(execution_status),
                "actuation_executed": True,
                "first_move_only_committable": True,
                "remaining_horizon_is_execution_authority": False,
                "artifact_reusable_after_transaction": True,
            }
            return {"ok": True, "reason": "verified_first_move_consumed", "consumed": True,
                    **copy.deepcopy(self._consumption_record)}

    def publish_bootstrap(self, *, result: dict[str, Any], snapshot: dict[str, Any], request: dict[str, Any],
                          orchestration_version: str, reference_time_s: float, l5_final: bool,
                          bootstrap_qualified: bool, transaction_id: str | None = None) -> ArtifactDecision:
        """Atomically seed the reusable fast-path cache from PRE_RELEASE verification.

        P0-4 contract: cold-start/phase-entry bootstrap is synchronous and non-actuating,
        but it may publish an artifact only after the same transaction has produced a
        verified Layer-4 result *and* a Layer-5 FINAL decision.  Publication cancels any
        older background refresh generation so stale work can never overwrite the fresh
        phase-entry artifact.
        """
        t0 = time.perf_counter()
        verified = self._is_verified(dict(result or {}))
        if not bool(bootstrap_qualified):
            return ArtifactDecision(False, "bootstrap_not_qualified", metadata={
                "publication_mode": "SYNCHRONOUS_PRE_RELEASE",
                "l4_verified": verified, "l5_final": bool(l5_final),
                "gate_elapsed_s": time.perf_counter()-t0,
            })
        if not verified:
            return ArtifactDecision(False, "bootstrap_l4_not_verified", metadata={
                "publication_mode": "SYNCHRONOUS_PRE_RELEASE",
                "l4_verified": False, "l5_final": bool(l5_final),
                "gate_elapsed_s": time.perf_counter()-t0,
            })
        if not bool(l5_final):
            return ArtifactDecision(False, "bootstrap_l5_not_final", metadata={
                "publication_mode": "SYNCHRONOUS_PRE_RELEASE",
                "l4_verified": True, "l5_final": False,
                "gate_elapsed_s": time.perf_counter()-t0,
            })
        meta = self._artifact_metadata(dict(result or {}), dict(snapshot or {}), dict(request or {}),
                                       orchestration_version, reference_time_s)
        meta.update({
            "publication_mode": "SYNCHRONOUS_PRE_RELEASE",
            "bootstrap_qualified": True,
            "l5_final": True,
            "bootstrap_transaction_id": str(transaction_id or ""),
            "atomic_publish": True,
            "background_refresh_required_after_bootstrap": True,
        })
        with self._lock:
            if self._closed:
                return ArtifactDecision(False, "artifact_gate_closed", metadata={
                    **meta, "gate_elapsed_s": time.perf_counter()-t0,
                })
            # Invalidate any older refresh worker from this episode/phase before the
            # atomic replacement.  The worker's generation check prevents late commit.
            self._generation += 1
            projected=self._execution_projection(dict(result or {}))
            self._artifact = {"result": projected, "metadata": copy.deepcopy(meta),
                              "prediction_snapshot": copy.deepcopy(snapshot)}
            self._executed_transaction_ids.clear()
            self._consumption_record = None
            self._refresh_status = {
                "state": "READY",
                "trigger_reason": "pre_release_current_phase_bootstrap",
                "publication_mode": "SYNCHRONOUS_PRE_RELEASE",
                "phase": str((request or {}).get("stage") or ""),
                "reference_time_s": float(reference_time_s),
                "verified": True,
                "verified_candidate_count": len((result or {}).get("verified_candidates") or []),
                "bootstrap_transaction_id": str(transaction_id or ""),
                "generation": self._generation,
            }
        out_meta = {**meta, "gate_elapsed_s": time.perf_counter()-t0}
        return ArtifactDecision(True, "bootstrap_artifact_published", result=copy.deepcopy(projected), metadata=out_meta)

    def schedule_refresh(self, *, invoke: Callable[[], dict[str, Any]], snapshot: dict[str, Any], request: dict[str, Any],
                         orchestration_version: str, reference_time_s: float, trigger_reason: str) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                return {"scheduled": False, "reason": "artifact_gate_closed"}
            if self._refresh_thread is not None and self._refresh_thread.is_alive():
                return {"scheduled": False, "reason": "refresh_already_running", **self._refresh_status}
            snap = copy.deepcopy(snapshot); req = copy.deepcopy(request)
            generation = self._generation
            self._refresh_status = {"state": "RUNNING", "trigger_reason": trigger_reason,
                                    "started_monotonic_s": time.monotonic(), "reference_time_s": float(reference_time_s),
                                    "phase": str(req.get("stage") or "")}

            def worker():
                t0 = time.monotonic()
                try:
                    out = invoke()
                    result = dict((out or {}).get("result") or {}) if isinstance(out, dict) else {}
                    verified = self._is_verified(result)
                    meta = self._artifact_metadata(result, snap, req, orchestration_version, reference_time_s) if verified else None
                    projected = self._execution_projection(result) if verified else None
                    with self._lock:
                        if generation != self._generation or self._closed:
                            return
                        if verified:
                            self._artifact = {"result": projected, "metadata": meta,
                                              "prediction_snapshot": copy.deepcopy(snap)}
                            self._executed_transaction_ids.clear()
                            self._consumption_record = None
                        self._refresh_status = {
                            "state": "READY" if verified else "UNVERIFIED",
                            "trigger_reason": trigger_reason,
                            "elapsed_s": time.monotonic() - t0,
                            "reference_time_s": float(reference_time_s),
                            "phase": str(req.get("stage") or ""),
                            "verified": verified,
                            "verified_candidate_count": len(result.get("verified_candidates") or []),
                        }
                except Exception as exc:
                    with self._lock:
                        if generation != self._generation or self._closed:
                            return
                        self._refresh_status = {"state": "FAILED", "trigger_reason": trigger_reason,
                                                "elapsed_s": time.monotonic()-t0, "error": repr(exc),
                                                "reference_time_s": float(reference_time_s), "phase": str(req.get("stage") or "")}

            self._refresh_thread = threading.Thread(target=worker, name="tdoc-rtmo-l4-refresh", daemon=True)
            self._refresh_thread.start()
            return {"scheduled": True, **self._refresh_status}


    def reset(self, reason: str = "new_online_episode") -> dict[str, Any]:
        """Invalidate every reusable artifact at a run/episode boundary.

        A refresh already running is not force-killed; its generation token prevents
        it from being promoted into the new episode.
        """
        with self._lock:
            self._generation += 1
            self._artifact = None
            self._executed_transaction_ids.clear()
            self._consumption_record = None
            self._refresh_status = {"state": "IDLE", "reset_reason": str(reason), "generation": self._generation}
            return {"ok": True, "generation": self._generation, "reason": str(reason)}

    def phase_seeded(self, phase: str) -> bool:
        """Return whether an atomically published artifact is available for ``phase``.

        This is intentionally a phase-entry/cold-start predicate only.  Full
        phase/version/freshness/envelope admission remains the responsibility of
        :meth:`acquire` inside the fast transaction.
        """
        with self._lock:
            meta = dict((self._artifact or {}).get("metadata") or {})
            available = bool(self._artifact is not None and str(meta.get("phase") or "") == str(phase))
            return available

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"refresh": copy.deepcopy(self._refresh_status), "has_artifact": self._artifact is not None,
                    "artifact_available": self._artifact is not None,
                    "artifact_metadata": copy.deepcopy((self._artifact or {}).get("metadata")),
                    "first_move_consumption": copy.deepcopy(self._consumption_record),
                    "executed_transaction_count_for_artifact": len(self._executed_transaction_ids)}

    def wait_for_idle(self, timeout_s: float = 5.0) -> bool:
        with self._lock:
            th = self._refresh_thread
        if th is None:
            return True
        th.join(max(float(timeout_s), 0.0))
        return not th.is_alive()

    def close(self) -> None:
        with self._lock:
            self._generation += 1
            self._closed = True
            self._artifact = None
            self._executed_transaction_ids.clear()
            self._consumption_record = None
