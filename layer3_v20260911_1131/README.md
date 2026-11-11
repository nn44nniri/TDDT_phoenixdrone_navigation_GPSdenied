# TDDT Dynamic N-Twin Layer 3 — `layer3_v20260911_1131`

Python implementation of Layer 3.1/3.2/3.3 of the Trans-Domain Digital Twin (TDDT) Dynamic N-Twin architecture with a shared Center of Consistency. The implementation is traced to `TDDT_Dynamic_NTwin_Layer3_details_FA_v3.md` from the supplied `TDDT_Dynamic_NTwin_v4` package.

## Deployment boundary

This archive contains **Layer 3 only**. The following five versioned packages are external runtime dependencies and are **not embedded** in the tarball:

- `twins_layer2_v20260820_1233`
- `climate_twin_v20260911_0922`
- `TDOC_ISO_Universal_Unit_Conversion_Standard20260824_0914`
- `boulder_multilayer_map_builder_v20260814_1215`
- `SARG_v20260821_0945`

Set their paths in `configs/configuration_boulder.yaml` or override them with the documented `TDDT_*` environment variables. `DependencyRegistry` validates exact version basenames and required runtime files at startup.

## Implemented architecture

- **Layer 3.1 — Shared Trans-Domain State:** packet registry, timestamp/freshness gates, quality/validity/reliability handling, TDOC unit/frame/semantic/uncertainty normalization, and canonical representations.
- **Layer 3.2 — Dynamic N-Twin Network:** explicit directed topology `G`, Top-B budget for non-mandatory edges, epistemic weights `W`, directional coupling strengths `K`, six typed coupling classes, cross-domain residuals, constrained `C*`, `J_C`, consistency region/margin, stability, emergence, hidden-loop diagnostics, failure containment, feedback routing, provenance and version state.
- **Layer 3.3 — Progressive Multi-Rate Coordination:** Fast records/`SS-KStore`, Fast→Meso aggregation, CRP matching, official three-key SARG selection `(regime, phase, CRP)`, MRG, Meso→Fast, Meso→Slow, `SETD-KStore`, Slow→Fast supervisory policy, authorized `Commit_safe`, anti-chattering and rollback.

Layer 3 never issues a final actuator command. Action/prediction candidates are routed to Layer 5.

## Runtime use of the five external packages

- MESA-NAV and WingMPET are opened with the original `twins_fmi.DomainTwin` wrapper and their TEP resources.
- MicroMetTwin is evaluated with the original `estimate_climate.py` and converted to a Layer-2 packet with the climate package's own wrapper.
- TDOC is the conversion gateway for non-canonical boundary envelopes and vector/covariance frame transformations.
- Boulder terrain/reference-climate/magnetic rasters, CRP cluster raster, exact cell assignments and canonical CRP statistics are consumed using the package's documented runtime contracts; live MicroMet climate remains authoritative runtime environmental evidence.
- SARG references are selected through the original `sarg_builder.selector.SARGSelector` API.

Runtime coupling is two-phase. Base Twin packets are first aligned and used to compute `W`, the active graph `G`, independent six-class `K_ij^(c)`, delay readiness and safety/Top-B gates. Only then may a compatible destination FMU operation execute. WingMPET operations 5/6/7 and MESA-owned operations 10/11 are gated by that plan; their returned packets are merged into the destination packet before the final CoC/stability assessment of the same tick. `MESA→Climate` performs a real TDOC NED→WGS84/time/altitude query when an explicit geodetic origin is supplied. `WingMPET→MESA` remains Model/Error evidence because the published MESA contract has no generic external-feasibility setter; the evidence is inserted into the destination MESA packet context rather than inventing an FMI variable. Layer 3 never overwrites Twin-owned state without a published destination operation.

## Configuration

Canonical configuration file:

```text
configs/configuration_boulder.yaml
```

The previous name `configs/example_boulder.yaml` is intentionally absent.

Default external layout is `../dependencies/<exact-version>/`, but paths can be overridden, for example:

