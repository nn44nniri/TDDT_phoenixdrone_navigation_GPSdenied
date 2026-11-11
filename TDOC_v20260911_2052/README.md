# Native Layer6 migration — 2026-10-08

The active execution backend is `layer6_v20261007_1032`: TDOC invokes
`Layer6Client.submit()` -> Layer6 service -> GUI -> MAVLink -> ArduPilot SITL ->
JSBSim. `TDDT_LAYER6_ROOT` is the only execution-package address. PT_LIB and the
ArduPilotController/JSBSimPhoenix FMUs are retired; historical notes below describe
older releases and are not current deployment instructions.

Source `configs/environment.sh` before using the CLI. The requested command stays:

```bash
tddt-tdoc --config configs/tdoc.yaml reset --refresh-external --build-fmus --build-fmus-refresh
```

It builds only `MESA_NAV.fmu`, `WingMPET.fmu`, `MicroMetTwin.fmu`. Cleanup preserves
those final artifacts and removes only the Twins/Climate reproducible build caches.
It never builds or cleans the native FlightStack. Preparation checks Layer6 source
and FlightStack assets without starting GUI, MAVLink, or flight.

See `docs/NATIVE_LAYER6_MIGRATION.md` for review and validation boundaries.

# TDOC_v20260911_2052

Python implementation of the **Trans-Domain Orchestration Core (TDOC)** for the Dynamic N-Twin TDDT architecture defined by `TDDT_Dynamic_NTwin_TDOC_details_FA_v4.md`.

## Invocation ownership

Architectural arrows are data/causal dependencies, not software ownership. Every numbered-layer transition is owned by TDOC:

```text
Ready -> TDOC Trigger -> Invoke(Layer i) -> VersionedRoute -> Ready(next)
```

The currently implemented runtime branches are:

```text
Common forward path:
Device/Input -> TDOC -> Layer 2 -> TDOC -> Layer 3 -> TDOC -> Layer 4 -> TDOC -> Layer 5

OFFLINE_SE:
Layer 5 -> TDOC -> Layer 2 -> ...   # Layer 6 is not in this loop

ONLINE_JSBSIM:
Layer 5 -> TDOC -> Layer 6 -> PT_LIB <-> ArduPilot <-> JSBSim -> TDOC -> Layer 7
Layer 7 -> TDOC VersionedRoute -> {Layer 2, Layer 3.1, Layer 3.2, Layer 3.3}
```

Layer 7 is the external `layer7_v20260901_0921` TDOC-managed feedback/adaptation side module.

## Layer 6 online-only boundary

Layer 6 participates **only** in `ONLINE_JSBSIM`. It is the execution/simulation environment reached after a valid Layer-5 online decision.

### Offline training (`OFFLINE_SE`)

```text
L5 -> TDOC -> L2 -> ...
```

Layer 6 is explicitly skipped. TDOC derives a versioned actuator-feedback packet from the valid Layer-5 result and re-invokes Layer 2. This closes the offline training loop while preserving `InvocationOwner=TDOC/RuntimeScheduler`.

### Online execution (`ONLINE_JSBSIM`)

```text
L5 -> TDOC -> L6
                 |
                 v
       external PT_LIB <-> ArduPilot <-> JSBSim
                 |
                 v
               TDOC -> Layer 7
```

PT_LIB is selected **only** through `TDDT_PTLIB_ROOT`, remains external-only, and is never vendored. TDOC reuses the `PTLibJSBSimAdapter` supplied by Layer 5, invokes Layer 7 through `TDDT_LAYER7_ROOT`, and owns all versioned feedback routes and safe-commit/rollback handshakes.

See `docs/LAYER6_ONLINE_ORCHESTRATION.md`.

## Current Layer status

- **Layer 1:** physical/event ingress boundary.
- **Layer 2:** external `MESA_NAV`, `WingMPET`, and `MicroMetTwin`, invoked by TDOC.
- **Layer 3:** external `Layer3Engine`; TDOC never uses `Layer3FMURuntime` as an invocation owner.
- **Layer 4:** external prediction/what-if/optimization engine; legacy reverse-ownership verification is disabled; TDOC owns replacement full-fidelity verification, which fails closed unless the Phoenix action bridge is evidence-validated.
- **Layer 5:** external `layer5_v20260911_1510`, loaded only from `TDDT_LAYER5_ROOT` through `Layer5Config + Layer5Engine`.
- **Layer 6:** implemented ONLINE-only TDOC execution boundary (`ONLINE_JSBSIM` only); absent from `OFFLINE_SE`.
- **Layer 7:** external `layer7_v20260901_0921`, loaded only from `TDDT_LAYER7_ROOT`; TDOC-owned side module, no direct actuation.

## External configuration

`configs/environment.sh` contains deployment roots. Important current additions are:

```bash
TDDT_LAYER5_ROOT=.../layer5_v20260911_1510
TDDT_PTLIB_ROOT=.../pt_lib_v20260914_1308   # set only for ONLINE_JSBSIM
TDDT_LAYER7_ROOT=.../layer7_v20260901_0921
```

