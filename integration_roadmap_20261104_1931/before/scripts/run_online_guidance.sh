#!/usr/bin/env bash
set -euo pipefail
TDOC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$TDOC_ROOT/configs/environment.sh"
exec "${PYTHON:-python3}" "$TDOC_ROOT/scripts/run_online_guidance.py" "$@"
