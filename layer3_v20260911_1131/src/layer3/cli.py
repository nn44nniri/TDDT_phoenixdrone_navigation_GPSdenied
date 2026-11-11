from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import Layer3Config
from .runtime import Layer3FMURuntime, RuntimeRequest
from .dependency import DependencyRegistry
from .utils import write_json


def _load_config(path: str) -> Layer3Config:
    return Layer3Config.from_yaml(path)


def cmd_run_request(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    req = RuntimeRequest.from_mapping(json.loads(Path(args.request).read_text(encoding="utf-8")))
    with Layer3FMURuntime(cfg) as rt:
        result = rt.step(req)
    write_json(args.output, result.to_dict())
    print(json.dumps({
        "ok": True,
        "output": str(Path(args.output).resolve()),
        "policy_version": result.snapshot.policy_version,
        "mismatch": result.snapshot.mismatch,
        "consistency_margin": result.snapshot.consistency_margin,
    }, indent=2))
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    reg = DependencyRegistry(cfg)
    python_status = reg.validate_python_runtime(strict=False)
    package_status = reg.validate(strict=False)
    api_status = reg.validate_api_contracts(strict=False)
    dataset_status = reg.validate_datasets(strict=False)
    report = {"ok": bool(python_status["all_ok"] and package_status["all_ok"] and api_status["all_ok"] and dataset_status["all_ok"]),
              "python_runtime": python_status, "external_packages": package_status,
              "how_usage_api_contracts": api_status, "datasets": dataset_status}
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 2


def cmd_demo(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    base = json.loads(Path(args.request).read_text(encoding="utf-8"))
    results = []
    with Layer3FMURuntime(cfg) as rt:
        for i in range(int(args.steps)):
            data = json.loads(json.dumps(base))
            data["reference_time_s"] = float(base.get("reference_time_s", 0.001)) + i * cfg.time.fast_dt_s
            req = RuntimeRequest.from_mapping(data)
            r = rt.step(req)
            results.append(r.to_dict())
    summary = {
        "ok": True,
        "steps": len(results),
        "meso_events": sum(1 for r in results if r.get("meso_event")),
        "slow_events": sum(1 for r in results if r.get("slow_event")),
        "last_snapshot": results[-1]["snapshot"] if results else None,
        "results": results,
    }
    write_json(args.output, summary)
    print(json.dumps({
        "ok": True,
        "steps": len(results),
        "meso_events": summary["meso_events"],
        "slow_events": summary["slow_events"],
        "output": str(Path(args.output).resolve()),
    }, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="TDDT Dynamic N-Twin Layer-3 runtime")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run-request", help="Execute one direct-FMU Layer-3 request")
    p.add_argument("--config", default="configs/configuration_boulder.yaml")
    p.add_argument("--request", default="examples/runtime_request.json")
    p.add_argument("--output", default="runtime/run_request_result.json")
    p.set_defaults(func=cmd_run_request)
    pf = sub.add_parser("preflight", help="Validate external package paths and Python runtime dependencies")
    pf.add_argument("--config", default="configs/configuration_boulder.yaml")
    pf.set_defaults(func=cmd_preflight)
    d = sub.add_parser("demo", help="Run multiple direct-FMU fast ticks")
    d.add_argument("--config", default="configs/configuration_boulder.yaml")
    d.add_argument("--request", default="examples/runtime_request.json")
    d.add_argument("--steps", type=int, default=6)
    d.add_argument("--output", default="runtime/demo_result.json")
    d.set_defaults(func=cmd_demo)
    args = ap.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
