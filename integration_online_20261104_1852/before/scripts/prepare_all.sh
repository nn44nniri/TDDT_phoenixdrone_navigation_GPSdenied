#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/configs/environment.sh"
exec "${PYTHON:-python3}" "$ROOT/scripts/prepare_all.py" "$@"
