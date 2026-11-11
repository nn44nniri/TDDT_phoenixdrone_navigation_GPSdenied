#!/usr/bin/env python3
"""Emit a fail-closed, cycle-level audit of the offline L2→L3→L4→L5 loop."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
from collections import Counter
from pathlib import Path
from typing import Any

import yaml


def _read(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def _bool(value: Any) -> bool:
    return value is True


def _cycle_number(transaction_id: str) -> int:
    try:
        return int(transaction_id.rsplit("-", 1)[-1])
    except (TypeError, ValueError):
        return -1


def _phase_map(report_dir: Path) -> dict[str, str]:
    path = report_dir / "MISSION_TIMELINE.csv"
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8", newline="") as stream:
        return {
            str(row.get("transaction_id")): str(row.get("phase") or "")
            for row in csv.DictReader(stream)
        }


def _offline_layer4_timing_contract(root: Path) -> dict[str, Any]:
    """Load the authoritative non-actuating full-horizon deadline.

    Offline Layer4 evaluates the complete prediction horizon and may only publish
    a verified first move to Layer5.  It is therefore not the Online committable
    control transaction governed by ``deadlines.layer4_s``.  Keep the two timing
    domains explicit and fail closed if the centralized contract is absent.
    """
    path = root / "configs" / "tdoc.yaml"
    source = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    deadlines = source.get("deadlines") or {}
    deadline = deadlines.get("layer4_pre_release_s")
    if not isinstance(deadline, (int, float)) or float(deadline) <= 0.0:
        raise RuntimeError("deadlines.layer4_pre_release_s is required for offline Layer4 audit")
    return {
        "timing_segment": "NON_ACTUATING_OFFLINE_FULL_HORIZON",
        "deadline_key": "deadlines.layer4_pre_release_s",
        "deadline_s": float(deadline),
        "actuation_forbidden_by_segment": True,
    }


def audit(root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    report_dir = root / "reports" / "offline" / "mission"
    phases = _phase_map(report_dir)
    l4_timing_contract = _offline_layer4_timing_contract(root)
    rows: list[dict[str, Any]] = []
    for path in sorted((root / "runtime" / "cycles").glob("mission-offline-*.json.gz")):
        cycle = _read(path)
        tx = str(cycle.get("transaction_id") or path.name.removesuffix(".json.gz"))
        statuses = cycle.get("layer_status") or {}
        l2 = statuses.get("2") or {}
        l3 = statuses.get("3") or {}
        l4 = statuses.get("4") or {}
        gates = ((cycle.get("provenance") or {}).get("decision_gate_trace") or {})
        l4_gate = gates.get("layer4") or {}
        l5_gate = gates.get("layer5") or {}
        feedback = ((cycle.get("future_layers") or {}).get("2_offline_feedback") or {})
        feedback_output = feedback.get("output") or {}
        timing = ((cycle.get("provenance") or {}).get("component_timing_s") or {})
        contract = ((cycle.get("provenance") or {}).get("offline_timing_contract") or {})
        ledger = ((cycle.get("provenance") or {}).get("knowledge_ledger") or {})
        storage = ledger.get("storage") or {}
        l3_snapshot = (cycle.get("layer3_result") or {}).get("snapshot") or {}
        final = _bool(l5_gate.get("FINAL"))
        effect = _bool(feedback_output.get("control_effect_applied"))
        feedback_applied = _bool(feedback_output.get("training_feedback_applied"))
        layer_ready = all(_bool(item.get("ready")) for item in (l2, l3, l4))
        l4_elapsed = timing.get("L4")
        l4_deadline = float(l4_timing_contract["deadline_s"])
        l4_deadline_ok = (
            isinstance(l4_elapsed, (int, float))
            and float(l4_elapsed) <= l4_deadline
        )
        loop_closed = _bool(contract.get("loop_closed"))
        contract_ok = _bool(contract.get("ok"))
        fallback_reason = l5_gate.get("fallback_reason")

        if final and effect and feedback_applied and layer_ready and l4_deadline_ok and loop_closed and contract_ok:
            outcome = "PASS_CONTROL"
        elif (not final) and (not effect) and str(feedback.get("status") or "").upper() == "HELD":
            outcome = "SAFETY_HOLD"
        else:
            outcome = "FAIL"

        rows.append(
            {
                "cycle": _cycle_number(tx),
                "transaction_id": tx,
                "phase": phases.get(tx, ""),
                "reference_time_s": cycle.get("reference_time_s"),
                "input_l2_packet_count": len(cycle.get("layer2_packets") or {}),
                "input_l3_snapshot_version": (
                    l3_snapshot.get("version")
                    or l3_snapshot.get("snapshot_version")
                    or l3_snapshot.get("policy_version")
                ),
                "l2_ready": _bool(l2.get("ready")),
                "l3_ready": _bool(l3.get("ready")),
                "l4_ready": _bool(l4.get("ready")),
                "l4_candidate_count": l4_gate.get("candidate_count"),
                "l4_feasible_count": l4_gate.get("feasible_count"),
                "l4_verified_count": l4_gate.get("verified_count"),
                "l5_final": final,
                "l5_fallback_reason": fallback_reason,
                "l2_feedback_status": feedback.get("status"),
                "l2_training_feedback_applied": feedback_applied,
                "l2_control_effect_applied": effect,
                "first_stop_gate": gates.get("first_stop_gate"),
                "first_stop_reasons": "|".join(str(x) for x in (gates.get("first_stop_reasons") or [])),
                "l2_elapsed_s": timing.get("L2"),
                "l3_elapsed_s": timing.get("L3"),
                "l4_elapsed_s": l4_elapsed,
                "l4_full_fidelity_s": timing.get("L4_full_fidelity"),
                "l5_elapsed_s": timing.get("L5"),
                "l4_timing_segment": l4_timing_contract["timing_segment"],
                "l4_deadline_key": l4_timing_contract["deadline_key"],
                "l4_deadline_s": l4_deadline,
                "l4_deadline_ok": l4_deadline_ok,
                "offline_loop_closed": loop_closed,
                "offline_contract_ok": contract_ok,
                "knowledge_recorded": _bool(ledger.get("knowledge_target_reached")),
                "knowledge_chunk": storage.get("chunk"),
                "knowledge_record_index": storage.get("record_index"),
                "outcome": outcome,
            }
        )

    counts = Counter(row["outcome"] for row in rows)
    l4_times = [float(row["l4_elapsed_s"]) for row in rows if isinstance(row["l4_elapsed_s"], (int, float))]
    summary = {
        "schema": "TDOC-offline-cycle-contract-audit-v2",
        "acceptance_rule": "PASS_CONTROL requires FINAL=true, physical control effect, L2 feedback, ready L2/L3/L4, Layer4 within its centralized NON_ACTUATING_OFFLINE_FULL_HORIZON deadline, and closed/OK offline contract; FINAL=false is never PASS",
        "cycles": len(rows),
        "outcomes": dict(counts),
        "all_cycles_classified_without_fail": counts.get("FAIL", 0) == 0,
        "layer4_timing_contract": l4_timing_contract,
        "all_layer4_within_segment_deadline": bool(l4_times) and all(
            value <= float(l4_timing_contract["deadline_s"]) for value in l4_times
        ),
        "online_control_segment_deadline_unchanged_s": float(
            ((yaml.safe_load((root / "configs" / "tdoc.yaml").read_text(encoding="utf-8")) or {}).get("deadlines") or {}).get("layer4_s")
        ),
        "layer4_max_s": max(l4_times) if l4_times else None,
        "knowledge_chunks": sorted({str(row["knowledge_chunk"]) for row in rows if row["knowledge_chunk"]}),
    }
    return rows, summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    rows, summary = audit(root)
    out = root / "reports" / "offline" / "mission"
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / "OFFLINE_CYCLE_CONTRACT_AUDIT.csv"
    if rows:
        with csv_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    (out / "OFFLINE_CYCLE_CONTRACT_AUDIT.json").write_text(
        json.dumps({"summary": summary, "cycles": rows}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if (summary["all_cycles_classified_without_fail"]
                 and summary["all_layer4_within_segment_deadline"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
