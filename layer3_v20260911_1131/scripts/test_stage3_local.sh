#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TDOC="${TDDT_TDOC_ROOT:-$HERE/../TDOC_ISO_Universal_Unit_Conversion_Standard20260824_0914}"
export PYTHONPATH="$HERE/src:$TDOC/src:${PYTHONPATH:-}"
cd "$HERE"
python3 -m pytest -q \
  tests/test_stage3_authority_and_atomicity.py \
  tests/test_future_timestamp_fail_closed.py \
  tests/test_tdoc_isolated_fmi_time_alignment.py \
  tests/test_packaging_contract.py \
  tests/test_site_forecast_fixture.py
