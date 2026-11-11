#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m pytest -q tests "$@"
