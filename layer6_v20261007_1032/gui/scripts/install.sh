#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [[ -z "${VIRTUAL_ENV:-}" ]]; then
  echo "[INFO] No active virtualenv; creating .venv"
  python3 -m venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
else
  echo "[INFO] Using active virtualenv: $VIRTUAL_ENV"
fi
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
python3 - <<'PY'
import yaml, pygame
from pymavlink import mavutil
from PyQt6.QtCore import QT_VERSION_STR
print('[OK] PyYAML:', yaml.__version__)
print('[OK] pygame:', pygame.version.ver)
print('[OK] pymavlink imported')
print('[OK] Qt:', QT_VERSION_STR)
PY
echo "[OK] Phoenix F310 Controller v3 dependencies installed."