`TDDT_PTLIB_ROOT` is intentionally empty by default. An online cycle fails closed if PT_LIB is not explicitly available.

No Layer package—including Layer 7—FMU, PT_LIB source, map raster, CRP/SARG dataset, or ISO conversion package is copied into TDOC. Mutable Layer-7 state is redirected to `runtime/layer7/state`.

## Mutable Layer-5 state

Layer 5 remains external and read-only from TDOC's packaging perspective. Unless deployment provides its own `TDDT_KSTORE_ROOT`, TDOC directs Layer-5 KStore state to:

```text
runtime/layer5/kstore
```

so the external `TDDT_LAYER5_ROOT` is not used as a mutable runtime store.

All persistent Layer-5 KStore JSON/JSONL data is gzip-compressed at rest. Per-cycle Layer-5 evidence is independently archived under `runtime/layer5/records/chunks/*.jsonl.gz`, including `HOLD` cycles, so a missing actuator command can never be confused with missing Layer-5 evidence. Layer-3, Layer-4 and Layer-7 native JSON/JSONL knowledge stores use the same transient-workspace/gzip-at-rest compatibility pattern. TDOC append histories (cycles, provenance, knowledge ledger and Layer2–7 records) are bounded gzip chunks rather than unbounded single files.

## Layer 7 and closed online feedback loop

TDOC validates the exact external package name `layer7_v20260901_0921`, API version `0.3.0`, contract `tdoc-layer7-v2`, `trigger_owner=TDOC`, `side_module=true`, and `direct_actuation=false`.

The online feedback transaction is:

```text
L6 ExecutionEvidence -> TDOC -> L7 -> TDOC VersionedRoute -> {L2,L3.1,L3.2,L3.3}
```

Layer-7 transient corrections are versioned and made available to the next cycle; supported `DeltaK` corrections are also applied as Layer-3 transient coupling deltas. Persistent/structural candidates remain fail-closed unless candidate-bound shadow/stability evidence, governance, safe synchronization boundary, and TDOC Safe Commit all pass. `ONLINE_JSBSIM` is classified as simulation evidence and is never silently promoted to FIELD evidence.

## ISO adapter

The external ISO/unit/frame/time/semantic/covariance package remains selected by `TDDT_UNIT_ADAPTER_ROOT`. TDOC validates/binds it; Layer 3.1 performs the actual normalization. `TDDT_TDOC_ROOT` is not used.

## Quick start

```bash
source configs/environment.sh
python -m pip install -e .
./run_preflight.sh
```

Offline cycle:

```bash
tddt-tdoc --config configs/tdoc.yaml run-cycle \
  --request examples/runtime_request.json \
  --output runtime/offline_cycle.json
```

Set `"learning_mode": "ONLINE_JSBSIM"` in the request and configure `TDDT_PTLIB_ROOT` for online execution.

See `HOW_USAGE.md` for the complete contract.


## Current Full-Fidelity safety boundary

The shipped `layer5_v20260911_1510` explicitly marks the Layer4 5D→Phoenix 4D mapping and local mixer/servo/ESC evidence as unvalidated. TDOC therefore does **not** guess this bridge. Layer-4 candidates remain unverified with `phoenix_action_bridge_unvalidated` until either explicit bounded `logical_actions` plus a validated physical mixer are supplied or the external Layer-5 mapping/mixer evidence is updated. Online knowledge can still reach Layer 7 through the non-actuating Layer-6 observation-only path; that path is reported as `DEGRADED`/HOLD-safe and never as operational actuation.

## Central preparation, online route inspection, and stateful offline mission training

All preparation is centrally owned by TDOC through `scripts/prepare_all.sh`. The preparation pipeline is package-first: all external package build/source readiness is established before generated map/CRP/SARG/climate data acquisition begins. The visible 23-stage order is:

1. install the current TDOC package;
2. validate every external package root and mandatory builder path;
3. build/package MESA-NAV + WingMPET FMUs;
4. build/package the Climate Twin FMU;
5. prepare the ISO adapter source package;
6. prepare the Boulder map/CRP builder package;
7. prepare the SARG package;
8. run the official Layer-2 TEP builder `PYTHONPATH=python python3 python/build_tddt_layer2_tep.py`, then validate `DomainTwin` plus `tep.csv`, `manifest.json`, `heldout_validation.json`, `quality_report.json`, and `VERSION` for both MESA-NAV and WingMPET;
9–13. prepare Layer 3, Layer 4, Layer 5, PT_LIB, and Layer 7 source/API packages in dependency order;
14. validate the authoritative mission region/start/destination;
15–16. acquire/reuse terrain/OSM/historical-climate/magnetic sources and build canonical CRP;
17. build SARG from Twins + Map + CRP + the rebuilt TEPs;
18. download the current horizon-aware Open-Meteo forcing;
19. invoke the official map-package `route3d` CLI;
20–23. final ISO, Layer 3, Layer 4/5, and Layer 7 preflights.

Long external commands stream their output and emit a periodic `still running (...)` heartbeat, so a several-minute terrain/download/build stage is visible rather than appearing hung.

```bash
source configs/environment.sh
./scripts/prepare_all.sh
```

