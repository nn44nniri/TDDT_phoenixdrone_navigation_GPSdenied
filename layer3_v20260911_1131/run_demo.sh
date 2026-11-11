#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
export PYTHONPATH="$HERE/src${PYTHONPATH:+:$PYTHONPATH}"
python -m layer3.cli preflight --config configs/configuration_boulder.yaml
python -m layer3.cli demo --config configs/configuration_boulder.yaml --request examples/runtime_request.json --steps 6 --output runtime/demo_result.json
