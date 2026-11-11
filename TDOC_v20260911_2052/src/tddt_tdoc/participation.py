from __future__ import annotations
from typing import Any, Mapping


def _downstream(cycle: Mapping[str, Any]) -> Mapping[str, Any]:
    return cycle.get("downstream_layers") or cycle.get("future_layers") or {}


def cycle_mpc_participation(cycle: Mapping[str, Any]) -> dict[str, Any]:
    """Return evidence-based Layer-4/Layer-5 MPC participation for one TDOC cycle.

    This function deliberately distinguishes computational participation from
    physical/execution authority.  It never infers actuation from an optimizer
    invocation or from a Layer-5 HOLD decision.
    """
    l4 = cycle.get("layer4_result") or {}
    l4_conf = l4.get("conformance") or {}
    l4_cands = l4.get("candidates") or []
    l4_feasible = [c for c in l4_cands if c.get("feasible") is True]
    l4_verified = l4.get("verified_candidates") or []
    l4_invoked = bool(l4)
    l4_optimizer = bool(l4_conf.get("C_optimizer"))
    l4_tube = bool(l4_conf.get("C_tube"))
    l4_robust = bool(l4_conf.get("C_robust"))
    l4_mpc = bool(l4_invoked and l4_optimizer and l4_tube)

    ds = _downstream(cycle)
    d5 = ds.get("5") or {}
    o5 = d5.get("output") or {}
    r5 = o5.get("result") or {}
    p5 = r5.get("provenance") or {}
    decision = r5.get("decision") or {}
    gates = r5.get("gates") or {}
    approval = decision.get("approval") or {}
    l5_invoked = bool(o5.get("ok") or r5)
    l5_mpc = bool(l5_invoked and p5.get("only_first_mpc_move") is True)
    # FINAL is a Layer-5 decision result, not a default boolean.  Preserve an
    # unevaluated state when Layer 5 did not run (for example an RTMO passive
    # boundary or an upstream Layer-4 artifact hold); reporting that state as
    # ``False`` incorrectly attributes an upstream stop to a safety rejection.
    final_evidence = gates.get("FINAL")
    if not isinstance(final_evidence, bool):
        final_evidence = approval.get("final_gate")
    final_gate = final_evidence if l5_invoked and isinstance(final_evidence, bool) else None
    control_present = bool(r5.get("control_action") or r5.get("layer6_package"))
    d6 = ds.get("6") or {}
    actuation_executed = bool(d6.get("status") == "EXECUTED" or ((d6.get("output") or {}).get("actuation_executed") is True))

    return {
        "schema": "TDOC-MPC-participation-cycle-v1",
        "transaction_id": cycle.get("transaction_id"),
        "reference_time_s": cycle.get("reference_time_s"),
        "learning_mode": (cycle.get("provenance") or {}).get("learning_mode"),
        "layer4": {
            "invoked": l4_invoked,
            "optimizer_active": l4_optimizer,
            "tube_mpc_active": l4_tube,
            "robust_active": l4_robust,
            "mpc_participated": l4_mpc,
            "candidate_count": len(l4_cands),
            "feasible_count": len(l4_feasible),
            "verified_count": len(l4_verified),
            "pareto_count": len(l4.get("pareto_front") or []),
            "physical_command_emitted": bool(l4.get("actuator_command")),
            "network_commit": bool(l4.get("network_commit")),
        },
        "layer5": {
            "invoked": l5_invoked,
            "mpc_first_move_participated": l5_mpc,
            "operational_mode": r5.get("operational_mode"),
            "final_gate": final_gate,
            "final_gate_evaluated": final_gate is not None,
            "candidate_id": decision.get("candidate_id"),
            "decision_reason": approval.get("reason") or p5.get("reason"),
            "control_present": control_present,
            "actuation_executed": actuation_executed,
        },
    }