When artifacts are already prepared, the compatibility mode remains available and performs no network download or rebuild:

```bash
./scripts/prepare_all.sh --no-build
```

For an air-gapped rebuild from persisted map sources use `--no-download` rather than `--no-build`.

Climate forcing is horizon-aware. The default `./scripts/prepare_all.sh` uses `--climate-hours 24`, creates `TDDT_CLIMATE_ROOT/climate_dataset/openmeteo_24h.csv`, and records it in the generated `tdoc_active_forcing.json` pointer. A custom horizon is selected end-to-end, for example:

```bash
./scripts/prepare_all.sh --climate-hours 72
```

which creates `openmeteo_72h.csv` and makes it the active input for Layer2 and the offline mission driver. `TDDT_CLIMATE_FORCING_FILE` remains an explicit deployment override and is fail-closed if it points to a missing file.

### Offline climate-time alignment (v20260907_1031)

Offline mission elapsed time is mapped into the active Climate forcing window. If `Layer3.time.mission_epoch_utc` lies inside that window, the Layer-3 epoch remains authoritative. If it lies outside (for example a static 2026-08-24 Layer-3 epoch with a freshly prepared 2026-09-07 forcing file), TDOC rebases only the Offline climate-query epoch to the forcing start and records `context.offline_climate_time_alignment`. Offline never clamps at the forcing end; if elapsed mission time exceeds the available forcing horizon, execution remains fail-closed and the forcing must be refreshed/extended. Online forcing semantics are unchanged.


### Stateful offline mission driver

`run_training_workflows.sh` now defaults to an origin-to-destination mission driver rather than six independent copies of `runtime_request.json`:

```bash
./scripts/run_training_workflows.sh --offline
```

The driver selects an energy-feasible SARG route as a **reference/prior only**. The realized training state is never copied from the next SARG row: TDOC queries MicroMetTwin at the realized location/time, applies its 3-D wind to external WingMPET operation 7, applies the selected Layer-4 candidate only through the documented high-level `delta_airspeed_m_s` and `delta_yaw_rate_rad_s` advisory channels, and propagates the next simulated state. A same-air-command/no-wind WingMPET counterfactual is propagated in parallel so wind influence is measurable rather than inferred visually.

The mission reference is wrapped with explicit `on_ground → vertical_takeoff → hover → transition_forward → climb/cruise/turn/approach → transition_back → vertical_landing → touchdown` phases. WingMPET operations 27/28/30/31 provide a 6-DOF shadow, motor dynamics, VTOL envelope and energy/SOC evidence using deterministic simulation-reference actuator states; these values are explicitly non-operational. If Layer 5 later supplies a verified `FINAL=true` physical control and Layer 2 confirms `control_effect_applied=true`, the cycle is marked `L5_CLOSED_LOOP`. With the current Phoenix mapping evidence Layer 5 remains fail-closed/HOLD, while the offline **simulation** trajectory still advances through WingMPET rather than teacher-forced SARG replay.

Mission reports are written to `reports/offline/mission/`:

- `MISSION_MANAGEMENT_DASHBOARD.html` — route, wind, energy, Layer-4 costs, phase, Fast/Meso/Slow, MPC/safety state, route-turn/obstacle context and Layer-4-vs-reference deviation; it now begins with an **External Layer → TDOC Return Pulse / Error-Behavior Panel** that places every Layer2/3/4/5 return pulse on its mission cycle and shows source, transport status, external-call elapsed time, deadline and payload behavior; temporal axes explicitly mean **flight elapsed seconds**, and total origin-to-destination flight duration is shown in seconds and `HH:MM:SS`;
- `MISSION_ROUTE_3D.html` — external 3-D terrain raster + SARG/VTOL reference + realized wind-aware WingMPET trajectory + same-air-command/no-wind counterfactual + sampled MicroMet 3-D wind cones + every Layer-4/5 decision tick with time/geography/UAV/sensor/actuator/climate details;
- `DECISION_POINT_DETAILS.json.gz` — complete compressed per-point details backing the 3-D report;
- `EXTERNAL_LAYER_RETURN_PULSES.csv` / `.json.gz` — complete machine-readable Layer2/3/4/5 → TDOC pulse history, including cycle, source, transport status, behavior classification, call elapsed time, deadline and event timestamp;
- `LAYER4_OPTIMIZATION_2D.html` — detailed objective/candidate panels; `LAYER4_OPTIMIZATION_TIMELINE.html` — readable per-cycle optimization timeline; `LAYER4_OPTIMIZATION_3D.html` is retained as a compatibility filename but now contains the same 2-D timeline instead of the former dense 3-D point cloud;
- `SARG_CRP_TREEMAP.html` — Plotly Treemap of the SARG/CRP hierarchy and assignment relationship;
- `DECISION_ACCURACY_EFFICIENCY_REPORT.html` — route/forecast accuracy, energy efficiency, Layer-4 feasibility/verification, Layer-5 decision and loop metrics;
- `MISSION_TIMELINE.csv` — one row per mission cycle;
- `LAYER4_CANDIDATES.csv` — all Layer-4 optimization candidates per cycle;
- `ACTUATOR_HISTORY.csv` — Layer-5 pilot-domain/physical actuator values only when actually available;
- `CLIMATE_HISTORY.csv`, `ENERGY_HISTORY.csv`, `PHASE_HISTORY.csv`, `ROUTE_HISTORY.csv` — specialized histories;
- `MISSION_SUMMARY.json` — selected SARG reference, realized/wind-counterfactual metrics, energy budget, ground-to-touchdown status, loop counts and state-source provenance;
- `LIBRARY_PARTICIPATION.html` / `reports/validation/LIBRARY_PARTICIPATION_AUDIT.json` — role-aware evidence distinguishing configured, invoked, causal, safety/gating and online-only library participation.

