#!/usr/bin/env bash
# Deterministic source/native-evidence guards only; no FlightStack access.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT"
python3 -m pytest -q tests/test_stage6_native_pair_boundary.py
TMP="$(mktemp -d)" || exit 1
trap 'rm -rf "$TMP"' EXIT
python3 scripts/validate_stage6_native_pair.py \
  --input examples/stage6_native_pair_EXAMPLE_NOT_SITE_EVIDENCE.json \
  --output "$TMP/native_pair.json" >"$TMP/diagnostic_stdout.json"
python3 - "$TMP/native_pair.json" <<'PY'
import json, sys
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text())
assert p['status']=='PAIR_COMPARABLE_DIAGNOSTIC_ONLY'
assert p['comparable'] and not p['native_controller_certified']
assert not p['dynamic_validation_certified'] and not p['operational_commit_allowed']
assert not p['flight_authorized']
print('STAGE6_NATIVE_PAIR_GUARD_PASS')
PY