```bash
export TDDT_TWINS_ROOT=/opt/tddt/twins_layer2_v20260820_1233
export TDDT_CLIMATE_ROOT=/opt/tddt/climate_twin_v20260911_0922
export TDDT_TDOC_ROOT=/opt/tddt/TDOC_ISO_Universal_Unit_Conversion_Standard20260824_0914
export TDDT_SARG_PACKAGE_ROOT=/opt/tddt/SARG_v20260821_0945
export TDDT_SARG_ROOT=/opt/tddt/SARG_v20260821_0945/generated/SARG
export TDDT_BOULDER_ROOT=/opt/tddt/boulder_multilayer_map_builder_v20260814_1215
export TDDT_MAP_ROOT=/opt/tddt/boulder_multilayer_map_builder_v20260814_1215/outputs/region_v1_map
export TDDT_CRP_ROOT=/opt/tddt/boulder_multilayer_map_builder_v20260814_1215/outputs/region_v1_map/CRP
```

## Quick start

```bash
python -m pip install -e .
source configs/environment.sh
./run_demo.sh
```

or:

```bash
python -m layer3.cli demo \
  --config configs/configuration_boulder.yaml \
  --request examples/runtime_request.json \
  --steps 6 \
  --output runtime/demo_result.json
```

See `HOW_USAGE.md` for input envelopes, runtime APIs, configuration fields, direct packet mode, safe commit/rollback, dependency invocation and validation.

## Validation artifacts

- `TEST_REPORT.json`
- `MANIFEST.json`
- `SHA256SUMS.txt`
- `CONFORMANCE_MATRIX.json`
- `docs/CONFORMANCE_REPORT.md`
- `docs/IMPLEMENTATION_MAP.md`
- `runtime/dependency_capabilities.json`


## External dependency environment

```bash
source configs/environment.sh
```

The configuration uses `${VAR:-fallback}` for all external dependency roots.

### Runtime dependency preflight

Boulder runtime sampling imports `map_mosaic_query.py`, which imports Shapely at module load time.
Therefore Layer 3 declares `shapely>=2.0` as a direct runtime dependency in both `pyproject.toml` and `requirements.txt`.
After loading the external paths, verify both Python dependencies and all five package roots with:

```bash
source configs/environment.sh
python -m layer3.cli preflight --config configs/configuration_boulder.yaml
```

A successful preflight reports `"ok": true`. The standard `run_demo.sh` runs this check automatically before starting the FMUs.



## 0935 Layer-3.2 + strict-TDOC conformance corrections

This release closes the defects found in the 2052 validation: release identity is consistent, generated caches/runtime outputs are excluded from the distributable archive, CRP nearest-cluster selection is separated from statistical acceptance, and SARG guidance is blocked when its CRP context is out of distribution.

The Boulder runtime path now consumes the aligned terrain, climate, magnetic and CRP cluster rasters **and** cross-checks `crp_cell_assignments.csv.gz` at the current pixel/time bin. Boulder climate remains reference context only; live weather remains owned by `MicroMetTwin`.

The SARG runtime path validates and consumes the documented authoritative family: `metadata.json`, `schema.json`, `scenarios.csv`, `scenario_climate_contexts.csv`, `sarg_records.csv`, `sarg_patterns.csv`, `sarg_lookup_index.csv`, `sarg_stage_summary.csv`, `status_codes.csv`, `fmi_execution_trace.csv`, `crs_rejections.csv`, `region_context.json`, `region_v1.geojson`, `sarg_samples.csv.gz`, and the selected pointwise `routes/Sxx_Cxxx.csv` trajectory.

Layer 3.2 edge records now expose `active`, six-class coupling type/vector, directional `K`, delay, `Phi` contract and provenance. `TimeScheduler` explicitly owns Fast/Meso/Slow boundary state. Diagnostic stability/emergence/LCI values remain labelled as diagnostics unless the corresponding identified dynamic model is available.


## Strict TDOC boundary contract (0935)

`runtime.require_contract_normalization: true` is enforced. External cross-domain physical values are accepted only as explicit envelopes containing at least `value`, `unit`, `frame`, and `semantic`; `uncertainty`, `timestamp`, `source`, and `provenance` are preserved or completed for audit. Bare external values are rejected. Bare values are permitted only on explicitly trusted Layer-3-internal paths after the destination FMU contract has been read.

`alignment.strict_units`, `alignment.strict_frames`, and `alignment.strict_semantics` are active runtime gates. Semantic relabeling requires an explicit coupling mapping/provenance. `CONTEXT_DEPENDENT`, `BODY`, or `MAP` frames require an explicit resolver whenever a basis change is needed; they are not silently treated as identity transforms. Qualified scalar labels such as `SCALAR_LOCAL_ATMOSPHERE` are canonicalized to `SCALAR` because they carry provenance rather than a vector basis.

