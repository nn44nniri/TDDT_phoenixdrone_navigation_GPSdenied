#!/usr/bin/env bash
set -euo pipefail
L9="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$L9"
export PYTHONPATH="$L9${PYTHONPATH:+:$PYTHONPATH}"
python3 -m pytest -q tests
python3 -m py_compile region_geojson.py map_mosaic_query.py site_spatial_contract.py scripts/validate_stage9_site_map.py
printf 'STAGE9_LOCAL_SPATIAL_CONTRACT_PASS\n'
