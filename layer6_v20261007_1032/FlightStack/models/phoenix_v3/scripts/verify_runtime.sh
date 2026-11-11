#!/usr/bin/env bash
set -euo pipefail
if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 /path/to/ardupilot [runtime_log.txt]" >&2
  exit 2
fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AP="$(cd "$1" && pwd)"
ARGS=(--ardupilot-root "$AP" --check-runtime)
if [[ $# -eq 2 ]]; then ARGS+=(--runtime-log "$2"); fi
python3 "$ROOT/scripts/validate_model.py" "${ARGS[@]}"
