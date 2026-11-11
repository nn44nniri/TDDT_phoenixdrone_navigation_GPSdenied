#!/usr/bin/env python3
"""Dynamic TDDT integration demo: Layer-5 -> Layer-6 -> Layer-7.

Default behaviour is a full flight-sequence contract test.  This program acts as
an in-process Layer-5 producer: it dynamically creates one canonical D_exec per
flight phase, submits every decision through Layer6Client, waits for the same
ArduPilot execution evidence used by demo_layer6.py, then emits one canonical
Layer7Input per decision.

The previous one-shot contract mode is retained with --single-decision.  A file
supplied by --layer5-file is used as the Layer-5 contract/template in dynamic
mode (constraints, safety, confidence, provenance and version are inherited),
or as the exact packet in --single-decision mode.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import random
import socket
import sys
import time
from typing import Any, Mapping

from layer6 import Layer6Client, Layer6ClientError
from layer6.client import PHASES


def _addr(text: str) -> tuple[str, int]:
    host, port = text.rsplit(":", 1)
    return host, int(port)


def _load_json(path: Path) -> Mapping[str, Any]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("Layer-5 input JSON root must be an object")
    return obj


def _receive_one(endpoint: str, timeout: float) -> Mapping[str, Any]:
    host, port = _addr(endpoint)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port)); sock.settimeout(timeout)
    print(f"[TDDT-DEMO] waiting for one Layer-5 packet/template on udp://{host}:{port}", flush=True)
    try:
        data, peer = sock.recvfrom(65535)
    finally:
        sock.close()
    obj = json.loads(data.decode("utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("Layer-5 UDP packet root must be a JSON object")
    print(f"[TDDT-DEMO] Layer-5 packet/template received from {peer[0]}:{peer[1]}", flush=True)
    return obj


def _send_layer7(endpoint: str, payload: Mapping[str, Any]) -> None:
    host, port = _addr(endpoint)
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(data, (host, port))
    finally:
        sock.close()
    print(f"[TDDT-DEMO] Layer7Input sent to udp://{host}:{port} ({len(data)} bytes)", flush=True)


def _jitter(value: float, amount: float, rng: random.Random, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value + rng.uniform(-amount, amount)))


def _packet_root(template: Mapping[str, Any]) -> Mapping[str, Any]:
    inner = template.get("layer6_package")
    return inner if isinstance(inner, Mapping) else template


def _build_dynamic_l5_decision(
    template: Mapping[str, Any], *, index: int, phase: str, mode: str,
    u_cmd: list[float], discrete: tuple[str, ...], ttl_s: float, rate_hz: float,
) -> dict[str, Any]:
    """Create a fresh Layer-5 D_exec while preserving the supplied L5 contract metadata."""
    base = copy.deepcopy(dict(_packet_root(template)))
    now = time.time()
    base["transaction_id"] = f"tddt-demo-{index:02d}-{phase}-{int(now * 1000)}"
    base["u_cmd"] = [float(x) for x in u_cmd]
    base.setdefault("C_exec", {})
    base["C_exec"].setdefault("pilot_domain", {"a": [-1, 1], "e": [-1, 1], "t": [0, 1], "r": [-1, 1]})
    base["C_exec"].setdefault("direct_pwm", False)
    base["C_exec"]["layer6_revalidation_required"] = True
    base.setdefault("S_safe", {"psf": True, "rta": True, "fallback": "baseline_safe_controller"})
    base["tau_apply"] = now
    base["tau_deadline"] = now + max(2.0, float(ttl_s))
    base.setdefault("rho_decision", 0.968)
    provenance = dict(base.get("pi_decision") or {})
    provenance.update({"source": "dynamic-demo-layer5", "phase": phase, "cycle": index})
    base["pi_decision"] = provenance
    base.setdefault("nu", "dynamic-demo-layer5-v1")
    base["execution_rate_hz"] = float(rate_hz)
    base["execution_intent"] = {
        "flight_phase": phase,
        "requested_pilot_mode": mode,
        "transition_semantics": "dynamic_layer5_demo",
        "discrete": list(discrete),
    }
    # Keep a minimal Layer-5 decision trace showing FINAL/first-move semantics.
    base["decision"] = {
        "mode": mode,
        "approval": {"governance": True, "final_gate": True},
        "FINAL": True,
    }
    base["control_action"] = {"first_move": list(base["u_cmd"])}
    return base


def _write_l7(
    l6: Layer6Client, l5: Mapping[str, Any], ack, status: Mapping[str, Any], *,
    phase: str, source: str, output: Path, trace: Path | None, endpoint: str | None,
) -> dict[str, Any]:
    l7 = l6.build_layer7_input(l5, ack=ack, status=status)
    l7["demo_metadata"] = {
        "source": "demo_tddt_l5_l7.py",
        "layer5_source": source,
        "phase": phase,
        "scope": "dynamic L5 -> L6 execution/reconstruction -> L7 contract test",
        "note": "PredictionEvidence and NetworkSnapshot remain null unless supplied by their owning layers.",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(l7, ensure_ascii=False, indent=2), encoding="utf-8")
    if trace is not None:
        trace.parent.mkdir(parents=True, exist_ok=True)
        with trace.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(l7, ensure_ascii=False, separators=(",", ":")) + "\n")
    if endpoint:
        _send_layer7(endpoint, l7)
    ev = l7.get("ExecutionEvidence", {})
    print(
        f"[TDDT-DEMO][L7] phase={phase} tx={l7.get('transaction_id')} "
        f"mode={ev.get('x_obs', {}).get('mode')} armed={ev.get('x_obs', {}).get('armed')} "
        f"u_applied={ev.get('u_applied_pilot')}", flush=True,
    )
    return l7


def _run_single(a, l6: Layer6Client, l5: Mapping[str, Any], source: str) -> None:
    ack = l6.submit(l5)
    print(f"[TDDT-DEMO] Layer-6 intake accepted tx={ack.transaction_id}", flush=True)
    if a.execution_wait:
        time.sleep(a.execution_wait)
    status = l6.gui_status(timeout=1.0)
    if not status or not status.get("mavlink_connected"):
        raise RuntimeError("Layer-6 cannot reconstruct execution evidence: GUI/MAVLink status unavailable")
    _write_l7(
        l6, l5, ack, status, phase="single-decision", source=source,
        output=a.layer7_output, trace=a.layer7_trace, endpoint=a.layer7_endpoint,
    )


def _run_dynamic(a, l6: Layer6Client, template: Mapping[str, Any], source: str) -> None:
    rng = random.Random(a.seed)
    # Fresh trace for each run.
    if a.layer7_trace:
        a.layer7_trace.parent.mkdir(parents=True, exist_ok=True)
        a.layer7_trace.write_text("", encoding="utf-8")

    for i, (name, mode, nominal, discrete, duration) in enumerate(PHASES):
        zero = name in {"ground", "arm", "touchdown", "disarm"}
        u = [
            0.0 if zero else _jitter(nominal[0], 0.03, rng),
            0.0 if zero else _jitter(nominal[1], 0.03, rng),
            0.0 if zero else _jitter(nominal[2], 0.025, rng, 0.0, 1.0),
            0.0 if zero else _jitter(nominal[3], 0.025, rng),
        ]
        hold_s = max(0.0, duration * a.time_scale)
        l5 = _build_dynamic_l5_decision(
            template, index=i, phase=name, mode=mode, u_cmd=u, discrete=discrete,
            ttl_s=hold_s + a.verify_timeout + 4.0, rate_hz=a.rate_hz,
        )
        print(
            f"[TDDT-DEMO][L5] {name:18s} mode={mode:10s} "
            f"u={[round(x, 3) for x in u]} discrete={discrete} tx={l5['transaction_id']}", flush=True,
        )
        ack = l6.submit(l5)
        print(f"[TDDT-DEMO][L6] intake accepted tx={ack.transaction_id}", flush=True)

        # Use the same execution/verification semantics as demo_layer6.py.
        if name in {"ground", "transition", "back_transition"}:
            l6._hold_and_verify(hold_s, verify_timeout=a.verify_timeout)
            status = l6._verify_mode_with_retry(
                mode=mode, command=u, discrete=discrete, hold_s=hold_s,
                verify_timeout=a.verify_timeout, transaction_prefix=l5["transaction_id"],
                rate_hz=a.rate_hz, retries=a.mode_retries,
            )
        elif name == "arm":
            status = l6._verify_arm_with_retry(
                command=u, hold_s=hold_s, verify_timeout=a.verify_timeout,
                transaction_prefix=l5["transaction_id"], rate_hz=a.rate_hz,
                retries=a.arm_retries,
            )
        else:
            status = l6._hold_and_verify(hold_s, verify_timeout=a.verify_timeout)

        if name == "touchdown":
            status = l6._wait_for_ground(a.landing_timeout)
        elif name == "disarm":
            status = l6._verify_disarm_with_retry(a.verify_timeout, a.disarm_retries)

        status = status or l6.gui_status(timeout=1.0)
        if not status or not status.get("mavlink_connected"):
            raise RuntimeError(f"No executable GUI/MAVLink evidence after phase {name}")
        _write_l7(
            l6, l5, ack, status, phase=name, source=source,
            output=a.layer7_output, trace=a.layer7_trace, endpoint=a.layer7_endpoint,
        )

    print(f"[TDDT-DEMO] dynamic sequence complete; latest Layer7Input: {a.layer7_output}", flush=True)
    if a.layer7_trace:
        print(f"[TDDT-DEMO] full Layer7 trace: {a.layer7_trace}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    p = argparse.ArgumentParser(description="Dynamic TDDT Layer5 -> Layer6 -> Layer7 execution demo")
    source = p.add_mutually_exclusive_group()
    source.add_argument("--layer5-file", type=Path, default=root / "config" / "layer5_decision_example.json",
                        help="Layer-5 contract/template JSON (default: config/layer5_decision_example.json)")
    source.add_argument("--layer5-listen", metavar="HOST:PORT",
                        help="receive one Layer-5 contract/template by UDP")
    p.add_argument("--single-decision", action="store_true",
                   help="retain legacy one-transaction contract test instead of dynamic full mission")
    p.add_argument("--layer5-timeout", type=float, default=30.0)
    p.add_argument("--layer6-endpoint", default="127.0.0.1:14661")
    p.add_argument("--execution-wait", type=float, default=1.0,
                   help="legacy --single-decision wait before reconstructing evidence")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--time-scale", type=float, default=1.0)
    p.add_argument("--rate-hz", type=float, default=30.0)
    p.add_argument("--connect-timeout", type=float, default=15.0)
    p.add_argument("--verify-timeout", type=float, default=8.0)
    p.add_argument("--landing-timeout", type=float, default=20.0)
    p.add_argument("--mode-retries", type=int, default=3)
    p.add_argument("--arm-retries", type=int, default=3)
    p.add_argument("--disarm-retries", type=int, default=3)
    p.add_argument("--layer7-output", type=Path, default=root / "runtime" / "layer7_input_demo.json",
                   help="latest Layer7Input JSON (overwritten after each phase)")
    p.add_argument("--layer7-trace", type=Path, default=root / "runtime" / "layer7_input_demo.jsonl",
                   help="JSONL trace containing one Layer7Input per dynamic Layer-5 decision")
    p.add_argument("--layer7-endpoint", metavar="HOST:PORT",
                   help="optional UDP destination; one Layer7Input is sent after every phase")
    p.add_argument("--start-if-needed", action="store_true")
    p.add_argument("--flightstack-dir")
    p.add_argument("--terminal", choices=("gnome-terminal","xfce4-terminal","konsole","xterm","x-terminal-emulator"))
    p.add_argument("--startup-check", type=float, default=3.0)
    p.add_argument("--debug", action="store_true")
    lifecycle = p.add_mutually_exclusive_group()
    lifecycle.add_argument("--stop-at-end", action="store_true")
    lifecycle.add_argument("--restart-at-end", action="store_true")
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if a.layer5_timeout <= 0 or a.execution_wait < 0 or a.connect_timeout <= 0 or a.verify_timeout <= 0:
        raise SystemExit("timeouts must be positive and --execution-wait must be >= 0")
    if a.time_scale < 0 or a.rate_hz <= 0:
        raise SystemExit("--time-scale must be >= 0 and --rate-hz must be > 0")
    if min(a.mode_retries, a.arm_retries, a.disarm_retries) < 1:
        raise SystemExit("retry counts must be >= 1")

    if a.layer5_listen:
        template = _receive_one(a.layer5_listen, a.layer5_timeout)
        source = f"udp://{a.layer5_listen}"
    else:
        template = _load_json(a.layer5_file)
        source = str(a.layer5_file)
    print(f"[TDDT-DEMO] Layer-5 {'packet' if a.single_decision else 'template'} source: {source}", flush=True)

    try:
        with Layer6Client(
            a.layer6_endpoint,
            start_if_needed=a.start_if_needed,
            flightstack_dir=a.flightstack_dir,
            terminal=a.terminal,
            startup_check=a.startup_check,
            lifecycle_debug=a.debug,
            startup_timeout=max(20.0, a.connect_timeout),
        ) as l6:
            l6.ensure_mavlink(a.connect_timeout)
            l6.wait_fcu_ready(timeout=max(20.0, a.connect_timeout, a.verify_timeout), stable_samples=3)

            if a.single_decision:
                _run_single(a, l6, template, source)
            else:
                _run_dynamic(a, l6, template, source)

            if a.restart_at_end:
                l6.restart_stack()
            elif a.stop_at_end:
                l6.stop_stack()
    except (Layer6ClientError, OSError, ValueError, RuntimeError) as exc:
        print(f"[TDDT-DEMO] ERROR: {exc}", file=sys.stderr, flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