A short validation run can set `--offline-max-cycles N` as a hard safety upper bound. With the default `0`, TDOC uses the wrapped reference length plus the terminal-correction budget, but successful touchdown terminates immediately. The older fixed-cycle harness remains available only as `--offline-driver legacy`.

Online validation remains separate and continues to require the external PT_LIB/Layer-6/Layer-7 path.

### Closed-loop truthfulness rule

A cycle is labelled `L5_CLOSED_LOOP` only when Layer 5 supplies a validated physical control action, TDOC routes it back to Layer 2, **and Layer 2 explicitly reports `control_effect_applied=true`**. Feedback admission alone is not treated as physical state propagation. When that evidence is absent, offline training is labelled `L4_ADVISORY_WINGMPET_SIMULATION`: SARG remains reference-only, MicroMet wind and Layer-4 high-level advisory influence WingMPET state propagation, and `teacher_forced=false`. Layer-4 abstract qLPV actions are never relabelled as Phoenix actuators.

Obstacle context is sampled read-only from external `TDDT_MAP_ROOT/terrain_obstacles.tif`; the report records terrain/building/natural/artificial context, route turn angle and Layer-4 next-step deviation. `turn_near_obstacle_context` is a correlation/context flag and does **not** claim obstacle causality.


## Runtime gzip/chunk storage audit

Persistent knowledge/history is gzip-compressed at rest and high-volume histories are stored as bounded `*.jsonl.gz` chunks. Layer 5 is visible under `runtime/layer5/` even when the controller is HOLD; its per-cycle decision envelopes are in `runtime/layer5/records/chunks/` and its external-compatible KStore mirror is under `runtime/layer5/kstore/`.

Read compressed JSON/JSONL transparently with `tddt-tdoc read-storage --path <file> [--jsonl]`. Audit the runtime layout with:

```bash
python scripts/audit_runtime_storage.py
```

The audit writes `reports/validation/STORAGE_COMPRESSION_AUDIT.json` and fails if persistent knowledge is left as plain JSON/JSONL, if chunk storage is missing, or if Layer-5 runtime records are absent.

Per-layer knowledge views are also maintained under `runtime/layer2/knowledge/` through `runtime/layer7/knowledge/`. This makes Layer-5 knowledge independently discoverable at `runtime/layer5/knowledge/chunks/*.jsonl.gz`; rebuild these views from the authoritative global ledger with `python scripts/rebuild_layer_knowledge_views.py`.

Per-layer runtime histories are compact views rather than duplicate full snapshots: `runtime/layerN/records/chunks/*.jsonl.gz` contains a payload hash, cycle reference and layer-specific summary; Layer 5/6/7 retain their small operational payload inline. The authoritative full cycle remains at `runtime/cycles/<transaction>.json.gz`. Rebuild compact views with `python scripts/rebuild_layer_runtime_views.py`. This avoids duplicating large Layer-4 candidate payloads while keeping them available from the referenced cycle and mission optimization reports.

External packages that require a fixed native JSONL filename (for example Layer-3 SS/SETD and Layer-4 native provenance/candidate streams) see that exact file only inside a transient workspace. Persistent storage is split into bounded gzip chunks under `<native-name>.jsonl.chunks/chunks/*.jsonl.gz` with `manifest.json.gz`; TDOC reconstructs the exact byte stream before external invocation. JSON singleton files remain atomic `.json.gz` snapshots. This preserves the external API contract while preventing large append histories from becoming one monolithic file.

Mission diagnostics also include `WIND_CAUSALITY_REPORT.html` / `WIND_CAUSALITY_METRICS.json` and `VTOL_PHASE_DASHBOARD.html` / `VTOL_PHASE_METRICS.json`; regenerate them with `python scripts/postprocess_mission_diagnostics.py`.

## Reset generated data and knowledge

Use the TDOC CLI to return a prepared/trained deployment to a clean state **without running `prepare` or `refresh`**:

```bash
source configs/environment.sh
tddt-tdoc --config configs/tdoc.yaml reset
# equivalent from source:
PYTHONPATH=src python -m tddt_tdoc.cli --config configs/tdoc.yaml reset
```

