#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
python3 -m pytest -q tests/test_stage5_native_pilot_boundary.py
RUN="$(mktemp -d)"
trap 'rm -rf "$RUN"' EXIT
set +e
python3 scripts/check_stage5_native_boundary.py \
 --input examples/layer4_result_verified_TEST_FIXTURE.json \
 --learning-mode ONLINE_JSBSIM --output "$RUN/native.json" > "$RUN/stdout.txt"
RC=$?
set -e
python3 - "$RUN/native.json" "$RC" <<'PY'
import json,sys
from pathlib import Path
x=json.loads(Path(sys.argv[1]).read_text())
assert int(sys.argv[2])==2,sys.argv[2]
assert x['status']=='BLOCKED_NATIVE_PILOT_CONTROLLER'
assert x['flight_authorized'] is False
assert x['native_controller_certified'] is False
assert x['full_fidelity_authority_for_online'] is False
print('STAGE5_LOCAL_NATIVE_BOUNDARY_PASS')
PY
