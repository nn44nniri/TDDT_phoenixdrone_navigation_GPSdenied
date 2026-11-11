# TDDT Layer 7 — EGB-RAC-L7

This package is a Python implementation of the Edge-oriented Layer 7 formalism in `docs/Formalism_layer7_v2.md`, aligned with `docs/TDDT_Dynamic_NTwin_Layer7_details_FA_v2.md` and the TDOC orchestration contract.

## Architectural role

Layer 7 is a **TDOC-managed side module**. TDOC owns invocation, versioned routing, synchronization boundaries, and the network lifecycle. Layer 7 consumes one immutable transaction containing execution evidence, prediction evidence, the network snapshot, and the decision trace. It returns versioned adaptation evidence/candidates to TDOC. It does **not** invoke Layers 2--6 and cannot actuate directly.

The implemented order is:

`EvidenceGate -> ResidualDecomposition -> SparseGP -> BayesianAttribution + Drift -> DeterministicRoute -> EvidenceSufficiency/Trigger -> CandidateUpdate -> Safety/Graph/Stability -> ShadowValidation/Governance -> TDOC-managed SafeCommit -> typed feedback`

This preserves the corrected `Attribution -> Route -> Trigger` order from Formalism v2.

## Edge design

- fixed-size Sparse GP dictionaries with incremental inverse updates;
- bounded Bayesian changepoint run-length state;
- compact factorized Bayesian attribution rather than an unbounded network;
- bounded SS-KStore and compact SETD-KStore;
- Tarjan SCC and bounded cycle selection;
- no MCMC at runtime;
- no online LMI solver; stability certificates may be precomputed and supplied;
- no unbounded history or all-simple-cycle enumeration;
- structural adaptation is fail-closed without field evidence, TEP coverage, stability/shadow evidence and governance authorization.

## External dependencies

No PT_LIB or TDOC code is vendored.

- `TDDT_PTLIB_ROOT`: optional external PT_LIB root for contract discovery/integration.
- `TDDT_TDOC_ROOT`: optional TDOC implementation root for deployment tooling; the Python side module integrates by contract/callback and does not import TDOC internals.
- `TDDT_KSTORE_ROOT`: optional root for persistent bounded SS/SETD KStores.
- `TDDT_LAYER7_STATE_ROOT`: optional staging root for safe-commit records.

## Install and run

The editable install **must be executed from the extracted package root**, i.e. the directory that contains `pyproject.toml` (and the compatibility `setup.py`).  Two supported extraction layouts are shown below.

### A. Keep the versioned directory

```bash
tar -xzf layer7_v20260901_0921.tar.gz
cd layer7_v20260901_0921
python3 -m venv env
. env/bin/activate
pip install -e .
tddt-layer7 --config configs/default.json preflight
tddt-layer7 --config configs/default.json demo
pytest -q
```

### B. Make an existing `.../layer7/code` directory the project root

```bash
cd /path/to/TDDT_navigation/layer7/code
tar -xzf /path/to/layer7_v20260901_0921.tar.gz --strip-components=1
python3 -m venv env
. env/bin/activate
pip install -e .
tddt-layer7 --config configs/default.json preflight
tddt-layer7 --config configs/default.json demo
pytest -q
```

`pytest>=7` is now an explicit installation dependency so the exact validation sequence above does not leave the `pytest` command missing after `pip install -e .`.

If `pip install -e .` reports that neither `setup.py` nor `pyproject.toml` exists, the shell is not in the extracted project root.  Run `pwd; ls pyproject.toml setup.py` before installing.

See `HOW_USAGE.md` and `docs/TDOC_INTEGRATION.md` for the complete input/output contract.

## v0.3.0 conformance notes

The former application hooks have been hardened into explicit contracts:

- FIELD/HIL evidence requires a linked PT_LIB transaction and matching TDOC quantity/unit/frame metadata.
- `twin_validity` is mandatory for weighted Twins; invalid Twins are hard-gated to zero weight.
- Structural validation is candidate-specific. Use `tdoc_validation_callback` to provide shadow/stability evidence after Layer 7 computes the candidate hash.
- TDOC can supply `tdoc_rollback_callback`; post-commit monitoring persists across cycles and restores the last-safe TEP version when rollback is acknowledged.
- Persistent KStore storage is bounded by `kstore_max_segments` and `setd_max_packages`.
- `Compliance_7` is calculated from the current transaction; it is no longer a hard-coded flag.

Production callback skeleton:

```python
from tddt_layer7 import Layer7SideModule

def validate_candidate(req):
    return {
        "base_version": req["base_version"],
        "candidate_hash": req["candidate_hash"],
        "stability_ok": True,
        "active_metrics": {"error": 2.0},
        "candidate_metrics": {"error": 1.0},
        "violation_count": 0,
        "coverage_ok": True,
        "margin": 0.5,
        "sample_count": 6,
    }

def commit_or_rollback(req):
    if req["operation"] == "commit":
        return req["candidate_version"]
    return req["target_version"]

module = Layer7SideModule(
    tdoc_validation_callback=validate_candidate,
    tdoc_commit_callback=commit_or_rollback,
    tdoc_rollback_callback=commit_or_rollback,
)
```
