#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
rm -rf "$ROOT/build" "$ROOT/fmus"
cmake -S "$ROOT" -B "$ROOT/build" -DCMAKE_BUILD_TYPE=Release
cmake --build "$ROOT/build" -j
python3 "$ROOT/scripts/package_fmus.py"
