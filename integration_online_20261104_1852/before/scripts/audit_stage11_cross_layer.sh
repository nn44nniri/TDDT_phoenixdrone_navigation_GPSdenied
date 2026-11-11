#!/usr/bin/env bash
# Read-only TDOC cross-layer audit; no offline training, SITL or actuator writes.
set -euo pipefail
if [[ $# -lt 4 ]]; then
  echo "Usage: $0 STAGE8_REPORT STAGE9_REPORT STAGE10_SOURCE_FMU_REPORT OUTPUT_JSON [--require-native]" >&2
  exit 64
fi
S8="$1";S9="$2";S10="$3";OUT="$4";shift 4
TDOC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TDOC"
export PYTHONPATH="$TDOC/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m tddt_tdoc.cli audit-integration \
  --stage8-report "$S8" --stage9-report "$S9" --stage10-report "$S10" \
  --output "$OUT" "$@"
