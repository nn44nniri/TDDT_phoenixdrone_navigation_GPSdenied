#!/usr/bin/env bash
set -Eeuo pipefail

REVISION="1-fixed5"
BASE_DIR="$PWD"
FLIGHTSTACK_DIR=""
DELAY_SECONDS="${FLIGHTSTACK_TERMINAL_DELAY:-2}"

usage() {
  cat <<USAGE
FlightStack three-terminal launcher - revision ${REVISION}

Usage:
  ./run_flightstack_3term_rev1.sh [--flightstack-dir DIR] [--delay SECONDS]

Defaults:
  FlightStack path: current working directory/FlightStack
  delay:            2 seconds between opening terminals

All paths are resolved from FlightStack itself. Child terminals are started with
--noprofile --norc, so obsolete ~/.profile or ~/.bashrc FlightStack paths are
not sourced by this launcher.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --flightstack-dir) FLIGHTSTACK_DIR="${2:?missing value}"; shift 2 ;;
    --delay) DELAY_SECONDS="${2:?missing value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$FLIGHTSTACK_DIR" ]] || FLIGHTSTACK_DIR="$BASE_DIR/FlightStack"
FLIGHTSTACK_DIR="$(cd "$FLIGHTSTACK_DIR" 2>/dev/null && pwd)" || {
  echo "ERROR: FlightStack not found: $FLIGHTSTACK_DIR" >&2
  exit 1
}

AP="$FLIGHTSTACK_DIR/ardupilot"
ENV_DIR="$FLIGHTSTACK_DIR/phoenix_environment_adapter_v2"
CTRL_DIR="$FLIGHTSTACK_DIR/phoenix_controller"
JSBSIM_BIN_DIR="$FLIGHTSTACK_DIR/jsbsim/build/src"
AP_VENV="$AP/venv-ardupilot-debian13"

[[ -x "$AP/Tools/autotest/sim_vehicle.py" ]] || { echo "ERROR: sim_vehicle.py not found: $AP/Tools/autotest/sim_vehicle.py" >&2; exit 1; }
[[ -f "$AP_VENV/bin/activate" ]] || { echo "ERROR: ArduPilot venv not found: $AP_VENV/bin/activate" >&2; exit 1; }
[[ -x "$JSBSIM_BIN_DIR/JSBSim" ]] || { echo "ERROR: JSBSim not found: $JSBSIM_BIN_DIR/JSBSim" >&2; exit 1; }
[[ -f "$ENV_DIR/.venv/bin/activate" ]] || { echo "ERROR: Environment Adapter venv not found: $ENV_DIR/.venv/bin/activate" >&2; exit 1; }
[[ -x "$CTRL_DIR/scripts/run_gui.sh" ]] || { echo "ERROR: controller run_gui.sh not found: $CTRL_DIR/scripts/run_gui.sh" >&2; exit 1; }

pick_terminal() {
  if command -v gnome-terminal >/dev/null 2>&1; then echo gnome-terminal
  elif command -v xfce4-terminal >/dev/null 2>&1; then echo xfce4-terminal
  elif command -v konsole >/dev/null 2>&1; then echo konsole
  elif command -v xterm >/dev/null 2>&1; then echo xterm
  elif command -v x-terminal-emulator >/dev/null 2>&1; then echo x-terminal-emulator
  else return 1
  fi
}
TERM_APP="$(pick_terminal)" || { echo "ERROR: no supported terminal emulator found." >&2; exit 1; }

launch_terminal() {
  local title="$1" cmd="$2"
  case "$TERM_APP" in
    gnome-terminal)
      gnome-terminal --title="$title" -- bash --noprofile --norc -c "$cmd" >/dev/null 2>&1 &
      ;;
    xfce4-terminal)
      xfce4-terminal --title="$title" --command="bash --noprofile --norc -c $(printf '%q' "$cmd")" >/dev/null 2>&1 &
      ;;
    konsole)
      konsole -p tabtitle="$title" -e bash --noprofile --norc -c "$cmd" >/dev/null 2>&1 &
      ;;
    xterm)
      xterm -T "$title" -e bash --noprofile --norc -c "$cmd" >/dev/null 2>&1 &
      ;;
    x-terminal-emulator)
      x-terminal-emulator -T "$title" -e bash --noprofile --norc -c "$cmd" >/dev/null 2>&1 &
      ;;
  esac
}

