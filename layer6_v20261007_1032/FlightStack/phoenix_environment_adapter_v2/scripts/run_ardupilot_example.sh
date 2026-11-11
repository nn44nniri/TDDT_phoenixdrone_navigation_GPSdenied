#!/usr/bin/env bash
set -euo pipefail
ARDUPILOT_DIR="${ARDUPILOT_DIR:-$HOME/ardupilot}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME="$ROOT/runtime"
mkdir -p "$HOME/.config/ardupilot"
grep -v "^PhoenixBoulder=" "$HOME/.config/ardupilot/locations.txt" 2>/dev/null > "$HOME/.config/ardupilot/locations.txt.tmp" || true
cat "$RUNTIME/locations.txt.fragment" >> "$HOME/.config/ardupilot/locations.txt.tmp"
mv "$HOME/.config/ardupilot/locations.txt.tmp" "$HOME/.config/ardupilot/locations.txt"
cd "$ARDUPILOT_DIR"
exec ./Tools/autotest/sim_vehicle.py \
  -v ArduPlane \
  -f jsbsim:phoenix_v3 \
  -L PhoenixBoulder \
  --add-param-file="$RUNTIME/phoenix_tailsitter.parm" \
  --console --map