`reset` deletes TDOC-owned `runtime/` (cycles, Layer2–7 records/knowledge, KStores, provenance, feedback and mutable state) and generated `reports/`. When external roots are configured, it additionally removes only provably-contained preparation-generated datasets: Boulder `TDDT_MAP_ROOT`/CRP, `outputs/tdoc_route_cli_work`, `TDDT_SARG_ROOT`, exact Layer-2 `MESA_NAV/tep` and `WingMPET/tep` directories, every TDOC-managed `climate_dataset/openmeteo_<N>h.csv`, and `climate_dataset/tdoc_active_forcing.json`. It **does not** delete external package source roots, surrounding Layer-2 resource contracts, Twin/Climate library FMUs, TDOC source/configuration/formalism, unrelated climate dataset files, or `region/region_v1.geojson`.

Plain `reset` remains purge-only. To purge and then rebuild the external generated datasets through the official central preparation pipeline, use `reset --refresh-external`; for example `reset --refresh-external --climate-hours 72`. Add `--build-fmus` to rebuild the three Twin FMUs before any internet-dependent dataset acquisition; add `--build-fmus-refresh` to additionally remove only reproducible build caches after successful builds while preserving final FMUs/source/config/scripts. Reset now prints each TDOC/external target before deletion, the deletion result/bytes, and then relays every preparation stage live. Long preparation stages emit heartbeat messages. `--refresh-timeout-s N` optionally bounds the whole refresh; `0` means no timeout. The refresh mode is fail-closed and returns failure if the external preparation pipeline cannot complete.

Use `reset --dry-run` to inspect targets without deleting anything, or `reset --no-external` to restrict deletion to TDOC-owned runtime/reports. `--refresh-external` is intentionally incompatible with the semantic intent of `--no-external`; refresh requires the external purge path. The executed reset writes `reports/reset/RESET_REPORT.json` and records refresh command/result when refresh was requested.


## Mission reporting robustness update (2026-09-03)

`MISSION_MANAGEMENT_DASHBOARD.html` now carries both an expanded core 2-D dashboard and separate 2-D time-series figures for every scalar detail section exposed by the 3-D mission hover. `MISSION_ROUTE_3D.html` and the management dashboard both include a Cycle Detail Explorer so a single cycle can be inspected without rendering hundreds of rows at once. Default zero/false/null values are hidden in the explorer for readability but remain available through explicit toggles and in `DECISION_POINT_DETAILS.json.gz`; flattened hover-backed scalars are also exported to `MISSION_DETAIL_TIMESERIES.csv`.

The offline mission progress denominator is now described as a **safety cycle budget**, not a required cycle count. A mission that reaches valid touchdown at cycle 230 correctly completes at cycle 230 even when the safety budget is 320; no post-touchdown samples are fabricated. Layer-4 objective plots retain the full mission time axis even when no feasible candidate exists late in the mission, and missing objective values remain gaps rather than zeros.

The known external Climate `terrain_truth.py` masked-element cast warning is filtered only at the TDOC Layer-2 adapter boundary and recorded in packet provenance. The external Climate package is not modified, and unrelated warnings are not suppressed.

## MPC participation and explicit offline→online training handoff

TDOC now records **MPC computational participation separately from authorization and actuation**. Layer 4 participation is based on the external Layer-4 conformance evidence (`C_optimizer`, `C_tube`, `C_robust`) and candidate/feasibility/verification counts. Layer 5 participation is based on the external Layer-5 `only_first_mpc_move` provenance, while `FINAL`, control presence, TDOC feedback acceptance, and executed Layer-6 actuation remain separate evidence fields. Mission reports persist `MPC_PARTICIPATION.json` and include a dedicated participation panel in `MISSION_MANAGEMENT_DASHBOARD.html`.

The formal Layer-5 v4 offline→online requirement is now enforced for explicit online training:

```text
OFFLINE_SE
  -> Layer5 SS-KStore
  -> Layer5 finalize_offline_episode()
  -> verified committed SETD-KStore
  -> TDOC version/hash/package-bound handoff
  -> ONLINE_JSBSIM
  -> Layer5 Load(valid SETD)
  -> TDOC Layer6
  -> external PT_LIB <-> ArduPilot <-> JSBSim
  -> ExecutionEvidence
  -> Layer7
  -> TDOC VersionedRoute
```

TDOC does not fabricate SETD knowledge and does not replace the external Layer-5 KStore. If the offline episode cannot be finalized to a verified SETD package, explicit online training is `NOT_READY/HELD` before Layer 6.

Primary CLI:

```bash
tddt-tdoc --config configs/tdoc.yaml train-online --online-cycles 10
```

This performs a new offline episode first, finalizes its SETD, and then starts the online continuation. To reuse an already finalized offline handoff:

```bash
tddt-tdoc --config configs/tdoc.yaml train-online \
  --reuse-existing-offline-knowledge \
  --online-cycles 10
```

The underlying workflow CLI is also explicit:

```bash
./scripts/run_training_workflows.sh --online-training --online-cycles 10
```

Online training requires the same verified SETD package identity recorded in `runtime/knowledge/handoff/offline_latest.json.gz`. `--online-cycles N` means at most **N consecutive ONLINE_JSBSIM iterations** in one continuous invocation; it is not a mission count. For complete repeated missions use:

```bash
tddt-tdoc --config configs/tdoc.yaml train-online \
  --online-episodes 100 \
  --online-max-cycles-per-episode 320
```

