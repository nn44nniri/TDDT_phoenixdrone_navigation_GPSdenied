#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATASET="${1:-/path/to/internet_datasets.tar.gz}"
TIME="${2:-2025-05-20T12:00:00Z}"
python3 -m phoenix_env_adapter.cli prepare \
  --dataset-tar "$DATASET" \
  --region "$ROOT/examples/region_v1.geojson" \
  --vehicle "$ROOT/examples/phoenix_drone_v3.yaml" \
  --time "$TIME" \
  --output "$ROOT/runtime"
