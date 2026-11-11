#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  preinstall.sh [--ardupilot-root PATH] [--build] [--no-clean]

Examples:
  ./scripts/preinstall.sh --ardupilot-root /path/to/ardupilot
  ./scripts/preinstall.sh --ardupilot-root /path/to/ardupilot --build

Actions:
  1) validate package files
  2) create timestamped backup inside ArduPilot checkout
  3) install phoenix_v3 aircraft/reset/default parameters
  4) install or upgrade Phoenix SIM_JSBSim.cpp integration safely
  5) validate the installed checkout
  6) optionally clean/configure/build ArduPlane SITL
USAGE
}

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AP=""
DO_BUILD=0
DO_CLEAN=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ardupilot-root) AP="${2:-}"; shift 2 ;;
    --build) DO_BUILD=1; shift ;;
    --no-clean) DO_CLEAN=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      if [[ -z "$AP" && -d "$1" ]]; then AP="$1"; shift
      else echo "Unknown argument: $1" >&2; usage; exit 2
      fi ;;
  esac
done

if [[ -z "$AP" ]]; then
  if [[ -f "$PWD/libraries/SITL/SIM_JSBSim.cpp" ]]; then AP="$PWD"
  else echo "ERROR: --ardupilot-root is required outside an ArduPilot root." >&2; exit 2
  fi
fi
AP="$(cd "$AP" && pwd)"

for cmd in python3 patch cmp install; do
  command -v "$cmd" >/dev/null || { echo "ERROR: required command not found: $cmd" >&2; exit 3; }
done
[[ -f "$AP/libraries/SITL/SIM_JSBSim.cpp" ]] || { echo "ERROR: not an ArduPilot checkout: $AP" >&2; exit 4; }
[[ -x "$AP/waf" ]] || { echo "ERROR: missing executable $AP/waf" >&2; exit 4; }

if command -v JSBSim >/dev/null; then
  echo "JSBSim: $(command -v JSBSim)"
  JSBSim --version 2>/dev/null | head -n 3 || true
else
  echo "WARNING: JSBSim is not currently in PATH. Installation can continue, but runtime will require it." >&2
fi

echo "[1/6] Validating packaged Phoenix v3 files..."
python3 "$ROOT/scripts/validate_model.py"

STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP="$AP/.phoenix_v3_preinstall_backups/$STAMP"
mkdir -p "$BACKUP/Tools/autotest/aircraft/phoenix_v3" "$BACKUP/Tools/autotest/default_params" "$BACKUP/libraries/SITL"
backup_if_exists() {
  local src="$1" dst="$2"
  if [[ -e "$src" ]]; then cp -a "$src" "$dst"; fi
}
backup_if_exists "$AP/Tools/autotest/aircraft/phoenix_v3/phoenix_v3.xml" "$BACKUP/Tools/autotest/aircraft/phoenix_v3/"
backup_if_exists "$AP/Tools/autotest/aircraft/phoenix_v3/reset.xml" "$BACKUP/Tools/autotest/aircraft/phoenix_v3/"
backup_if_exists "$AP/Tools/autotest/aircraft/phoenix_v3/README.md" "$BACKUP/Tools/autotest/aircraft/phoenix_v3/"
backup_if_exists "$AP/Tools/autotest/default_params/phoenix_v3.parm" "$BACKUP/Tools/autotest/default_params/"
backup_if_exists "$AP/libraries/SITL/SIM_JSBSim.cpp" "$BACKUP/libraries/SITL/"
echo "[2/6] Backup created: $BACKUP"

echo "[3/6] Installing Phoenix aircraft model and parameter profile..."
"$ROOT/scripts/install_model.sh" "$AP"

echo "[4/6] Installing/upgrading SIM_JSBSim Phoenix integration..."
"$ROOT/scripts/apply_patch.sh" "$AP"

echo "[5/6] Validating installed checkout..."
python3 "$ROOT/scripts/validate_model.py" --ardupilot-root "$AP"

if [[ "$DO_BUILD" -eq 1 ]]; then
  echo "[6/6] Building ArduPlane SITL..."
  cd "$AP"
  if [[ "$DO_CLEAN" -eq 1 ]]; then ./waf clean; fi
  ./waf configure --board sitl
  ./waf plane
  echo "PASS: ArduPlane SITL build completed."
else
  echo "[6/6] Build skipped. Re-run with --build to compile automatically."
fi

cat <<EOF2

Phoenix v3 preinstall complete.
ArduPilot root: $AP
Backup: $BACKUP

Runtime command:
  cd "$AP"
  ./Tools/autotest/sim_vehicle.py -v ArduPlane -f jsbsim:phoenix_v3 -L PhoenixBoulder -w \\
    --add-param-file=Tools/autotest/default_params/phoenix_v3.parm \\
    --console --map --out=udp:127.0.0.1:14551 --out=udp:127.0.0.1:14552

After the first launch, verify generated runtime files with:
  "$ROOT/scripts/verify_runtime.sh" "$AP"
EOF2