`--online-missions` is an alias of `--online-episodes`. Each episode opens a fresh TDOC-owned Layer-6 session boundary, PT_LIB remains the only owner of ArduPilot/JSBSim prepare/connect, the first live position must be within the configured origin tolerance, and the episode ends only on destination/touchdown evidence or the per-episode safety cap.

PT_LIB and Layer-7 participation are reported in `reports/online/ONLINE_JSBSIM_SUMMARY.json` and `ONLINE_ORCHESTRATION_DIAGNOSTICS.json`. A whole run is successful only when every requested online cycle has live `Layer6/PT_LIB` evidence and Layer7 `OK`; one successful cycle can no longer make a 100-cycle run `contract_ok=true`. Validation fixtures have their own `validation_contract_ok` and never count as live training.


## Online training orchestration correction — v20260904_1028

- `train-online` auto-reuses `runtime/knowledge/handoff/offline_latest.json.gz` when it already exists; use `--run-offline-first` to force a fresh offline episode or `--reuse-existing-offline-knowledge` to require reuse.
- TDOC-owned mutable KStore/state paths from an older `TDOC_vYYYYMMDD_HHMM` shell are rebased to the current release when `configs/environment.sh` is sourced; genuine custom paths are preserved.
- `reports/online/ONLINE_JSBSIM_SUMMARY.json` and `ONLINE_ORCHESTRATION_DIAGNOSTICS.json` are created even when online training is stopped before Layer6.
- Docker `Up` status or Compose `sleep infinity` is not accepted as execution readiness. Layer6 requires PT_LIB `prepare`, `connect`, and non-empty `io_snapshot` evidence and rejects explicit negative readiness. TDOC does not directly start Docker processes.
- Required Layer5 companion for that historical release: `layer5_v20260904_1028` (`2026.9.4.1028`). Its valid fail-closed OFFLINE_SE HOLD decisions are recorded as non-actuating knowledge so a HOLD-dominated single episode can finalize SS/SETD.
- Layer4 remained `layer4_v20260907_0608`; its unvalidated Phoenix action bridge remained fail-closed. Layer7 remained `layer7_v20260901_0921`; subsequent live runs reached Layer7 and confirmed evidence-gated `OK/NO_CHANGE` behavior rather than an internal Layer7 orchestration defect.

## Online episode/session/reporting correction — v20260904_1216

- `./scripts/run_training_workflows.sh --offline` is the preferred offline-only command. The old `--skip-online` spelling remains as a deprecated compatibility alias.
- `--online-cycles` remains the continuous iteration cap. `--online-episodes` / `--online-missions` adds complete origin-to-destination repetitions with `--online-max-cycles-per-episode`, origin verification, destination tolerance and touchdown-altitude evidence.
- Layer6 keeps one PT_LIB adapter session across normal cycles and performs one bounded session recovery only after explicit negative runtime evidence (`ready=false`, disconnected, timeout, etc.). Metadata-only READY snapshots remain rejected and are never converted into runtime evidence.
- Online diagnostics read `downstream_layers[6].output.execution_evidence`, so real PT_LIB invocation is no longer missed by the reporter. Strict contract coverage is per-cycle rather than `any(...)`.
- Online visualization is generated from all `workflow-online_jsbsim-*` cycles. `tdoc_route_3d.html` prefers live Layer6/PT_LIB geographic positions, falls back to MESA-NAV only when live geographic evidence is absent, and overlays the complete offline/SARG mission reference as reference-only context.
- The old dense `LAYER4_OPTIMIZATION_3D.html` visualization has been replaced in-place by a four-panel 2-D optimization decision timeline; the canonical new filename is `LAYER4_OPTIMIZATION_TIMELINE.html`.
- Training preflight is fail-fast for every required execution mode; a wrong external package/version cannot consume a full mission and fail only at finalization.

## 2026-09-04 online orchestration hardening (v20260904_2120)

This release preserves the Layer-4 and Layer-7 contracts and updates the TDOC/Layer-5/PT_LIB boundary after analysis of a real 100-cycle ONLINE_JSBSIM run.

- **Episode-scoped PT_LIB session:** Layer 6 opens PT_LIB `prepare/connect` once and reuses the session across healthy cycles. A new session is opened only at an episode boundary or after explicit negative runtime evidence (`ready=false`, `DISCONNECTED`, `TIMEOUT`, etc.). TDOC still never owns Docker commands.
- **Fresh non-interactive telemetry/truth (historical v2120 behavior):** `pt_lib_v20260904_2120` bounded-polled the MAVLink consumer at canonical snapshots and bound lifecycle readiness/truth hooks for non-interactive callers. The v20260905_1056 update below supersedes this with a persistent background receiver/truth stream for the edge hot path.
- **Online climate time alignment:** ONLINE_JSBSIM requests use the active external Climate Twin forcing window instead of the static example timestamp. This removes the observed `CLIMATE_TIME_OUTSIDE_FORECAST_RANGE -> validity:MicroMetTwin` rejection while preserving Layer-4 fail-closed feasibility checks.
- **Layer 5 safety remains unchanged:** unvalidated Layer4-5D -> Phoenix-4D mapping/mixer/servo/ESC evidence is not bypassed; HOLD remains valid and no fabricated physical command is emitted.
- **Layer 7 safety remains unchanged:** Layer 7 remains TDOC-managed, direct-actuation forbidden, and persistent updates remain evidence-gated.

