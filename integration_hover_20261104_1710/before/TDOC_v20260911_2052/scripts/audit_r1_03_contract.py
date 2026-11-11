#!/usr/bin/env python3
"""R1.03: record the TDOC/Layer6 boundary without importing or starting it.

A successful capture is not a compatible API or flight release. Current defects
are kept in the matrix. Signature binding uses AST plus inspect, never eval.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
import tomllib

TDOC = "TDOC_v20260911_2052"
L6 = "layer6_v20261007_1032"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def owned(root, relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("source path must be relative and contained")
    current = root
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("source symlink not allowed")
    current.resolve().relative_to(root)
    return current


def class_methods(tree, name):
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    if cls.bases:
        raise ValueError("inherited API requires explicit review")
    return {n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def signature(node):
    """Build an unexecuted signature including positional-only and keyword-only rules."""
    a = node.args
    positional = a.posonlyargs + a.args
    defaults = [inspect.Parameter.empty] * (len(positional) - len(a.defaults)) + [ast.unparse(d) for d in a.defaults]
    params = []
    for index, (p, default) in enumerate(zip(positional, defaults)):
        kind = inspect.Parameter.POSITIONAL_ONLY if index < len(a.posonlyargs) else inspect.Parameter.POSITIONAL_OR_KEYWORD
        params.append(inspect.Parameter(p.arg, kind, default=default))
    if a.vararg:
        params.append(inspect.Parameter(a.vararg.arg, inspect.Parameter.VAR_POSITIONAL))
    for p, default in zip(a.kwonlyargs, a.kw_defaults):
        params.append(inspect.Parameter(p.arg, inspect.Parameter.KEYWORD_ONLY,
                      default=inspect.Parameter.empty if default is None else ast.unparse(default)))
    if a.kwarg:
        params.append(inspect.Parameter(a.kwarg.arg, inspect.Parameter.VAR_KEYWORD))
    return inspect.Signature(params)


def display_signature(node, name=None):
    returns = " -> " + ast.unparse(node.returns) if node.returns is not None else ""
    return f"{name or node.name}({ast.unparse(node.args)}){returns}"


def check_call(method, call):
    if method is None:
        return {"status": "MISSING_API", "detail": "method absent from current Layer6Client"}
    if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
        return {"status": "UNRESOLVED_CALL", "detail": "dynamic argument expansion requires review"}
    try:
        signature(method).bind(object(), *[object() for _ in call.args],
                               **{k.arg: object() for k in call.keywords})
    except TypeError as exc:
        return {"status": "SIGNATURE_MISMATCH", "detail": str(exc)}
    return {"status": "CALL_SHAPE_COMPATIBLE", "detail": "argument binding only; semantics not validated"}


def client_receiver(node):
    if isinstance(node, ast.Name):
        return node.id == "client"
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
        return node.attr in {"client", "_client"}
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"
            and node.func.attr == "_get_client")


def client_calls(tree):
    """Receivers in the two reviewed boundary files; no whole-program inference."""
    functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    result = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
            continue
        constructor = isinstance(n.func.value, ast.Name) and n.func.value.id == "module" and n.func.attr == "Layer6Client"
        if not constructor and not client_receiver(n.func.value):
            continue
        parents = [f for f in functions if f.lineno <= n.lineno <= f.end_lineno]
        owner = min(parents, key=lambda f: f.end_lineno-f.lineno).name if parents else "<module>"
        result.append(("__init__" if constructor else n.func.attr, owner, n))
    return sorted(result, key=lambda row: row[2].lineno)


def returned_dict_keys(tree, method):
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == method)
    returns = [n for n in ast.walk(node) if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict)]
    if len(returns) != 1 or any(not isinstance(k, ast.Constant) or not isinstance(k.value, str) for k in returns[0].value.keys):
        raise ValueError("status producer changed shape; explicit review required")
    return sorted(k.value for k in returns[0].value.keys)


def source_ref(root, sources, ref):
    key, anchor = ref.split(":", 1)
    relative = sources[key]
    text = owned(root, relative).read_text()
    if relative.endswith(".py"):
        matches = [n for n in ast.walk(ast.parse(text)) if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == anchor]
        if len(matches) != 1:
            raise ValueError(f"ambiguous or missing source anchor: {ref}")
        line = matches[0].lineno
    else:
        matches = [i for i, line in enumerate(text.splitlines(), 1) if anchor in line]
        if len(matches) != 1:
            raise ValueError(f"ambiguous or missing text anchor: {ref}")
        line = matches[0]
    return {"ref": ref, "path": relative, "line": line}


def audit(root, contract_path):
    contract = json.loads(contract_path.read_text())
    sources = contract["sources"]
    before = {p: sha(owned(root, p)) for p in sources.values()}
    before[str(contract_path.relative_to(root))] = sha(contract_path)
    trees = {key: ast.parse(owned(root, relative).read_text()) for key, relative in sources.items() if relative.endswith(".py")}
    methods = class_methods(trees["client"], "Layer6Client")
    declared = {r["method"]: r for r in contract["api"]}
    if len(declared) != len(contract["api"]) or len({r["field"] for r in contract["status"]}) != len(contract["status"]):
        raise ValueError("duplicate API/status row")
    if any(not row.get(k) for row in contract["api"] + contract["status"] for k in ("unit_frame", "time_tx", "owner", "evidence")):
        raise ValueError("contract row lacks units/frame, timing, ownership or evidence")
    call_rows = []
    for key in ("adapter", "bridge"):
        for name, caller, call in client_calls(trees[key]):
            call_rows.append({"method": name, "caller": caller, "path": sources[key], "line": call.lineno,
                              "expected_call": ast.unparse(call), **check_call(methods.get(name), call)})
    uncovered = sorted({row["method"] for row in call_rows} - set(declared))
    required_nodes = [n for n in ast.walk(trees["adapter"]) if isinstance(n, ast.Tuple)
                      and all(isinstance(x, ast.Constant) and isinstance(x.value, str) for x in n.elts)
                      and {"submit", "cancel", "gui_status"}.issubset({x.value for x in n.elts})]
    if len(required_nodes) != 1:
        raise ValueError("preflight API declaration changed; explicit review required")
    required = [x.value for x in required_nodes[0].elts]
    if set(required) - set(declared):
        raise ValueError("required preflight API not documented")
    api = []
    for name, row in declared.items():
        node = methods.get(name)
        calls = [r for r in call_rows if r["method"] == name]
        if not calls:
            raise ValueError(f"documented production API has no current call: {name}")
        statuses = {r["status"] for r in calls}
        status = "MISSING_API" if node is None else "SIGNATURE_MISMATCH" if "SIGNATURE_MISMATCH" in statuses else "UNRESOLVED_CALL" if "UNRESOLVED_CALL" in statuses else "CALL_SHAPE_COMPATIBLE"
        api.append({**row, "actual_signature": None if node is None else display_signature(node, name),
                    "status": status, "preflight_required": name in required,
                    "definition": None if node is None else {"path": sources["client"], "line": node.lineno}, "calls": calls})
    keys = returned_dict_keys(trees["gui"], "_virtual_status_payload")
    status_fields = []
    for row in contract["status"]:
        # Availability here concerns explicit top-level producer keys only.
        status_fields.append({**row, "actual_availability": "PUBLISHED_KEY_VALUES_MAY_BE_UNAVAILABLE" if row["field"] in keys else "MISSING_KEY"})
    if set(keys) - {r["field"] for r in status_fields}:
        raise ValueError("GUI status has undocumented keys")
    sections = ("api", "status", "packet", "clocks", "transactions", "readiness", "gaps")
    refs = sorted({ref for section in sections for row in contract[section] for ref in row["evidence"]})
    evidence = [source_ref(root, sources, ref) for ref in refs]
    actual_version = tomllib.loads(owned(root, sources["project"]).read_text())["project"]["version"]
    after = {p: sha(owned(root, p)) for p in before}
    if before != after or uncovered:
        raise ValueError(f"source drift or undocumented API: {uncovered}")
    return {"schema": "TDDT-R1.03-contract-audit-v1", "step": "R1.03", "audit_status": "PASS",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(), "bundle_root": str(root),
            "contract_version": contract["version"], "source_hashes": before, "sources_unchanged": before == after,
            "preflight_required_api": required, "api": api, "status_fields": status_fields,
            "layer6_project_version": actual_version,
            "producer_status_keys": keys, "source_references": evidence,
            "api_signature_compatibility": "PASS" if all(r["status"] == "CALL_SHAPE_COMPATIBLE" for r in api) else "FAIL",
            "public_client_inventory": [{"method": name, "signature": display_signature(n, name), "line": n.lineno,
                                         "production_boundary": name in declared}
                                        for name, n in methods.items() if not name.startswith("_") or name == "__init__"],
            "coverage": {"production_methods": len(api), "callsites": len(call_rows), "status_rows": len(status_fields),
                         "producer_keys": len(keys), "uncovered_methods": uncovered},
            "scope": "static producer/consumer contract capture; no API execution, no transport, no live telemetry",
            "flight_ready": False, "runtime_started": False, "contract": contract}


def matrix(result):
    c = result["contract"]
    lines = ["# ماتریس قرارداد TDOC ↔ Layer6 — R1.03", "",
             "**ثبت قرارداد: PASS؛ سازگاری امضاهای API فعلی: " + result["api_signature_compatibility"] + ".**",
             "", "این سند وضعیت موجود و نیاز مصرف‌کننده را جدا ثبت می‌کند. مقادیر زنده اندازه‌گیری نشده‌اند؛ این نتیجه مجوز اجرا یا پرواز نیست.", "",
             "## متدها و امضاهای واقعی", "", "| متد | امضای واقعی | نیاز TDOC | واحد / frame | زمان و transaction | نتیجه / گام مسئول |",
             "|---|---|---|---|---|---|"]
    for row in result["api"]:
        sig = (row['actual_signature'] or 'MISSING').replace('|', '\\|')
        lines.append(f"| `{row['method']}` | `{sig}` | {row['purpose']} | {row['unit_frame']} | {row['time_tx']} | {row['status']}؛ {row['owner']} |")
    lines += ["", "CALL_SHAPE_COMPATIBLE فقط به معنی پذیرفتن آرگومان‌های فراخوان فعلی است؛ رفتار اجرایی، کیفیت داده و safety را تأیید نمی‌کند.",
              "", "## محل دقیق همهٔ فراخوان‌های TDOC", "", "| متد | فراخوان / خط | عبارت مورد انتظار مصرف‌کننده | نتیجه |", "|---|---|---|---|"]
    for row in result["api"]:
        for call in row["calls"]:
            lines.append(f"| `{row['method']}` | `{call['path']}:{call['line']}` | `{call['expected_call']}` | {call['status']}: {call['detail']} |")
    lines += ["", "## قرارداد status", "", "انتشار یک کلید، معتبر یا تازه‌بودن مقدار آن را اثبات نمی‌کند. نبودن کلید با صفر/identity یا truth جبران نمی‌شود.", "",
              "| فیلد status | واحد / frame و نیاز | زمان / تازگی / transaction | انتشار فعلی | گام مسئول |", "|---|---|---|---|---|"]
    for row in result["status_fields"]:
        lines.append(f"| `{row['field']}` | {row['unit_frame']}؛ {row['requirement']} | {row['time_tx']} | {row['actual_availability']} | {row['owner']} |")
    for key, title in (("packet", "بستهٔ تصمیم و evidence"), ("clocks", "زمان‌ها و cadence"),
                       ("transactions", "correlation و لغو"), ("readiness", "statusهای چرخهٔ عمر"), ("gaps", "اختلاف‌های باز و مسئول رفع")):
        lines += ["", "## " + title, "", "| شناسه | قرارداد مورد نیاز | رفتار موجود / محدودیت | مسئول |", "|---|---|---|---|"]
        for row in c[key]:
            lines.append(f"| `{row['id']}` | {row['required']} | {row['actual']} | {row['owner']} |")
    lines += ["", "## قواعد معماری", ""] + ["- " + note for note in c["invariants"]]
    lines += ["", "## شواهد سورس", "", "تمام ارجاع‌ها در audit با فایل واقعی تطبیق داده شده‌اند؛ SHA-256 فایل‌ها در CONTRACT_AUDIT.json است.", ""]
    for ref in result["source_references"]:
        lines.append(f"- `{ref['ref']}` → `{ref['path']}:{ref['line']}`")
    lines += ["", "## فهرست عمومی Client", "", "متدهای اضافهٔ عمومی از مسیر تولیدی TDOC تفکیک شده‌اند؛ اجرای demo دلیل بسته‌شدن حلقه نیست.", ""]
    for row in result["public_client_inventory"]:
        lines.append(f"- `{row['signature']}`؛ {'مصرف مستقیم TDOC' if row['production_boundary'] else 'خارج از فراخوان مستقیم مرز TDOC؛ run_demo_sequence مسیر demo است'}")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.bundle_root.resolve()
    output = args.output_dir.absolute()
    output.resolve().relative_to(root / TDOC / "reports/online")
    if not output.name.startswith("R1_03_"):
        raise ValueError("output must be a new R1_03_ directory under TDOC reports/online")
    contract = root / TDOC / "docs/contracts/TDOC_LAYER6_API_STATUS_CONTRACT_v1.json"
    result = audit(root, contract)
    output.mkdir(parents=True, exist_ok=False)
    (output / "CONTRACT_AUDIT.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    (output / "TDOC_LAYER6_API_STATUS_MATRIX_FA_v1.md").write_text(matrix(result))
    print(json.dumps({k: result[k] for k in ("step", "audit_status", "api_signature_compatibility", "coverage", "flight_ready")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
