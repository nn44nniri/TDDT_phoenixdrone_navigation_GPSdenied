#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Only disposable staging/build directories are removed. Preserve the last good
# FMU until a complete replacement archive has been tested successfully.
rm -rf "$ROOT/build/cmake" "$ROOT/build/fmu_stage"
cmake -S "$ROOT" -B "$ROOT/build/cmake" -DCMAKE_BUILD_TYPE=Release
cmake --build "$ROOT/build/cmake" --config Release -j
STAGE="$ROOT/build/fmu_stage"
mkdir -p "$STAGE/binaries/linux64" "$STAGE/resources" "$ROOT/fmu"
cp "$ROOT/modelDescription.xml" "$STAGE/"
cp "$ROOT/build/cmake/MicroMet.so" "$STAGE/binaries/linux64/"
# Ancillary docs use the filenames actually shipped with this release.
# Runtime contract data remains mandatory; missing contracts fail the build.
find "$ROOT/docs" -maxdepth 1 -type f \( -name '*.md' -o -name '*.json' \) -exec cp '{}' "$STAGE/resources/" \;
cp -a "$ROOT/resources/tddt_layer2" "$STAGE/resources/"
TMP_FMU="$ROOT/fmu/.MicroMetTwin.build.$$.zip"
trap 'rm -f "$TMP_FMU"' EXIT
(cd "$STAGE" && zip -qr "$TMP_FMU" .)
unzip -tq "$TMP_FMU" >/dev/null
mv -f "$TMP_FMU" "$ROOT/fmu/MicroMetTwin.fmu"
echo "$ROOT/fmu/MicroMetTwin.fmu"
