#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
source "$ROOT/configs/environment.sh"
python -m tddt_tdoc --config "$ROOT/configs/tdoc.yaml" generate-reports --output-dir "$ROOT/reports/visualization" "$@"