def aggregate_mpc_participation(cycles: list[Mapping[str, Any]]) -> dict[str, Any]:
    rows = [cycle_mpc_participation(c) for c in cycles]
    n = len(rows)
    def count(path: tuple[str, str]) -> int:
        a, b = path
        return sum(bool((r.get(a) or {}).get(b)) for r in rows)
    l4_inv = count(("layer4", "invoked")); l4_mpc = count(("layer4", "mpc_participated"))
    l5_inv = count(("layer5", "invoked")); l5_mpc = count(("layer5", "mpc_first_move_participated"))
    feasible_cycles = sum((r["layer4"].get("feasible_count") or 0) > 0 for r in rows)
    verified_cycles = sum((r["layer4"].get("verified_count") or 0) > 0 for r in rows)
    final_cycles = count(("layer5", "final_gate")); act_cycles = count(("layer5", "actuation_executed"))
    def ratio(x: int, d: int) -> float:
        return float(x / d) if d else 0.0
    return {
        "schema": "TDOC-MPC-participation-summary-v1",
        "cycle_count": n,
        "layer4": {
            "invoked_cycles": l4_inv,
            "invocation_ratio_total": ratio(l4_inv, n),
            "mpc_participation_cycles": l4_mpc,
            "mpc_participation_ratio_total": ratio(l4_mpc, n),
            "mpc_participation_ratio_when_invoked": ratio(l4_mpc, l4_inv),
            "cycles_with_feasible_candidate": feasible_cycles,
            "feasible_cycle_ratio_when_invoked": ratio(feasible_cycles, l4_inv),
            "cycles_with_verified_candidate": verified_cycles,
            "verified_cycle_ratio_when_invoked": ratio(verified_cycles, l4_inv),
            "candidate_count_total": sum(int(r["layer4"].get("candidate_count") or 0) for r in rows),
            "feasible_candidate_count_total": sum(int(r["layer4"].get("feasible_count") or 0) for r in rows),
            "verified_candidate_count_total": sum(int(r["layer4"].get("verified_count") or 0) for r in rows),
            "interpretation": "MPC participation means qLPV/optimizer+tube computation was executed; it is not evidence of physical actuation.",
        },
        "layer5": {
            "invoked_cycles": l5_inv,
            "invocation_ratio_total": ratio(l5_inv, n),
            "mpc_first_move_cycles": l5_mpc,
            "mpc_participation_ratio_total": ratio(l5_mpc, n),
            "mpc_participation_ratio_when_invoked": ratio(l5_mpc, l5_inv),
            "final_gate_cycles": final_cycles,
            "final_gate_ratio_when_invoked": ratio(final_cycles, l5_inv),
            "actuation_executed_cycles": act_cycles,
            "actuation_ratio_when_invoked": ratio(act_cycles, l5_inv),
            "hold_cycles": sum((r["layer5"].get("operational_mode") == "HOLD") for r in rows),
            "interpretation": "Layer-5 MPC reasoning/first-move participation is separated from FINAL authorization and executed actuation.",
        },
        "cycles": rows,
    }



def _reasons(record: Any) -> list[str]:
    if not isinstance(record, Mapping):
        return []
    out: list[str] = []
    value = record.get("reasons")
    if isinstance(value, (list, tuple)):
        out.extend(str(x) for x in value if x not in (None, ""))
    elif value not in (None, ""):
        out.append(str(value))
    for key in ("reason", "error", "release_blocked_reason"):
        value = record.get(key)
        if value not in (None, ""):
            out.append(str(value))
    return list(dict.fromkeys(out))