The TDOC gateway now routes and audits scalar unit conversion, NED↔ENU, FRD↔FLU, WGS84↔ECEF, local-NED→WGS84, generic CRS conversion, MSL↔AGL, ellipsoidal↔orthometric, NED-Down↔altitude, QNH/QFE/QNE pressure references, pressure↔log-pressure, True/Magnetic/Grid/Nav/ENU heading conventions, meteorological wind-FROM→vector-TO, IAS/CAS/EAS/TAS/Mach, quaternion convention/frame conversion, and UTC/GPS/TAI/mission/FMI time conversions. Numeric covariance/uncertainty propagation is used where TDOC exposes the required Jacobian/numeric propagation adapter.

Known upstream metadata mismatches are corrected without modifying external packages by the versioned audit file `resources/tdoc_contract_overrides.json`. In particular, MESA operation 10 uses dimensionless `ln(P[Pa])`, dimensionless `ln(scale_height[m])`, and a pressure time scale in seconds; Wing operation 6 wind time derivatives use `m/s2` and wind spatial Jacobians use `1/s`. Every override carries its reason and provenance.

The official example request uses strict envelopes for MESA/Wing inputs, Climate latitude/longitude/time/altitude, target position, and environmental-anchor inputs. Layer-2 output packets are augmented with `signal_envelopes`, and cross-domain FMU calls consume those source envelopes before destination normalization.

## Registry-driven N-Twin semantics

The three supplied FMUs are the default operational profile, not a hard-coded network limit. `TwinRegistry.register()` can add additional packet-producing Twins with a generic or declared alignment adapter and CoC observations. Stability diagnostics derive their node order from the active graph, CoC accepts explicit observation projections `P_i` into arbitrary shared dimensions plus linear or registered `Psi_m` constraints, and Hidden Loop analysis enumerates simple cycles from the active graph rather than using a fixed three-node cycle. Partial CoC results are explicitly `partial_solved`/`unassessed`; they are never promoted to an `inside=true` consistency claim merely because evidence is missing.

## Scenario-report foundation inherited from 1103 (superseded by A* 1211)

This release adds an executable operational scenario instead of a synthetic plotting-only example. `examples/run_layer3_scenario.py` calls the real `Layer3FMURuntime` for all three configured Twins and records the actual Fast/Meso/Slow `StepResult` stream. The authoritative boundary/start/destination come from external Boulder `region/region_v1.geojson`; the 3D report uses the real `terrain_obstacles.tif` raster and separately shows the executed WingMPET input path, MESA-NAV estimate, coupled MicroMet query positions and 3-D wind. The 2D report shows CoC mismatch/margin, risk, all weights/couplings, TDOC health and Meso/Slow event boundaries.

Run after loading the deployment environment:

```bash
source configs/environment.sh
./run_scenario_report.sh
```

The default output is `reports/scenario/`. See `docs/SCENARIO_REPORT.md` for artifact semantics and command options. `plotly>=6.0` is now a declared/preflight-validated runtime dependency.

## A* Layer-3 scenario (1211)

The operational scenario is now route-selected by `RasterAStarPlanner`, not a preconstructed sinusoidal trajectory. A* uses Boulder terrain/obstacle/wind raster evidence and replans from MESA uncertainty, actual MicroMet wind, Layer-3 risk, Meso boundaries and safety/cross-track triggers. WingMPET operations 09/31/14/15/16 provide aerodynamic feasibility, power/SoC, path-time-energy accounting, goal progress and mission objective evaluation. `ObjectiveRegistry` reports explicit Fast/Meso/Slow target/actual/error/achievement percentages. See `docs/SCENARIO_REPORT.md` and run `./run_scenario_report.sh`.


## 1211 validated A* release snapshot

The release-validation run uses 12 Fast evaluations, producing 4 Meso events and 2 Slow events (`3 Fast -> 1 Meso`, `2 Meso -> 1 Slow`). The A* planner replanned six times from runtime feedback. The executed mission path was about 3034.51 m versus a 2384.29 m direct start-to-destination distance; the harness reached the destination in about 289.73 s with about 2920.77 J WingMPET-accounted energy. Final objective attainment was Fast 97.26%, Meso 96.55%, and Slow 95.03%. Slow remains below 100% because the safety-fraction and mean-Fast-attainment targets are intentionally not fabricated as satisfied.
