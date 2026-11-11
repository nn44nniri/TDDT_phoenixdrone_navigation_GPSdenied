#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
export PYTHONPATH="$HERE/src${PYTHONPATH:+:$PYTHONPATH}"
python -m layer3.cli preflight --config configs/configuration_boulder.yaml
python examples/run_layer3_scenario.py \
  --config configs/configuration_boulder.yaml \
  --request examples/runtime_request.json \
  --steps "${L3_SCENARIO_STEPS:-36}" \
  --terrain-grid-n "${L3_SCENARIO_TERRAIN_GRID_N:-90}" \
  --output-dir "${L3_SCENARIO_OUTPUT:-reports/scenario}"
