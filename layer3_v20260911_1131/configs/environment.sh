#!/usr/bin/env bash
# TDDT Dynamic N-Twin Layer 3 - external dependency environment
#
# Load these variables into the CURRENT terminal before running Layer 3:
#   source configs/environment.sh
# or:
#   . configs/environment.sh
#
# IMPORTANT: executing `./configs/environment.sh` starts a child shell and does
# not persist exports in the parent terminal. Use `source` or `.` as above.

export TDDT_TWINS_ROOT="/media/p1/DigitalTwin/TDDT_navigation/layer2/navigation_simulator/code/TDDT_Dynamic_NTwin/twins_layer2_v20260820_1233"
export TDDT_CLIMATE_ROOT="/media/p1/DigitalTwin/TDDT_navigation/layer2/climate/code/TDDT_Dynamic_NTwin/climate_twin_v20260911_0922"
export TDDT_TDOC_ROOT="/media/p1/DigitalTwin/TDDT_navigation/adapter/code/TDOC_ISO_Universal_Unit_Conversion_Standard20260824_0914"
export TDDT_BOULDER_ROOT="/media/p1/DigitalTwin/TDDT_navigation/datasets/CRP_map/code/boulder_multilayer_map_builder_v20260814_1215"
export TDDT_SARG_PACKAGE_ROOT="/media/p1/DigitalTwin/TDDT_navigation/datasets/SARG/code/SARG_v20260821_0945"

# Derived runtime locations used by configuration_boulder.yaml.
export TDDT_MAP_ROOT="${TDDT_BOULDER_ROOT}/outputs/region_v1_map"
export TDDT_CRP_ROOT="${TDDT_MAP_ROOT}/CRP"
export TDDT_SARG_ROOT="${TDDT_SARG_PACKAGE_ROOT}/generated/SARG"
