> **STAGE-5 NATIVE PILOT AUTHORITY UPDATE (2026-10-09)**: This standalone source release intentionally blocks `ONLINE_JSBSIM` native pilot `FINAL`/`layer6_package` until an independently verified ArduPilot controller replay provider exists. Historical positive online examples in this document describe an earlier model-only simulation path and are **not flight authorization**. See `README_STAGE5_REPAIR_AND_SITE_VALIDATION_FA.md`.

# TDDT Dynamic N-Twin — Layer 5

**Package:** `layer5_v20260911_1510`  
**Python:** `>=3.10`  
**Architecture:** ERA-qLPV-MPC with PhoenixDrone specialization, Predictive Safety Filter, Runtime Assurance, Governance, Single-Episode KStore, and TDOC-managed boundaries.

## Architectural role

Layer 5 consumes the atomic output of Layer 4 and turns **already generated Layer-4 candidates** into an admissible, confidence-scored, safety-filtered and governed decision. It does **not** regenerate the Layer-4 Pareto front, does **not** commit predicted `W/K/G`, and does **not** write PWM/RPM directly.

The implementation follows the decision chain:

`Layer4 Y4 -> TDOC/CI -> Candidate Registry -> Admissibility -> ERA-qLPV correction -> Lexicographic decision -> Confidence -> Operational mode -> PSF -> RTA -> Governance -> Layer6 package`

and the lifecycle split:

- `OFFLINE_SE`: exactly one resumable offline episode; Layer-2 virtual Twin adapters; SS-KStore -> atomic SETD-KStore; no Layer 6/JSBSim call.
- `ONLINE_JSBSIM`: valid SETD required; PT_LIB/JSBSim adapter boundary; no new training episode; no persistent knowledge writes from Layer 5.

## Segmented KStore storage

The FILE KStore is intentionally multi-file. SS-KStore writes bounded append-only JSONL segments under `ss/segments/` with a hashed `ss/SS_MANIFEST.json`; no global ever-growing `ss_kstore.jsonl` is created. SETD-KStore is published as a versioned directory under `setd/packages/`, with component shards and a package manifest. SS records and SETD data parts have explicit byte ceilings; oversized indivisible SETD values fall back to canonical-JSON byte shards, while an oversized SS record fails closed. `SETD_KSTORE.json` is only a small current-package pointer. This realizes the v4 formalism's chunked FILE backend and preserves the article's append-only/low-memory SS-KStore -> compressed/loadable SETD-KStore memory path without requiring the whole high-frequency episode in RAM.

See `docs/KSTORE_STORAGE.md` for the exact layout and recovery rules.

## Phoenix fail-closed status

The shipped `phoenix_drone_v3.yaml` authorizes forward-flight integration only. Transition is not validated, `RUDD_DT_GAIN=0`, direct PWM is disabled, servo dynamics are not identified, and the Layer4-action-to-Phoenix pilot-domain mapping is not validated. Consequently:

- full-envelope VTOL transition remains disabled;
- differential-thrust yaw is not claimed;
- non-zero qLPV correction remains disabled until a calibrated qLPV model + mixer + servo identification are supplied;
- serialized Layer-4 inputs without an explicit validated Phoenix pilot-domain action sequence fall back safely.

This is intentional conformance behavior, not an implementation omission.

## Install

```bash
cd layer5_v20260911_1510
python -m pip install -e .
source configs/environment.sh
```

The shipped `configs/environment.sh` resolves the external TDOC unit/frame/time/semantic/covariance adapter through `TDDT_UNIT_ADAPTER_ROOT` only. It also defines `TDDT_KSTORE_ROOT`, which is the primary filesystem location for Layer-5 file-backed knowledge persistence and defaults to `$TDDT_ORCHESTRATOR_ROOT/runtime/kstore`; edge deployments may override it to a dedicated persistent volume.

PT_LIB is an external-only dependency. For `ONLINE_JSBSIM`, point `TDDT_PTLIB_ROOT` at the extracted `pt_lib_v20260907_0608` package root. Layer 5 does not ship or vendor PT_LIB, and the adapter rejects an incomplete root or any `pt_lib` module resolved outside that exact root.

```bash
export TDDT_PTLIB_ROOT=/path/to/pt_lib_v20260907_0608
```

For strict TDOC preflight:

```bash
export TDDT_UNIT_ADAPTER_ROOT=/path/to/TDOC_ISO_Universal_Unit_Conversion_Standard20260824_0914
tddt-layer5 preflight --strict-external
```

Run a serialized Layer-4 result:

```bash
tddt-layer5 run-layer4 \
  --input /path/to/layer4_result.json \
  --output runtime/layer5_result.json
```

The bundled real Layer-4 demo is deliberately expected to return `HOLD`, because that demo contains no full-fidelity verified candidates.

See `HOW_USAGE.md` for the full API/configuration contract and `CONFORMANCE_MATRIX.json` for formalism coverage.


## v20260904_1028 orchestration hardening

- Valid `OFFLINE_SE` HOLD results caused by normal admissibility/verification fail-closed gates are persisted as non-actuating Fast knowledge. Invalid contract/lifecycle input is not learned.
- This permits the one allowed offline episode to finalize SS-KStore into SETD-KStore even when physical Phoenix mapping remains intentionally unvalidated.
- `PTLibJSBSimAdapter.prepare()` and `connect()` no longer latch successful state after explicit negative external readiness results.
- No actuator mapping, mixer validation, direct PWM, or safety gate has been relaxed.

## v20260905_1056 online edge adapter update
The PT_LIB environment adapter now starts the PT_LIB non-interactive background MAVLink receiver and persistent JSBSim truth stream once after `connect_session()`, and stops both during controlled close. This realizes the existing `PrepareOnce -> ConnectPersistent -> ReadBoundary/ReadTruth -> Shutdown` formalism without changing Layer-5 MPC, safety, authorization, KStore, or Phoenix validation gates.

## v20260905_1908 commissioned nominal Phoenix control

Layer5 now separates two gates: `nominal_control` and `qlpv_nonzero_correction`. A bounded PT_LIB SITL commissioning certificate may enable the nominal trajectory-reference tracker and ArduPilot mixer path. This tracker consumes the selected Layer4 state trajectory; the Layer4 5-D `actions` remain hypothetical and are never reinterpreted as actuators. Legacy v1 static commissioning does not identify servo bandwidth or ESC mapping. PT_LIB commissioning v2 adds bounded held-SITL identification of the effective command-to-`SERVO_OUTPUT_RAW` surface dynamics and throttle-to-twin-motor normalized mapping. Layer5 accepts that evidence only as `simulation_only`, binds a calibrated actuator-local qLPV correction model to the exact Phoenix profile/airframe hash, and still requires per-candidate chance/safety gates. This does not claim real-aircraft servo/ESC identification.


## PT_LIB FMI 0.8.0 migration (v20260909_1144)

`ONLINE_JSBSIM` now uses external `pt_lib_v20260909_0932` through its documented `PTLibFmiClient` facade and resolves the external FMI 2.0 Co-Simulation artifacts exclusively through `EXT_FMU_SIM_ROOT`. The required FMUs are `ArduPilotController.fmu` and `JSBSimPhoenix.fmu`; Docker/container runtime is not part of this online execution path. The command order is prepare, set initial geospatial state before connect, connect, precheck, set FBWA, set HOME, arm, enable GNSS-denied mode, acquire trim, set environment, then use `send_pilot_command` with `read_state`/`read_actuator_feedback`. ArduPilot remains the mixer owner and direct PWM remains prohibited. `OFFLINE_SE` remains independent of PT_LIB and these FMUs.
