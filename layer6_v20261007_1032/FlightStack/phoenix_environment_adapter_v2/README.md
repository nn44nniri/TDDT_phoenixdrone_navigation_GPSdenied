# Phoenix Environment Adapter v2 (package version 1.1.0)

Updated runtime adapter for the supplied Boulder ERA5/Copernicus data and Phoenix ArduPilot SITL + embedded JSBSim stack.

## Fix in this revision

The v1 runtime *sent* `SIM_WIND_SPD` and `SIM_WIND_DIR`, but did not verify that ArduPilot actually applied them. This revision:

- targets the exact ArduPilot system/component that emitted the heartbeat;
- sends `PARAM_SET` for `SIM_WIND_SPD` and `SIM_WIND_DIR`;
- waits for the corresponding `PARAM_VALUE` echo and verifies the value;
- retries failed parameter updates and terminates with a clear error if the MAVLink endpoint is not bidirectional;
- prints an explicit `mavlink_wind` status in each runtime JSON update.

## Install

```bash
cd FlightStack/phoenix_environment_adapter_v2
./scripts/install.sh
source .venv/bin/activate
```

## Prepare

```bash
phoenix-env prepare \
  --dataset-tar ../data/internet_datasets.tar.gz \
  --region ../data/region_v1.geojson \
  --vehicle ../data/phoenix_drone_v3.yaml \
  --time 2025-05-20T12:00:00Z \
  --output runtime
```

## Runtime

If TCP 5760 is available bidirectionally:

```bash
phoenix-env runtime \
  --dataset-tar ../data/internet_datasets.tar.gz \
  --region examples/region_v1.geojson \
  --time 2025-05-20T12:00:00Z \
  --cache runtime/dataset_cache \
  --mavlink tcp:127.0.0.1:5760 \
  --update-hz 1
```

If the Phoenix launcher provides the dedicated Environment Adapter MAVLink endpoint on UDP 14551, prefer:

```bash
phoenix-env runtime \
  --dataset-tar ../data/internet_datasets.tar.gz \
  --region examples/region_v1.geojson \
  --time 2025-05-20T12:00:00Z \
  --cache runtime/dataset_cache \
  --mavlink udpin:0.0.0.0:14551 \
  --update-hz 1
```

A healthy update includes, for example:

```json
"mavlink_wind": {"status":"applied","SIM_WIND_SPD":2.46,"SIM_WIND_DIR":263.9}
```

Then MAVProxy should report matching values:

```text
param show SIM_WIND_SPD
param show SIM_WIND_DIR
```

See `docs/CONNECTION.md` for the ArduPilot/JSBSim boundary.
