# TDDT Dynamic N-Twin Layer 4 — Lightweight Edge Implementation

Package: `layer4_v20260907_0608`  
Python: 3.10+  
CLI: `tddt-layer4` or `python -m layer4.cli`

This package implements Layer 4 as a bounded-compute **Prediction + What-if Simulation + Optimization** service for an onboard UAV edge CPU. It consumes an atomic Layer-3 `NetworkSnapshot` and produces traceable candidate futures; it never emits actuator commands and never commits `W`, `K`, or `G`.

## Architecture

`L3 Snapshot -> Hash/Version -> DFA/Reduced Scenarios -> Beta(W) + FF-RLS(K) -> qLPV/Tube -> 7-objective robust chance-constrained optimizer -> Square-root Cubature UQ -> WLS-CoC -> Pareto -> POD screening -> selective Layer-3/Twin full-fidelity verification -> Candidate Registry`

The external Twin, Climate, TDOC, SARG, CRP/map, and Layer-3 packages are deliberately **not vendored**. Their roots are resolved through `configs/environment.sh`.

## Important calibration boundary

`resources/edge_model/qlpv_model.npz` is a small reference qLPV realization that makes the package executable and testable. It is not claimed to be an aircraft-identification certificate. For deployment, fit/replace the qLPV vertices from validated VTOL data or approved Twin trajectories using `tddt-layer4 fit-qlpv` and revalidate the ROM, periodic-RPI terminal sets, and timing evidence. The bounded online optimizer is complete on the packaged model; aircraft-specific model calibration remains a separate deployment requirement.

## Quick start

```bash
source configs/environment.sh
python -m pip install -e .
python -m layer4.cli preflight --config configs/configuration_boulder.yaml
python -m layer4.cli run-snapshot \
  --config configs/configuration_boulder.yaml \
  --snapshot examples/layer3_snapshot_example.json \
  --output runtime/layer4_result.json
```

Use `--strict-external` with `preflight` when full-fidelity verification must be available before launch.

## External versions targeted

- `twins_layer2_v20260820_1233`
- `climate_twin_v20260824_0945`
- `TDOC_ISO_Universal_Unit_Conversion_Standard20260824_0914`
- `SARG_v20260821_0945`
- `boulder_multilayer_map_builder_v20260814_1215`
- `layer3_v20260907_0608`

See `HOW_USAGE.md`, `docs/Formalism_layer4.md`, and `EXTERNAL_COMPATIBILITY.json`.

## Optimization realization in v20260826_1723

The online solver now optimizes the full seven-element Layer-4 objective vector (`mission`, `time`, `energy`, `risk`, `uncertainty`, `consistency`, `margin`) inside the optimization loop. A shared hypothetical action sequence is evaluated against a reduced uncertainty pool; finite reduced-scenario min-max cost, chance probability, continuous hard-constraint violation penalties, the intersection of tube-tightened input sets, and the periodic RPI terminal certificate are all part of the optimization merit/gates rather than post-processing only. Final Pareto ranking remains non-dominated in the original seven-objective space.

`resources/pareto/explicit_regions.json` now contains a validated reference PWA warm-start region for the packaged Boulder/reference model. It is intentionally not a deployment certificate; outside validated explicit regions the complete bounded online optimizer is normative.
