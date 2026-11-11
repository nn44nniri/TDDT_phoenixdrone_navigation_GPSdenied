#!/usr/bin/env bash
export TDDT_TWINS_ROOT="${TDDT_TWINS_ROOT:-/media/p1/DigitalTwin/TDDT_navigation/layer2/navigation_simulator/code/TDDT_Dynamic_NTwin/twins_layer2_v20260820_1233}"
export TDDT_CLIMATE_ROOT="${TDDT_CLIMATE_ROOT:-/media/p1/DigitalTwin/TDDT_navigation/layer2/climate/code/TDDT_Dynamic_NTwin/climate_twin_v20260824_0945}"

# External ISO unit/frame/time/semantic/covariance adapter used by Layer 3.1.
# This is the only environment variable used for the external conversion/adapter package.
export TDDT_UNIT_ADAPTER_ROOT="${TDDT_UNIT_ADAPTER_ROOT:-/media/p1/DigitalTwin/TDDT_navigation/adapter/code/TDOC_ISO_Universal_Unit_Conversion_Standard20260824_0914}"

export TDDT_BOULDER_ROOT="${TDDT_BOULDER_ROOT:-/media/p1/DigitalTwin/TDDT_navigation/datasets/CRP_map/code/boulder_multilayer_map_builder_v20260814_1215}"
export TDDT_SARG_PACKAGE_ROOT="${TDDT_SARG_PACKAGE_ROOT:-/media/p1/DigitalTwin/TDDT_navigation/datasets/SARG/code/SARG_v20260821_0945}"
export TDDT_LAYER3_ROOT="${TDDT_LAYER3_ROOT:-/media/p1/DigitalTwin/TDDT_navigation/layer3/code/layer3_v20260907_0608}"
export TDDT_LAYER4_ROOT="${TDDT_LAYER4_ROOT:-/media/p1/DigitalTwin/TDDT_navigation/layer4/code/layer4_v20260907_0608}"

# External-only ONLINE FMI bridge. OFFLINE_SE never imports PT_LIB or opens these FMUs.
# TDDT_PTLIB_ROOT points at pt_lib_v20260914_1308; EXT_FMU_SIM_ROOT points at
# ardupilot_JSBsim_v20260914_1308/FMU and must contain the two FMI 2.0 Co-Simulation FMUs.
export TDDT_PTLIB_ROOT="${TDDT_PTLIB_ROOT:-}"
export EXT_FMU_SIM_ROOT="${EXT_FMU_SIM_ROOT:-}"

# Future packages. Leave unset until their implementations exist.
export TDDT_LAYER5_ROOT="${TDDT_LAYER5_ROOT:-}"
export TDDT_LAYER6_ROOT="${TDDT_LAYER6_ROOT:-}"
export TDDT_LAYER7_ROOT="${TDDT_LAYER7_ROOT:-}"

export TDDT_SARG_ROOT="${TDDT_SARG_ROOT:-$TDDT_SARG_PACKAGE_ROOT/generated/SARG}"
export TDDT_MAP_ROOT="${TDDT_MAP_ROOT:-$TDDT_BOULDER_ROOT/outputs/region_v1_map}"
export TDDT_CRP_ROOT="${TDDT_CRP_ROOT:-$TDDT_MAP_ROOT/CRP}"

# TDOC itself always resolves to the current package directory; no TDDT_TDOC_ROOT is required.
export TDDT_ORCHESTRATOR_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Layer-5 file-backed SS/SETD knowledge persistence root. Override this on the edge device
# to place the segmented KStore on a dedicated persistent filesystem. If the shell still
# carries the default runtime path of an older layer5_vYYYYMMDD_HHMM release, rebase it.
case "${TDDT_KSTORE_ROOT:-}" in
  ""|*/layer5_v????????_????/runtime/kstore) export TDDT_KSTORE_ROOT="$TDDT_ORCHESTRATOR_ROOT/runtime/kstore" ;;
  *) export TDDT_KSTORE_ROOT="$TDDT_KSTORE_ROOT" ;;
esac

export PYTHONPATH="$TDDT_ORCHESTRATOR_ROOT/src:$TDDT_LAYER3_ROOT/src:$TDDT_LAYER4_ROOT/src:$TDDT_TWINS_ROOT/python:$TDDT_CLIMATE_ROOT/python:$TDDT_UNIT_ADAPTER_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
