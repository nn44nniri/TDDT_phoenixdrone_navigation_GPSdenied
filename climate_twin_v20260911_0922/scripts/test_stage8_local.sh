#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
export PYTHONPATH="$HERE/python${PYTHONPATH:+:$PYTHONPATH}"
python3 -m pytest -q tests
printf 'STAGE8_LOCAL_CLIMATE_BOUNDARY_PASS\n'
