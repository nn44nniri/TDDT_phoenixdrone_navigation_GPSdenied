#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
PYTHONPATH=src python -m layer5.cli demo --output runtime/demo_result.json
