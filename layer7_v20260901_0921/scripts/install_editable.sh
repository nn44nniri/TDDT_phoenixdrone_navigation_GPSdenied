#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ ! -f pyproject.toml ]]; then
  echo "ERROR: pyproject.toml is missing from package root: $ROOT" >&2
  exit 2
fi
python3 -m pip install -e .
echo "Installed tddt-layer7 from $ROOT"
command -v tddt-layer7 >/dev/null
command -v pytest >/dev/null
