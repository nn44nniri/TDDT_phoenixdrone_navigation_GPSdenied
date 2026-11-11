#!/usr/bin/env bash
# TDOC authoritative runtime environment.
# Every external TDDT library/module root is declared here and nowhere else.
# YAML/config files may describe policy and internal configuration, but may not
# override or provide fallback addresses for external packages.

export TDDT_ORCHESTRATOR_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export TDDT_INTEGRATED_ROOT="$(cd "$TDDT_ORCHESTRATOR_ROOT/.." && pwd)"

# Immutable external package roots: environment.sh is the single source of truth.
export TDDT_TWINS_ROOT="$TDDT_INTEGRATED_ROOT/twins_layer2_v20260820_1233"
export TDDT_CLIMATE_ROOT="$TDDT_INTEGRATED_ROOT/climate_twin_v20260911_0922"
export TDDT_UNIT_ADAPTER_ROOT="$TDDT_INTEGRATED_ROOT/TDOC_ISO_Universal_Unit_Conversion_Standard20260824_0914"
export TDDT_BOULDER_ROOT="$TDDT_INTEGRATED_ROOT/boulder_multilayer_map_builder_v20260814_1215"
export TDDT_SARG_PACKAGE_ROOT="$TDDT_INTEGRATED_ROOT/SARG_v20260821_0945"
export TDDT_LAYER3_ROOT="$TDDT_INTEGRATED_ROOT/layer3_v20260911_1131"
export TDDT_LAYER4_ROOT="$TDDT_INTEGRATED_ROOT/layer4_v20260907_0608"
export TDDT_LAYER5_ROOT="$TDDT_INTEGRATED_ROOT/layer5_v20260911_1510"
# Native Layer6 owns GUI/MAVLink/ArduPilot/JSBSim; retired FMI roots must not leak from an older shell.
unset TDDT_PTLIB_ROOT EXT_FMU_SIM_ROOT
export TDDT_LAYER6_ROOT="$TDDT_INTEGRATED_ROOT/layer6_v20261007_1032"
export TDDT_LAYER7_ROOT="$TDDT_INTEGRATED_ROOT/layer7_v20260901_0921"

# Canonical generated/reference dataset roots.
export TDDT_INTERNET_DATASETS_ROOT="$TDDT_INTEGRATED_ROOT/internet_datasets"
export TDDT_BOULDER_INTERNET_DATASETS_ROOT="$TDDT_INTERNET_DATASETS_ROOT/boulder_multilayer_map_builder"
export TDDT_CLIMATE_INTERNET_DATASETS_ROOT="$TDDT_INTERNET_DATASETS_ROOT/climate_twin"
export TDDT_MAP_ROOT="$TDDT_BOULDER_ROOT/outputs/region_v1_map"
export TDDT_CRP_ROOT="$TDDT_MAP_ROOT/CRP"
export TDDT_SARG_ROOT="$TDDT_SARG_PACKAGE_ROOT/generated/SARG"
# Optional forcing file remains empty unless this authoritative file is edited.
export TDDT_CLIMATE_FORCING_FILE=""

# TDOC-owned mutable state roots.
export TDDT_LAYER5_KSTORE_ROOT="$TDDT_ORCHESTRATOR_ROOT/runtime/layer5/kstore"
export TDDT_KSTORE_ROOT="$TDDT_LAYER5_KSTORE_ROOT"
export TDDT_LAYER7_KSTORE_BASE="$TDDT_ORCHESTRATOR_ROOT/runtime/layer7/kstore"
export TDDT_LAYER7_STATE_ROOT="$TDDT_ORCHESTRATOR_ROOT/runtime/layer7/state"
export TDDT_REGION_FILE="$TDDT_ORCHESTRATOR_ROOT/region/region_v1.geojson"

# Ensure the integrated-project virtual environment exists. Creating it is
# intentionally limited to Python's venv bootstrap: package installation remains
# an explicit, requirements-pinned deployment step.
_TDDT_VENV_ROOT="$TDDT_INTEGRATED_ROOT/.venv"
if [[ ! -x "$_TDDT_VENV_ROOT/bin/python" || ! -f "$_TDDT_VENV_ROOT/pyvenv.cfg" ]]; then
    if ! command -v python3 >/dev/null 2>&1; then
        printf '[TDOC] ERROR: python3 is required to create %s\n' "$_TDDT_VENV_ROOT" >&2
        unset _TDDT_VENV_ROOT
        return 1 2>/dev/null || exit 1
    fi
    printf '[TDOC] Creating Python virtual environment: %s\n' "$_TDDT_VENV_ROOT" >&2
    if ! python3 -m venv "$_TDDT_VENV_ROOT"; then
        printf '[TDOC] ERROR: Failed to create venv. Check write permissions and install python3-venv if needed.\n' >&2
        unset _TDDT_VENV_ROOT
        return 1 2>/dev/null || exit 1
    fi
fi

# Use the TDOC-local interpreter by default. An explicitly supplied PYTHON
# remains authoritative for controlled deployments. Do not prepend the venv's
# bin directory to PATH; the documented source-tree CLI wrappers select PYTHON.
if [[ -z "${PYTHON:-}" ]]; then
    export PYTHON="$_TDDT_VENV_ROOT/bin/python"
fi
unset _TDDT_VENV_ROOT

# Source-tree launchers for the documented CLI sequence.  They remain valid
# before an editable package installation.  In particular, scripts/python
# exposes only the selected interpreter instead of prepending every console
# script from a potentially relocated venv ahead of system build tools.
case ":$PATH:" in
    *":$TDDT_ORCHESTRATOR_ROOT/scripts:"*) ;;
    *) export PATH="$TDDT_ORCHESTRATOR_ROOT/scripts:$PATH" ;;
esac

# Bundled source trees are authoritative and precede any installed packages.
_TDDT_SOURCE_PATH="$TDDT_ORCHESTRATOR_ROOT/src:$TDDT_LAYER3_ROOT/src:$TDDT_LAYER4_ROOT/src:$TDDT_LAYER5_ROOT/src:$TDDT_LAYER6_ROOT/src:$TDDT_LAYER7_ROOT/src:$TDDT_TWINS_ROOT/python:$TDDT_CLIMATE_ROOT/python:$TDDT_UNIT_ADAPTER_ROOT/src"
export PYTHONPATH="$_TDDT_SOURCE_PATH${PYTHONPATH:+:$PYTHONPATH}"
unset _TDDT_SOURCE_PATH