## 2026-09-05 edge-online runtime update (v20260905_1056)

- `--online-max-cycles-per-episode-auto` resolves a mission safety cap without requiring a numeric CLI value. It prefers the latest offline mission `cycle_budget_max` and observed cycle count; if unavailable it falls back to origin/destination distance, nominal ground speed, `fast_dt_s`, margin and reserve bounds from `configs/tdoc.yaml`.
- An online mission episode is an execution repetition from origin to destination; it is **not** a new Layer-5 retraining episode. Layer 5 still loads finalized offline SETD knowledge and persistent online updates remain owned by Layer 7.
- Layer 6 now tolerates a few transient stale boundary samples on the same PT_LIB session before any expensive recovery. Persistent MAVLink receiving and JSBSim truth streaming are supplied by the required PT_LIB/Layer5 companion versions.
- Online terminal output is change-oriented: mission constants are printed once, while each cycle emphasizes progress, delta-goal, simulator-time advance, cycle latency, Layer-4 feasibility, Layer-5 decision state, Layer-6 latency/recovery, and Layer-7 state.
- Online flight performance is written to `reports/online/ONLINE_FLIGHT_PERFORMANCE.json`, `.html`, and `ONLINE_FLIGHT_PERFORMANCE_TIMESERIES.csv`. Distance/time/energy efficiencies are left null when movement or destination completion is insufficient rather than fabricated.

## v20260905_1555 decision-state reporting update

- Online mission progress is grounded only in live PT_LIB geographic position. A stationary vehicle remains at zero progress and is annotated with its Layer5/Layer6 no-actuation cause rather than receiving synthetic progress.
- Every online decision carries post-decision Phoenix vehicle/actuator feedback: attitude, speed, SERVO1..4 PWM, estimated elevon angles, estimated rotor speeds and explicit measured/estimated semantics.
- Offline mission reports now include WingMPET simulated attitude, speed, elevon commands and motor angular speeds/RPM at every decision.
- `tdoc_layers_2d.html` is now a decision-oriented time-series dashboard; raw parameter and JSON inventories remain available in collapsed audit sections.
- The PT_LIB integration follows the external-intelligence workflow in the supplied `pt_lib_doc_v1/HOW_USAGE.md`; internal `navstart`/`AUTOPILOT_DEMO` decision ownership is not mixed with TDOC/Layer5.

## 2026-09-05 strict ONLINE control-readiness gate — v20260905_1846

A normal `train-online` invocation is now a **control-training** request. Before cycle 1, TDOC asks the external Layer5/Phoenix specialization whether the Layer4 5-D candidate action can be legally bound and verified through the Phoenix logical/physical actuator path. If mapping, mixer, servo-dynamics or ESC-correction evidence is not ready, TDOC exits fail-closed with code 2 and writes `reports/online/ONLINE_CONTROL_READINESS.json`; it no longer wastes hundreds of stationary HOLD cycles.

For telemetry/PT_LIB/Layer7 validation without a control/mission-progress claim, use the explicit `--online-observation-only` switch. This mode does not weaken Layer5 FINAL or physical authorization gates.

A second defensive guard terminates a mission with `online_control_actuation_stalled` when an apparently ready configuration nevertheless remains `NO_ACTUATION_STATIONARY` with `no_admissible_bound_candidate` for the configured consecutive-cycle limit.

## 2026-09-05 nominal Phoenix online-control commissioning — v20260905_1908

Normal `train-online` now attempts a bounded held-SITL PT_LIB/ArduPilot commissioning when the Phoenix nominal control path is not yet ready. The commissioning certificate validates only the nominal pilot-domain -> ArduPilot -> SERVO_OUTPUT_RAW mixer/trim/throttle path. Layer4's five-dimensional hypothetical action vector is never treated as an actuator vector. Layer5 instead derives bounded nominal pilot commands from the selected Layer4 trajectory, while non-zero qLPV correction remains fail-closed until servo/ESC dynamic evidence is identified. Use `--no-online-auto-commission` to require a pre-existing readiness state. Observation-only validation never requires actuation and no longer triggers the no-actuation stall abort.

## v20260906_0810 online evidence hardening

TDOC now preserves PT_LIB's direct-CLI relative logging semantics when PT_LIB is called as a library: relative PT_LIB log paths are anchored to `TDDT_PTLIB_ROOT` and pinned absolute before the persistent online session continues. Authorized Layer5→Layer6 control writes carry end-to-end command correlation IDs where PT_LIB exposes them. Online workflow summaries are run-scoped and atomically refreshed from invocation start, so a crashed/blocked Online run cannot silently leave a prior Offline summary as the apparent current result.


## FMI online runtime (v20260909_1144)

