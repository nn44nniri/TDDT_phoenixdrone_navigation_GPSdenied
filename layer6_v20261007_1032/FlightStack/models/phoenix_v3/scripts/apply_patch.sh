#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 /path/to/ardupilot" >&2
  exit 2
fi

AP="$(cd "$1" && pwd)"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="$AP/libraries/SITL/SIM_JSBSim.cpp"
V3="$ROOT/patched_source/SIM_JSBSim.cpp"
V2="$ROOT/reference_sources/SIM_JSBSim.cpp.v2"
STOCK="$ROOT/reference_sources/SIM_JSBSim.cpp.stock"
PATCH_STOCK="$ROOT/patches/SIM_JSBSim_phoenix_v3.patch"
PATCH_V2="$ROOT/patches/SIM_JSBSim_phoenix_v3_v2_to_v3.patch"

[[ -f "$TARGET" ]] || { echo "ERROR: missing $TARGET" >&2; exit 3; }
for f in "$V3" "$V2" "$STOCK" "$PATCH_STOCK" "$PATCH_V2"; do
  [[ -f "$f" ]] || { echo "ERROR: missing packaged file $f" >&2; exit 4; }
done

same_file() { cmp -s "$1" "$2"; }
has_v3_markers() {
  grep -q 'strcmp(jsbsim_model, "phoenix_v3") == 0' "$TARGET" && \
  grep -q 'fcs/left-motor-cmd-norm' "$TARGET" && \
  grep -q 'Do not specify gamma for the zero-speed tailsitter initial condition' "$TARGET" && \
  grep -q '<ubody unit=\\"FT/SEC\\"> 0.0 </ubody>' "$TARGET"
}

if same_file "$TARGET" "$V3"; then
  echo "Phoenix v3 SIM_JSBSim integration already present; nothing to do."
  exit 0
fi

STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP="$TARGET.before_phoenix_v3.$STAMP"
cp -a "$TARGET" "$BACKUP"

if same_file "$TARGET" "$STOCK"; then
  echo "Detected stock SIM_JSBSim.cpp; installing tested Phoenix v3 source."
  install -m 0644 "$V3" "$TARGET"
elif same_file "$TARGET" "$V2" || [[ "$(sha256sum "$TARGET" | cut -d' ' -f1)" == "cb4f3d8d10f6f3d19fd884d101c959f0518dc080ca5dc3f7c2ee528ca0ea59bf" ]]; then
  echo "Detected Phoenix v2 SIM_JSBSim.cpp; upgrading to v3."
  install -m 0644 "$V3" "$TARGET"
else
  cd "$AP"
  if patch --dry-run -p1 < "$PATCH_V2" >/tmp/phoenix_v3_patch.$$ 2>&1; then
    echo "Detected v2-compatible source; applying v2->v3 patch."
    patch -p1 < "$PATCH_V2"
  elif patch --dry-run -p1 < "$PATCH_STOCK" >/tmp/phoenix_v3_patch.$$ 2>&1; then
    echo "Detected stock-compatible source; applying stock->v3 patch."
    patch -p1 < "$PATCH_STOCK"
  else
    cat /tmp/phoenix_v3_patch.$$ >&2 || true
    rm -f /tmp/phoenix_v3_patch.$$
    cp -a "$BACKUP" "$TARGET"
    echo "ERROR: SIM_JSBSim.cpp is neither the packaged stock/v2 source nor patch-compatible." >&2
    echo "No unknown ArduPilot source was overwritten. Backup: $BACKUP" >&2
    exit 5
  fi
  rm -f /tmp/phoenix_v3_patch.$$
fi

if ! has_v3_markers; then
  echo "ERROR: Phoenix v3 markers are missing after integration; restoring backup." >&2
  cp -a "$BACKUP" "$TARGET"
  exit 6
fi

echo "Phoenix v3 SIM_JSBSim integration installed successfully."
echo "Backup: $BACKUP"
