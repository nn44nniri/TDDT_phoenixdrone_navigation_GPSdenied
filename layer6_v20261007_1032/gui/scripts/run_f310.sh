#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$ROOT/.venv/bin/activate" && "${VIRTUAL_ENV:-}" == "" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
fi
exec python3 "$ROOT/../src/gui/phoenix_f310_controller.py" \
  --config "$ROOT/config/f310_phoenix.yaml" \
  "$@"
