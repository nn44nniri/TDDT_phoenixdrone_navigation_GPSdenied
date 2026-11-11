#!/usr/bin/env bash
set -euo pipefail

# Independent Layer-2 library. FMUs, Phoenix YAML and their Python access
# modules are resolved from this root; SARG does not duplicate their loaders.
TWINS_LIBRARY=/media/p1/DigitalTwin/TDDT_navigation/layer2/navigation_simulator/code/TDDT_Dynamic_NTwin/twins_layer2_v20260820_1232
MAPS=/media/p1/DigitalTwin/TDDT_navigation/datasets/CRP_map/code/boulder_multilayer_map_builder_v20260814_1214/outputs/region_v1_map
REGION_FILE=./region/region_v1.geojson
SARG_OUTPUT=./generated/SARG

python3.13 -m sarg_builder.cli \
  --twins-library "$TWINS_LIBRARY" \
  --maps "$MAPS" \
  --crp "$MAPS/CRP" \
  --region-file "$REGION_FILE" \
  --output "$SARG_OUTPUT"

python3.13 -m sarg_builder.report_cli \
  --sarg "$SARG_OUTPUT" \
  --maps "$MAPS" \
  --region-file "$REGION_FILE" \
  --reports "$SARG_OUTPUT/reports"