`ONLINE_JSBSIM` no longer requires Docker containers. `TDDT_PTLIB_ROOT` must resolve `pt_lib_v20260914_1308`, and `EXT_FMU_SIM_ROOT` must resolve the `FMU/` directory containing exactly the compatible `ArduPilotController.fmu` and `JSBSimPhoenix.fmu` artifacts. TDOC delegates the online command sequence to Layer5/PT_LIB: prepare, set initial geospatial state before connect, connect, precheck, FBWA, HOME, ARM, GNSS-denied, trim, environment, then pilot-domain streaming through `send_pilot_command`. ArduPilot remains the actuator mixer owner; direct PWM and direct JSBSim actuator injection remain prohibited. The Offline path remains `Layer5 -> TDOC -> Layer2/WingMPET` and never depends on PT_LIB or `EXT_FMU_SIM_ROOT`.

Online training reuses the finalized Offline SETD through TDOC's compact handoff manifest and Layer5 KStore identity; it does not bulk-load raw Offline mission records into the real-time loop.


## TDOC-owned Online guidance sequence (2026-09-10)

`configs/online_guidance_sequence.yaml` is the authoritative TDOC orchestration policy for Online PT_LIB lifecycle ordering. TDOC/RuntimeScheduler enforces `prepare -> set_initial_geospatial_state -> connect -> precheck -> set_mode(FBWA) -> set_home -> arm -> enable_gnss_denied -> acquire_trim -> set_environment -> control_loop -> release_control -> close`. The sequence is derived from `pt_lib_v20260914_1308/scripts/random_flight_demo.py`, but TDOC uses strict fail-closed precheck/trim for control training. Layer5 remains the FINAL decision owner; PT_LIB remains the FMI master; `ArduPilotController.fmu` remains the sole mixer/actuator authority; commands are sent only through `PTLibFmiClient.send_pilot_command()` and direct PWM/direct JSBSim actuator injection are prohibited.

### Online Layer4 timing boundary

RTMO Online fast transactions do not synchronously execute the multi-second Layer4 full-fidelity path. They consume only verified Layer4 artifacts admitted by phase/version/freshness/state-envelope gates; otherwise the transaction holds safely while the TDOC-owned Layer4 refresh lane computes a replacement. This preserves the `>=10 Hz / <=100 ms` fast-path contract without weakening Layer4 verification.

### Certificate freshness for RTMO fast-path reuse

RTMO readiness now audits the live Layer4 certificate bank. Certificate v4 freshness uses mission-reference time rather than wall-clock age between Offline and Online preparation; current state/action containment is rechecked by Layer4 on every reuse. Historical point certificates remain legacy-only and do not satisfy the new envelope-aware readiness guard.

### Full-fidelity workspace provenance

Persistent Layer-4 verification replay state is isolated by run/episode/cycle/phase/candidate. Legacy `runtime/layer4_verification/Cxxxx/` paths are not used for new verification workspaces; repeated candidate IDs across cycles therefore cannot share restored Layer-3 state.


### Problem-19 qLPV actuator identification

Online auto-commissioning now requests `PTLIB-Phoenix-control-commission-v2`. The certificate performs bounded held-SITL effective actuator identification before Layer5 binds a simulation-only actuator-local qLPV model. The path remains Layer5 pilot-domain → PT_LIB → ArduPilot mixer → JSBSim; direct PWM/motor/elevon injection is forbidden. If dynamic identification or exact qLPV binding fails, `qlpv_nonzero_correction_ready=false` remains fail-closed while nominal commissioned control can remain available.

### RTMO live capability authority (problem 20)
Production RTMO readiness uses `runtime/rtmo/capability_registry.json.gz` plus the live Layer4 FF certificate bank and current execution commissioning. Offline CSV/JSON reports are audit-only and cannot authorize a phase. `tdoc.rtmo.capability.require_live_registry=true` is fail-closed when the registry is absent.

### Solution 9: PT_LIB demo is not an operational controller

`pt_lib_v20260914_1308/scripts/random_flight_demo.py` is used only as a public lifecycle/sanity reference. Its randomized command ranges are not imported into TDOC or Layer5 as operational bounds. Online commands originate only from `Layer5 FINAL`, are transported through PT_LIB, and are mixed/applied by `ArduPilotController.fmu`.

## P1-8 readiness taxonomy

Online readiness is now reported on three independent axes in `reports/online/ONLINE_READINESS_TAXONOMY.json`: infrastructure readiness, current-phase RuntimeRelease readiness, and mission-wide certification. Legacy `ONLINE_CONTROL_READINESS.status=READY` remains compatibility metadata for infrastructure only and is explicitly not mission-executability authority.


## P2-3 current Online observation contract

Online distinguishes visibility of the current PT_LIB/FMI plant observation from control/navigation freshness. `current_online_observation` may be present in PRE_RELEASE/HOLD and is `DIAGNOSTIC_EXECUTION_STATE` unless it can be correlated to the immediately preceding realized Layer6 execution. Only `Layer6=EXECUTED` with `actuation_executed=true` creates a `post_execution_observation`; the next cycle becomes `execution_correlated=true` only when episode/cycle adjacency holds and plant-time is present and non-regressing. Simulator/PT_LIB state is never injected into MESA: `navigation_authority=MESA_STD`, `truth_injection_to_mesa=false`.