q() { printf '%q' "$1"; }
Q_FS="$(q "$FLIGHTSTACK_DIR")"
Q_AP="$(q "$AP")"
Q_ENV="$(q "$ENV_DIR")"
Q_CTRL="$(q "$CTRL_DIR")"
Q_JS="$(q "$JSBSIM_BIN_DIR")"
Q_VENV="$(q "$AP_VENV")"

# IMPORTANT fixed4: use exact process-name matching for arduplane/JSBSim.
# Using pkill -f here can match this terminal's own bash -c command line because
# that command line itself contains the string JSBSim, causing Terminal 1 to exit.
# Terminal 1 order intentionally follows the requested manual procedure:
# 1) JSBSim PATH + validation
# 2) stop previous processes
# 3) delete old JSBSim helper XML files
# 4) cd to FlightStack/ardupilot
# 5) activate ./venv-ardupilot-debian13
# 6) run sim_vehicle.py with the Phoenix profile
CMD1="echo '[Terminal 1] ArduPilot + JSBSim'; \
export PATH=$Q_JS:\$PATH; \
echo \"JSBSim PATH: $JSBSIM_BIN_DIR\"; \
which JSBSim; \
JSBSim --version; \
pkill -x arduplane || true; \
pkill -x JSBSim || true; \
pkill -f '[m]avproxy' || true; \
rm -f $Q_AP/Tools/autotest/jsbsim_start_0.xml; \
rm -f $Q_AP/Tools/autotest/jsbsim_fgout_0.xml; \
cd $Q_AP; \
source ./venv-ardupilot-debian13/bin/activate; \
./Tools/autotest/sim_vehicle.py \
  -v ArduPlane \
  -f jsbsim:phoenix_v3 \
  -L PhoenixBoulder \
  -w \
  --add-param-file=Tools/autotest/default_params/phoenix_v3.parm \
  --console \
  --map \
  --out=udp:127.0.0.1:14551 \
  --out=udp:127.0.0.1:14552; \
status=\$?; echo; echo \"sim_vehicle.py exited with status \$status\"; exec bash --noprofile --norc"

CMD2="cd $Q_ENV; \
source .venv/bin/activate; \
echo '[Terminal 2] Environment Adapter'; \
phoenix-env prepare \
  --dataset-tar ../data/internet_datasets.tar.gz \
  --region ../data/region_v1.geojson \
  --vehicle ../data/phoenix_drone_v3.yaml \
  --time 2025-05-20T12:00:00Z \
  --output runtime; \
echo; echo 'Environment preparation command completed.'; \
echo; echo 'Starting Environment Adapter runtime...'; \
phoenix-env runtime \
  --dataset-tar $Q_FS/data/internet_datasets.tar.gz \
  --region examples/region_v1.geojson \
  --time 2025-05-20T12:00:00Z \
  --cache runtime/dataset_cache \
  --mavlink udpin:0.0.0.0:14551 \
  --update-hz 1; \
status=\$?; echo; echo \"phoenix-env runtime exited with status \$status\"; \
exec bash --noprofile --norc"

CMD3="cd $Q_CTRL; \
source $Q_VENV/bin/activate; \
echo '[Terminal 3] Phoenix keyboard/F310 controller'; \
./scripts/run_gui.sh --connect udpin:0.0.0.0:14552; \
exec bash --noprofile --norc"

echo "Using terminal emulator: $TERM_APP"
echo "FlightStack root: $FLIGHTSTACK_DIR"
echo "ArduPilot root:   $AP"
echo "JSBSim bin dir:   $JSBSIM_BIN_DIR"
echo
echo "Opening Terminal 1: ArduPilot + JSBSim"
launch_terminal "FlightStack 1 - ArduPilot + JSBSim" "$CMD1"
sleep "$DELAY_SECONDS"

echo "Opening Terminal 2: Environment Adapter"
launch_terminal "FlightStack 2 - Environment Adapter" "$CMD2"
sleep "$DELAY_SECONDS"

echo "Opening Terminal 3: Phoenix Controller"
launch_terminal "FlightStack 3 - Phoenix Controller" "$CMD3"

echo "All three terminals were launched in order."
