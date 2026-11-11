#!/usr/bin/env bash
set -euo pipefail
#python build_regional_map.py \
#  --output outputs/boulder_9km2 \
#  --center-lat 40.015 \
#  --center-lon -105.292 \
#  --size-m 3000 \
#  --resolution-m 10 \
#  --start-date 2020-05-20 \
#  --end-date 2020-05-20

# After the first successful online acquisition, repeat the complete build offline:
# python3 build_regional_map.py \
#   --output outputs/boulder_9km2 \
#   --center-lat 40.015 --center-lon -105.292 \
#   --size-m 3000 --resolution-m 10 \
#   --start-date 2020-05-20 --end-date 2025-05-20 \
#   --offline


python3 build_regional_map.py \
  --region-file region/region_v1.geojson \
  --start-date 2010-05-20 \
  --end-date 2025-05-20


python3 map_mosaic_query.py point \
  --region-file region/region_v1.geojson \
  --hour 14

python3 map_mosaic_query.py mosaic3d \
  --region-file region/region_v1.geojson \
  --hour 14

python3 map_mosaic_query.py route3d \
  --region-file region/region_v1.geojson \
  --mosaic-count 3 \
  --hour 14
  
python3 build_crp_dataset.py \
  --region-file region/region_v1.geojson \
  --clusters 32 \
  --plot3d-clusters