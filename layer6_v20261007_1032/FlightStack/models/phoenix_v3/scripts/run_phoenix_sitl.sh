#!/usr/bin/env bash
set -euo pipefail
if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 /path/to/ardupilot [--wipe]" >&2
  exit 2
fi
AP="$(cd "$1" && pwd)"
WIPE=""
[[ "${2:-}" == "--wipe" ]] && WIPE="-w"
command -v JSBSim >/dev/null || { echo "ERROR: JSBSim is not in PATH" >&2; exit 3; }
[[ -x "$AP/Tools/autotest/sim_vehicle.py" ]] || { echo "ERROR: invalid ArduPilot root: $AP" >&2; exit 4; }
cd "$AP"
exec ./Tools/autotest/sim_vehicle.py \
  -v ArduPlane \
  -f jsbsim:phoenix_v3 \
  -l 40.010000,-105.300000,1798.37,53 \
  ${WIPE:+$WIPE} \
  --add-param-file=Tools/autotest/default_params/phoenix_v3.parm \
  --console --map \
  --out=udp:127.0.0.1:14551 \
  --out=udp:127.0.0.1:14552
