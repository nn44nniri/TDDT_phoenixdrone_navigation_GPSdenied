#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
python3 -m pytest -q tests/test_stage4_native_controller_boundary.py
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
set +e
python3 scripts/check_stage4_native_readiness.py \
  --snapshot examples/stage4_native_controller_EXAMPLE_NOT_SITE_EVIDENCE.json \
  --output "$TMP/report.json" > "$TMP/stdout.txt"
rc=$?
set -e
python3 - "$TMP/report.json" "$rc" <<'PY'
import json,sys
from pathlib import Path
r=json.loads(Path(sys.argv[1]).read_text())
assert int(sys.argv[2]) == 2
assert r['status'] == 'BLOCKED_NATIVE_CONTROLLER'
assert r['native_controller_certified'] is False
assert r['flight_authorized'] is False
print('STAGE4_LOCAL_GUARD_PASS')
PY