def cycle_decision_gate_trace(cycle: Mapping[str, Any]) -> dict[str, Any]:
    """Build a read-only, per-cycle trace from Layer-4 verification to Layer-6 authority.

    This function does not participate in selection or authorization.  It only exposes
    existing Layer-4/Layer-5 evidence so a HOLD can be attributed to the first gate
    that actually rejected the path.  No safety gate is bypassed or relaxed.
    """
    l4 = cycle.get("layer4_result") or {}
    l4_candidates = l4.get("candidates") or []
    l4_by_id = {
        str(c.get("candidate_id")): c for c in l4_candidates
        if isinstance(c, Mapping) and c.get("candidate_id") is not None
    }
    verified_ids = {
        str(c.get("candidate_id")) for c in (l4.get("verified_candidates") or [])
        if isinstance(c, Mapping) and c.get("candidate_id") is not None
    }

    ds = _downstream(cycle)
    d5 = ds.get("5") or {}
    o5 = d5.get("output") or {}
    r5 = o5.get("result") if isinstance(o5, Mapping) and isinstance(o5.get("result"), Mapping) else o5
    r5 = r5 or {}
    gates = r5.get("gates") or {}
    decision = r5.get("decision") or {}
    approval = decision.get("approval") or {}
    fallback = r5.get("fallback") or {}

    ci = gates.get("integrity") or {}
    adm = gates.get("admissibility") or {}
    bind = gates.get("phoenix_binding") or {}
    policy = gates.get("policy_gate") or {}
    solver = gates.get("solver") or {}
    candidate_ids = set(l4_by_id) | set(map(str, ci)) | set(map(str, adm)) | set(map(str, bind)) | set(map(str, policy)) | set(map(str, solver))
    selected_id = decision.get("candidate_id")
    if selected_id is not None:
        candidate_ids.add(str(selected_id))

    candidates = []
    for cid in sorted(candidate_ids):
        c4 = l4_by_id.get(cid) or {}
        verification = c4.get("verification") or {}
        ci_rec = ci.get(cid) or {}
        adm_rec = adm.get(cid) or {}
        bind_rec = bind.get(cid) or {}
        policy_rec = policy.get(cid) or {}
        solver_rec = solver.get(cid) or {}
        vp_rec = solver_rec.get("vp") if isinstance(solver_rec, Mapping) else {}
        vp_rec = vp_rec if isinstance(vp_rec, Mapping) else {}
        verified = bool(c4.get("verified") is True or c4.get("candidate_verified") is True or cid in verified_ids)
        admissible = bool(
            ci_rec.get("ok") is True and adm_rec.get("ok") is True and
            bind_rec.get("ok") is True and policy_rec.get("ok") is True
        )
        solver_ok = bool(solver_rec.get("ok") is True and vp_rec.get("ok") is True) if solver_rec else None
        candidates.append({
            "candidate_id": cid,
            "layer4": {
                "feasible": c4.get("feasible"),
                "verified": verified,
                "verification_executed": verification.get("executed") if isinstance(verification, Mapping) else None,
                "verification_reasons": _reasons(verification),
            },
            "layer5": {
                "integrity_ok": ci_rec.get("ok") if ci_rec else None,
                "integrity_reasons": _reasons(ci_rec),
                "admissible": admissible,
                "admissibility_gate_ok": adm_rec.get("ok") if adm_rec else None,
                "admissibility_reasons": _reasons(adm_rec),
                "phoenix_binding_ok": bind_rec.get("ok") if bind_rec else None,
                "phoenix_binding_reasons": _reasons(bind_rec),
                "policy_gate_ok": policy_rec.get("ok") if policy_rec else None,
                "policy_reasons": _reasons(policy_rec),
                "qlpv_solver_ok": solver_rec.get("ok") if solver_rec else None,
                "verification_preserving_ok": vp_rec.get("ok") if vp_rec else None,
                "solver_reasons": _reasons(solver_rec) + _reasons(vp_rec),
                "selected": str(selected_id) == cid if selected_id is not None else False,
            },
        })

    tdoc_gate = gates.get("tdoc_boundary") or {}
    lifecycle = gates.get("lifecycle") or {}
    budget = gates.get("budget") or {}
    psf = gates.get("psf") or {}
    rta = gates.get("rta") or {}
    governance = gates.get("governance") or {}
    fallback_reason = fallback.get("reason") or approval.get("reason") or (r5.get("provenance") or {}).get("reason")

    # P1-9: root-cause attribution must stop at the first upstream gate that
    # actually prevented participation.  In the RTMO Online fast lane Layer-4
    # can be admission-ready while no reusable verified artifact exists.  In
    # that case Layer-5 is intentionally HELD and never invoked; attributing
    # FINAL=false to Layer-5 is downstream/misleading.  This is a read-only
    # diagnostic precedence rule and does not change any gate authority.
    prov = cycle.get("provenance") or {}
    learning_mode = str(prov.get("learning_mode") or "")
    pre_release = bool(prov.get("rtmo_pre_release_verify") is True)
    layer_status = cycle.get("layer_status") or {}
    l4_gate = layer_status.get("4") or {}
    l5_status = str(d5.get("status") or o5.get("status") or "NOT_RUN")
    l4_invoked = bool(l4)
    raw_l5_result = o5.get("result") if isinstance(o5, Mapping) else None
    l5_invoked = bool(
        (isinstance(raw_l5_result, Mapping) and bool(raw_l5_result))
        or (o5.get("ok") is True and l5_status not in ("HELD", "NOT_RUN", "NOT_INVOKED"))
    )
    final_evidence = gates.get("FINAL")
    if not isinstance(final_evidence, bool):
        final_evidence = approval.get("final_gate")
    final_gate = final_evidence if l5_invoked and isinstance(final_evidence, bool) else None
    l4_artifact_unavailable = bool(
        learning_mode == "ONLINE_JSBSIM"
        and not pre_release
        and not l4_invoked
        and l4_gate.get("ready") is True
        and not l5_invoked
        and l5_status in ("HELD", "NOT_RUN", "NOT_INVOKED")
    )

    stop_gate = "FINAL"
    stop_reasons: list[str] = []
    upstream_failure=next(((lid,(layer_status.get(lid) or {}).get('invocation') or {})
        for lid in ('2','3') if ((layer_status.get(lid) or {}).get('invocation') or {}).get('ok') is False),None)
    if upstream_failure:
        lid,invocation=upstream_failure
        reason=('DEADLINE_EXCEEDED' if invocation.get('late_output_discarded') else str(invocation.get('status') or 'FAILED'))
        stop_gate=f'L{lid}_{reason}'
        stop_reasons=[str(invocation.get('error') or reason)]
    elif l4_artifact_unavailable:
        stop_gate, stop_reasons = "L4_ARTIFACT_UNAVAILABLE", ["no_valid_l4_artifact"]
    elif tdoc_gate and tdoc_gate.get("ok") is False:
        stop_gate, stop_reasons = "L5_TDOC_BOUNDARY", _reasons(tdoc_gate)
    elif lifecycle and lifecycle.get("ok") is False:
        stop_gate, stop_reasons = "L5_LIFECYCLE", _reasons(lifecycle)
    elif fallback_reason == "no_admissible_bound_candidate":
        # Attribute the most informative upstream cause without changing L5 authority.
        if l4_candidates and not verified_ids:
            stop_gate = "L4_FULL_FIDELITY_VERIFICATION"
            reasons = []
            for c in candidates:
                reasons.extend((c.get("layer4") or {}).get("verification_reasons") or [])
            stop_reasons = list(dict.fromkeys(reasons)) or ["no_full_fidelity_verified_candidate"]
        else:
            stop_gate = "L5_ADMISSIBILITY"
            reasons = []
            for c in candidates:
                l5c = c.get("layer5") or {}
                reasons.extend(l5c.get("integrity_reasons") or [])
                reasons.extend(l5c.get("admissibility_reasons") or [])
                reasons.extend(l5c.get("phoenix_binding_reasons") or [])
                reasons.extend(l5c.get("policy_reasons") or [])
            stop_reasons = list(dict.fromkeys(reasons)) or [str(fallback_reason)]
    elif fallback_reason == "no_verification_preserving_solver_candidate":
        stop_gate = "L5_QLPV_VERIFICATION_PRESERVING"
        reasons = []
        for c in candidates:
            reasons.extend((c.get("layer5") or {}).get("solver_reasons") or [])
        stop_reasons = list(dict.fromkeys(reasons)) or [str(fallback_reason)]
    elif fallback_reason and not final_gate:
        stop_gate, stop_reasons = "L5_FALLBACK", [str(fallback_reason)]
    elif budget and budget.get("ok") is False:
        stop_gate, stop_reasons = "L5_EDGE_BUDGET", ["edge_deadline_exceeded"]
    elif psf and psf.get("gate") is not True:
        stop_gate, stop_reasons = "L5_PSF", _reasons(psf) or ["psf_gate_false"]
    elif rta and not (rta.get("gate") is True and rta.get("source") == "advanced"):
        stop_gate, stop_reasons = "L5_RTA", _reasons(rta) or ["rta_advanced_path_not_assured"]
    elif governance and governance.get("gate") is not True:
        stop_gate, stop_reasons = "L5_GOVERNANCE", _reasons(governance) or ["governance_gate_false"]
    elif not final_gate:
        native_reason=r5.get("native_intent_rejection")
        stop_gate, stop_reasons = "L5_FINAL", [str(native_reason) if native_reason else "FINAL_false_without_more_specific_gate"]

    d6 = ds.get("6") or {}
    o6 = d6.get("output") or {}
    ev6 = o6.get("execution_evidence") or {}
    write = ev6.get("write_result") or {}
    stream = write.get("stream_status") or {}
    last = stream.get("last_result") or {}
    release = last.get("release_confirmation") or ev6.get("release_confirmation") or {}

    # RTMO execution precedence: an upstream Layer-5 FINAL can never mask a
    # downstream Layer-6 failure/non-execution. This is orchestration-only and
    # does not alter PSF/RTA/governance or actuator authority.
    l6_status = str(d6.get("status") or o6.get("status") or "NOT_RUN")
    if l6_status == "FAILED":
        stop_gate = "EXECUTION_FAILED"
        stop_reasons = [str(o6.get("reason") or "layer6_failed")]
    elif stop_gate == "FINAL" and l6_status not in ("EXECUTED", "OBSERVATION_ONLY"):
        stop_gate = "EXECUTION_NOT_EXECUTED"
        stop_reasons = [f"layer6_status={l6_status}"]

    return {
        "schema": "TDOC-decision-gate-trace-cycle-v1",
        "transaction_id": cycle.get("transaction_id"),
        "reference_time_s": cycle.get("reference_time_s"),
        "learning_mode": (cycle.get("provenance") or {}).get("learning_mode"),
        "layer4": {
            "invoked": l4_invoked,
            "admission_ready": l4_gate.get("ready"),
            "artifact_unavailable": l4_artifact_unavailable,
            "candidate_count": len(l4_candidates),
            "feasible_count": sum(bool(c.get("feasible") is True) for c in l4_candidates if isinstance(c, Mapping)),
            "verified_count": len(verified_ids),
            "verified_candidate_ids": sorted(verified_ids),
        },
        "layer5": {
            "invoked": l5_invoked,
            "status": l5_status,
            "candidate_id": selected_id,
            "fallback_reason": fallback_reason,
            "psf": {"gate": psf.get("gate"), "reasons": _reasons(psf)},
            "rta": {"gate": rta.get("gate"), "source": rta.get("source"), "reasons": _reasons(rta)},
            "governance": {"gate": governance.get("gate"), "reasons": _reasons(governance)},
            "FINAL": final_gate,
            "FINAL_evaluated": final_gate is not None,
            "layer6_package_present": isinstance(r5.get("layer6_package"), Mapping),
        },
        "layer6": {
            "status": d6.get("status") or o6.get("status"),
            "reason": o6.get("reason"),
            "latch_running": stream.get("running"),
            "send_count": stream.get("send_count"),
            "release_confirmed": release.get("ready") if isinstance(release, Mapping) else None,
            "released": release.get("released") if isinstance(release, Mapping) else None,
            "holding": release.get("holding") if isinstance(release, Mapping) else None,
            "release_token_consumed": release.get("release_token_consumed") if isinstance(release, Mapping) else None,
            "direct_pwm": False,
            "mixer_owner": "ArduPilot",
        },
        "first_stop_gate": stop_gate,
        "first_stop_reasons": stop_reasons,
        "first_stop_attribution": {
            "policy": "earliest_nonparticipating_upstream_gate",
            "layer4_artifact_precedes_layer5_final": True,
            "source": "cycle_participation_evidence",
        },
        "candidates": candidates,
        "safety_semantics": "read_only_trace; no PSF/RTA/governance bypass; no direct PWM; ArduPilot mixer remains authoritative",
    }
