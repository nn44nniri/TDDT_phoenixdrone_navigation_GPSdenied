#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
python3 -m pytest -q tests
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
python3 -m tdoc_unit_converter.boundary_cli \
  --input examples/stage7_ned_velocity_EXAMPLE_NOT_SITE_EVIDENCE.json \
  --output "$TMP/good.json" --target-unit m/s --target-frame ENU \
  --expected-semantic navigation_velocity --expected-role current_state \
  --expected-producer MESA_NAV --expected-epoch-id EXAMPLE_SIM_EPOCH \
  --reference-time-s 10.01 >"$TMP/good_stdout.json"
python3 - "$TMP/good.json" <<'PY'
import json,sys
r=json.load(open(sys.argv[1]));assert r['status']=='CONVERSION_DIAGNOSTIC_PASS_UNCERTIFIED';assert r['result']['value']==[2.,1.,3.];assert r['flight_authorized'] is False
print('STAGE7_LOCAL_BOUNDARY_PASS')
PY
