#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 1 ]]; then
  echo "Usage: $0 /path/to/ardupilot" >&2
  exit 2
fi
AP="$(cd "$1" && pwd)"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
install -d "$AP/Tools/autotest/aircraft/phoenix_v3" "$AP/Tools/autotest/default_params"
install -m 0644 "$ROOT/aircraft/phoenix_v3/phoenix_v3.xml" "$AP/Tools/autotest/aircraft/phoenix_v3/phoenix_v3.xml"
install -m 0644 "$ROOT/aircraft/phoenix_v3/reset.xml" "$AP/Tools/autotest/aircraft/phoenix_v3/reset.xml"
install -m 0644 "$ROOT/aircraft/phoenix_v3/README.md" "$AP/Tools/autotest/aircraft/phoenix_v3/README.md"
install -m 0644 "$ROOT/default_params/phoenix_v3.parm" "$AP/Tools/autotest/default_params/phoenix_v3.parm"
echo "Installed phoenix_v3 v3 aircraft model, ground-VTOL reset and default parameter profile."
