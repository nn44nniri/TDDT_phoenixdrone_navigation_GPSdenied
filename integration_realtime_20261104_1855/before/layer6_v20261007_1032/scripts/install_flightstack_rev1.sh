#!/usr/bin/env bash
set -Eeuo pipefail

REVISION="1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$PWD"
ASSETS_DIR=""
FLIGHTSTACK_DIR=""
REPAIR_MODEL_ONLY=0
CHECK_REPLAY_MAP_ONLY=0

usage() {
  cat <<USAGE
FlightStack Debian 13 installer - revision ${REVISION}

Usage:
  ./install_flightstack_rev1.sh [--base-dir DIR] [--assets-dir DIR] [--repair-model-only] [--check-replay-map]

  --repair-model-only uses the existing stack, aligns model/data and rebuilds
  ArduPlane without downloads, prerequisite installation or resetting knowledge.
  --check-replay-map verifies the native MAVProxy/wx/OpenCV map dependencies only.

Defaults:
  --base-dir   current working directory: $PWD
  --assets-dir current working directory, then the script directory

The final installation path is always:
  <base-dir>/FlightStack

This deliberately does NOT use the old fixed path:
  /media/p1/DigitalTwin/TDDT_navigation/arduPilot/
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-dir) BASE_DIR="${2:?missing value for --base-dir}"; shift 2 ;;
    --assets-dir) ASSETS_DIR="${2:?missing value for --assets-dir}"; shift 2 ;;
    --repair-model-only) REPAIR_MODEL_ONLY=1; shift ;;
    --check-replay-map) CHECK_REPLAY_MAP_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

BASE_DIR="$(mkdir -p "$BASE_DIR" && cd "$BASE_DIR" && pwd)"
FLIGHTSTACK_DIR="$BASE_DIR/FlightStack"

