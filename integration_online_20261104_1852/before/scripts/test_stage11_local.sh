#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
export PYTHONPATH="$HERE/src${PYTHONPATH:+:$PYTHONPATH}"
python3 -m pytest -q tests/test_stage11_integration_boundary.py \
  tests/test_config_registry.py tests/test_native_rejection_trace.py \
  tests/test_native_measured_clock.py \
  tests/test_native_bridge_contracts.py \
  tests/test_native_layer6_migration.py::test_ack_without_execution_correlation_fails_closed \
  || { echo 'STAGE11_LOCAL_REGRESSION_FAILED'; exit 1; }
echo 'STAGE11_LOCAL_CROSS_LAYER_GUARDS_PASS'
