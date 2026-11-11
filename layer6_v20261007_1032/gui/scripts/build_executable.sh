#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
python3 -m pip install --upgrade pyinstaller
rm -rf build dist PhoenixF310GUI.spec
python3 -m PyInstaller \
  --clean \
  --noconfirm \
  --name PhoenixF310GUI \
  --add-data "config:config" \
  --collect-all pygame \
  ../src/gui/phoenix_f310_gui.py
printf '\nBuilt executable directory: %s/dist/PhoenixF310GUI\n' "$HERE"
printf 'Run: %s/dist/PhoenixF310GUI/PhoenixF310GUI --connect udpin:0.0.0.0:14552\n' "$HERE"
