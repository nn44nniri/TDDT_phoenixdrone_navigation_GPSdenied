#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
cd "$HERE"
python3 -m pytest -q tests
printf '%s\n' STAGE10_LOCAL_REFERENCE_CONTRACT_PASS