log() { printf '\n[%s] %s\n' "$(date +'%H:%M:%S')" "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

find_asset() {
  local canonical="$1"; shift
  local dirs=()
  [[ -n "$ASSETS_DIR" ]] && dirs+=("$ASSETS_DIR")
  dirs+=("$PWD" "$SCRIPT_DIR")
  local d pattern f
  for d in "${dirs[@]}"; do
    [[ -d "$d" ]] || continue
    if [[ -f "$d/$canonical" ]]; then printf '%s\n' "$d/$canonical"; return 0; fi
    for pattern in "$@"; do
      shopt -s nullglob
      for f in "$d"/$pattern; do
        [[ -f "$f" ]] && { printf '%s\n' "$f"; shopt -u nullglob; return 0; }
      done
      shopt -u nullglob
    done
  done
  return 1
}

require_cmd() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"; }

check_replay_map() {
  local replay_python="$FLIGHTSTACK_DIR/ardupilot/venv-ardupilot-debian13/bin/python"
  [[ -x "$replay_python" ]] || die "Native Python missing for replay map."
  PYTHONPATH="$SCRIPT_DIR/../src${PYTHONPATH:+:$PYTHONPATH}" \
    "$replay_python" -B -m layer6.replay_map --check-dependencies || \
    die "Native map dependencies missing; install MAVProxy, wxPython, OpenCV, NumPy and Pillow for the native Python."
}

if [[ "$CHECK_REPLAY_MAP_ONLY" == 1 ]]; then
  check_replay_map
  exit 0
fi

for c in git tar cmake make python3 patch; do require_cmd "$c"; done

install_aligned_model() {
  python3 "$SCRIPT_DIR/apply_r6_04_body_rates.py" --ardupilot-root "$FLIGHTSTACK_DIR/ardupilot" --restore-r303-base
  python3 "$SCRIPT_DIR/apply_r3_03_model_alignment.py" --flightstack-dir "$FLIGHTSTACK_DIR"
  (
    cd "$FLIGHTSTACK_DIR/models/phoenix_v3"
    ./scripts/preinstall.sh --ardupilot-root "$FLIGHTSTACK_DIR/ardupilot" --no-clean
  )
  python3 "$SCRIPT_DIR/apply_r6_04_body_rates.py" --ardupilot-root "$FLIGHTSTACK_DIR/ardupilot"
  (
    cd "$FLIGHTSTACK_DIR/ardupilot"
    "$PYTHON" -B ./waf configure --board sitl
    "$PYTHON" -B ./waf plane
  )
}

if [[ "$REPAIR_MODEL_ONLY" == 1 ]]; then
  AP_VENV="$FLIGHTSTACK_DIR/ardupilot/venv-ardupilot-debian13"
  [[ -f "$AP_VENV/bin/activate" ]] || die "Existing ArduPilot venv required."
  python3 "$SCRIPT_DIR/apply_r3_03_model_alignment.py" --flightstack-dir "$FLIGHTSTACK_DIR" --repair-venvs-only
  source "$AP_VENV/bin/activate"
  export PYTHON="$AP_VENV/bin/python"
  export PATH="$FLIGHTSTACK_DIR/jsbsim/build/src:$PATH"
  python3 "$SCRIPT_DIR/apply_r3_02_environment_alignment.py" --environment-root "$FLIGHTSTACK_DIR/phoenix_environment_adapter_v2"
  python3 "$SCRIPT_DIR/apply_r4_02_environment_binding.py" --environment-root "$FLIGHTSTACK_DIR/phoenix_environment_adapter_v2"
  install_aligned_model
  check_replay_map
  python3 "$SCRIPT_DIR/apply_r3_03_model_alignment.py" --flightstack-dir "$FLIGHTSTACK_DIR" --refresh-ground-profile
  log "Four-foot model/data repair and native rebuild completed; flight readiness is not granted."
  exit 0
fi

PREREQ_ARCHIVE="$(find_asset 'install-prereqs-debian13-pexpect-fix-v2.tar.gz' 'install-prereqs-debian13-pexpect-fix-v2.tar*.gz' 'install-prereqs-debian13*.tar*.gz')" || die "Debian 13 prerequisite archive not found."
MODEL_ARCHIVE="$(find_asset 'phoenix_v3_jsbsim_model_v4.tar.gz' 'phoenix_v3_jsbsim_model_v4*.tar*.gz')" || die "Phoenix JSBSim model archive not found."
ENV_ARCHIVE="$(find_asset 'phoenix_environment_adapter_v2.tar.gz' 'phoenix_environment_adapter_v2*.tar*.gz')" || die "Environment Adapter archive not found."
CTRL_ARCHIVE="$(find_asset 'phoenix_f310_controller_v6.tar.gz' 'phoenix_f310_controller_v6*.tar*.gz')" || die "F310 controller archive not found."
DATASET_ARCHIVE="$(find_asset 'internet_datasets.tar.gz' 'internet_datasets.tar*.gz')" || die "internet_datasets archive not found."
REGION_FILE="$(find_asset 'region_v1.geojson' 'region_v1*.geojson')" || die "region_v1.geojson not found."
ALIGNMENT_HELPER="$SCRIPT_DIR/apply_r3_02_environment_alignment.py"
[[ -f "$ALIGNMENT_HELPER" ]] || die "R3.02 environment alignment helper not found."
# Use the pinned canonical profile, rather than a stale copy in the asset archive.
VEHICLE_FILE="$(python3 "$ALIGNMENT_HELPER" --print-profile)" || die "R3.02 canonical vehicle profile invalid."

log "Installation root: $FLIGHTSTACK_DIR"
log "Assets directory resolution complete."

mkdir -p "$FLIGHTSTACK_DIR"/{phoenix_environment_adapter_v2,phoenix_controller,models,data,runtime}

clone_repo() {
  local url="$1" dest="$2" recurse="${3:-0}"
  if [[ -d "$dest/.git" ]]; then
    log "Repository already exists; keeping current checkout: $dest"
    return 0
  fi
  [[ ! -e "$dest" || -z "$(ls -A "$dest" 2>/dev/null || true)" ]] || die "Destination exists and is not an empty git checkout: $dest"
  rm -rf "$dest"
  if [[ "$recurse" == 1 ]]; then git clone --recurse-submodules "$url" "$dest"; else git clone "$url" "$dest"; fi
}

log "Cloning ArduPilot"
clone_repo https://github.com/ArduPilot/ardupilot.git "$FLIGHTSTACK_DIR/ardupilot" 1

log "Installing Debian 13 ArduPilot prerequisites"
# IMPORTANT: the Debian-13 prerequisite script derives ARDUPILOT_ROOT as
#   realpath "$SCRIPT_DIR/../../"
# and creates its venv from $PWD. Therefore it must live directly in
# ArduPilot/Tools/environment_install and must be executed with cwd=ArduPilot.
PREREQ_DEST="$FLIGHTSTACK_DIR/ardupilot/Tools/environment_install"
mkdir -p "$PREREQ_DEST"
rm -f "$PREREQ_DEST/install-prereqs-debian13-v2.sh"
tar -xzf "$PREREQ_ARCHIVE" --strip-components=1 -C "$PREREQ_DEST"
PREREQ_SCRIPT="$PREREQ_DEST/install-prereqs-debian13-v2.sh"
[[ -f "$PREREQ_SCRIPT" ]] || die "Prerequisite script not found after extraction: $PREREQ_SCRIPT"
chmod +x "$PREREQ_SCRIPT"

# Layer-6 invariant: never read-modify-write the user's ~/.bashrc or ~/.profile.
# All runtime/build paths are process-local.  The third-party prerequisite helper
# is executed with an isolated temporary HOME so any shell-customisation attempt
# cannot touch the user's real home directory.
INSTALL_HOME="$FLIGHTSTACK_DIR/runtime/install-home"
rm -rf "$INSTALL_HOME"
mkdir -p "$INSTALL_HOME"
(
  cd "$FLIGHTSTACK_DIR/ardupilot"
  HOME="$INSTALL_HOME" \
  PATH="$PATH" \
  "$PREREQ_SCRIPT" -y
)
rm -rf "$INSTALL_HOME"

AP_VENV="$FLIGHTSTACK_DIR/ardupilot/venv-ardupilot-debian13"
[[ -f "$AP_VENV/bin/activate" ]] || die "Expected ArduPilot venv was not created: $AP_VENV"
# shellcheck disable=SC1091
source "$AP_VENV/bin/activate"
export PYTHON="$AP_VENV/bin/python"

log "Building ArduPlane SITL"
cd "$FLIGHTSTACK_DIR/ardupilot"
./waf configure --board sitl
./waf plane
[[ -x build/sitl/bin/arduplane ]] || die "ArduPlane SITL binary was not produced."

log "Cloning and building JSBSim"
clone_repo https://github.com/JSBSim-Team/jsbsim.git "$FLIGHTSTACK_DIR/jsbsim" 0
cmake -S "$FLIGHTSTACK_DIR/jsbsim" -B "$FLIGHTSTACK_DIR/jsbsim/build" -DCMAKE_BUILD_TYPE=Release
cmake --build "$FLIGHTSTACK_DIR/jsbsim/build" -j"$(nproc)"
export PATH="$FLIGHTSTACK_DIR/jsbsim/build/src:$PATH"
command -v JSBSim >/dev/null 2>&1 || die "JSBSim binary is not available in expected build path."
JSBSim --version || true

log "Installing Phoenix v3 model into JSBSim/ArduPilot integration"
rm -rf "$FLIGHTSTACK_DIR/models/phoenix_v3"
mkdir -p "$FLIGHTSTACK_DIR/models/phoenix_v3"
tar -xzf "$MODEL_ARCHIVE" --strip-components=1 -C "$FLIGHTSTACK_DIR/models/phoenix_v3"
install_aligned_model

log "Installing Phoenix Environment Adapter"
rm -rf "$FLIGHTSTACK_DIR/phoenix_environment_adapter_v2"
mkdir -p "$FLIGHTSTACK_DIR/phoenix_environment_adapter_v2"
tar -xzf "$ENV_ARCHIVE" --strip-components=1 -C "$FLIGHTSTACK_DIR/phoenix_environment_adapter_v2"
python3 "$ALIGNMENT_HELPER" --environment-root "$FLIGHTSTACK_DIR/phoenix_environment_adapter_v2"
python3 "$SCRIPT_DIR/apply_r4_02_environment_binding.py" --environment-root "$FLIGHTSTACK_DIR/phoenix_environment_adapter_v2"
cd "$FLIGHTSTACK_DIR/phoenix_environment_adapter_v2"
chmod +x ./scripts/install.sh
./scripts/install.sh

log "Installing Phoenix F310/keyboard controller"
rm -rf "$FLIGHTSTACK_DIR/phoenix_controller"
mkdir -p "$FLIGHTSTACK_DIR/phoenix_controller"
tar -xzf "$CTRL_ARCHIVE" --strip-components=1 -C "$FLIGHTSTACK_DIR/phoenix_controller"
cd "$FLIGHTSTACK_DIR/phoenix_controller"
chmod +x ./scripts/install.sh ./scripts/run_gui.sh
# Runtime documentation activates the ArduPilot venv for the controller.
# Activate it here as well so controller dependencies are installed there.
# shellcheck disable=SC1091
source "$AP_VENV/bin/activate"
./scripts/install.sh

log "Copying mission/environment input data"
cp -f "$DATASET_ARCHIVE" "$FLIGHTSTACK_DIR/data/internet_datasets.tar.gz"
cp -f "$REGION_FILE" "$FLIGHTSTACK_DIR/data/region_v1.geojson"
cp -f "$VEHICLE_FILE" "$FLIGHTSTACK_DIR/data/phoenix_drone_v3.yaml"

cat > "$FLIGHTSTACK_DIR/.flightstack_rev1" <<META
revision=1
installed_at=$(date -Iseconds)
base_dir=$BASE_DIR
META

log "Basic installation verification"
check_replay_map
test -x "$FLIGHTSTACK_DIR/ardupilot/build/sitl/bin/arduplane"
test -x "$FLIGHTSTACK_DIR/jsbsim/build/src/JSBSim"
test -x "$FLIGHTSTACK_DIR/models/phoenix_v3/scripts/preinstall.sh"
test -x "$FLIGHTSTACK_DIR/phoenix_environment_adapter_v2/scripts/install.sh"
test -x "$FLIGHTSTACK_DIR/phoenix_controller/scripts/run_gui.sh"
test -f "$FLIGHTSTACK_DIR/data/internet_datasets.tar.gz"
test -f "$FLIGHTSTACK_DIR/data/region_v1.geojson"
test -f "$FLIGHTSTACK_DIR/data/phoenix_drone_v3.yaml"

cat <<DONE

============================================================
FlightStack installation completed successfully (revision 1).
Root: $FLIGHTSTACK_DIR

Run Layer-6 from the package root with:
  cd "$SCRIPT_DIR/.."
  python3 src/run_flightstack.py start --flightstack-dir "$FLIGHTSTACK_DIR"

Equivalent wrapper (kept for the working shell workflow):
  cd "$SCRIPT_DIR/.."
  ./scripts/run_layer6_stack.sh start --flightstack-dir "$FLIGHTSTACK_DIR"

Because FlightStack is installed inside the Layer-6 package, this shorter command is also valid:
  cd "$SCRIPT_DIR/.."
  python3 src/run_flightstack.py start

If no terminal window appears, run with detailed diagnostics:
  python3 src/run_flightstack.py start --debug --terminal gnome-terminal

The launcher uses the proven GNOME/X11 handoff mode and confirms each terminal
by an in-window PID handshake under runtime/pids/.
============================================================
DONE
