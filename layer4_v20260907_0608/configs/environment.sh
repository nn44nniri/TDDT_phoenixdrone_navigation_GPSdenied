#!/usr/bin/env bash
# External packages are intentionally NOT vendored into Layer 4.
# Deployment roots for the current TDDT navigation installation.

export TDDT_TWINS_ROOT="/media/p1/DigitalTwin/TDDT_navigation/layer2/navigation_simulator/code/TDDT_Dynamic_NTwin/twins_layer2_v20260820_1233"
export TDDT_CLIMATE_ROOT="/media/p1/DigitalTwin/TDDT_navigation/layer2/climate/code/TDDT_Dynamic_NTwin/climate_twin_v20260824_0945"
export TDDT_TDOC_ROOT="/media/p1/DigitalTwin/TDDT_navigation/adapter/code/TDOC_ISO_Universal_Unit_Conversion_Standard20260824_0914"
export TDDT_BOULDER_ROOT="/media/p1/DigitalTwin/TDDT_navigation/datasets/CRP_map/code/boulder_multilayer_map_builder_v20260814_1215"
export TDDT_SARG_PACKAGE_ROOT="/media/p1/DigitalTwin/TDDT_navigation/datasets/SARG/code/SARG_v20260821_0945"
export TDDT_LAYER3_ROOT="/media/p1/DigitalTwin/TDDT_navigation/layer3/code/layer3_v20260907_0608"

export TDDT_SARG_ROOT="${TDDT_SARG_ROOT:-$TDDT_SARG_PACKAGE_ROOT/generated/SARG}"
export TDDT_CRP_ROOT="${TDDT_CRP_ROOT:-$TDDT_BOULDER_ROOT/outputs/region_v1_map/CRP}"
export TDDT_MAP_ROOT="${TDDT_MAP_ROOT:-$TDDT_BOULDER_ROOT/outputs/region_v1_map}"
export PYTHONPATH="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src:${TDDT_LAYER3_ROOT}/src:${TDDT_TWINS_ROOT}/python${PYTHONPATH:+:$PYTHONPATH}"
