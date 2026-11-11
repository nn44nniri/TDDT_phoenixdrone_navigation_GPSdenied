#!/usr/bin/env bash
set -euo pipefail

TWINS_LIBRARY=/home/shabgard/Desktop/DigitalTwin/TDDT_navigation/layer2/navigation_simulator/code/TDDT_Dynamic_NTwin/twins_layer2_v20260812_1755
MAPS=/home/shabgard/Desktop/DigitalTwin/TDDT_navigation/datasets/CRP_map/code/boulder_multilayer_map_builder_v20260805_1129/outputs/region_v1_map
REGION_FILE=./region/region_v1.geojson
SARG_OUTPUT=./generated/SARG

python3.13 -m sarg_builder.report_cli \
  --sarg ./generated/SARG \
  --maps "$MAPS" \
  --region-file "$REGION_FILE" \
  --reports ./generated/SARG/reports
