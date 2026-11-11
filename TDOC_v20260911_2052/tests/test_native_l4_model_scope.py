"""Real WingMPET replay/certificate consumption under the explicit model scope.

No GUI, FCU, flight command or fake positive certificate is used. A declared
hover initial condition is propagated by the shipped native WingMPET FMU; its
actual outputs feed the production physical verdict, mission feasibility and
persisted certificate manager. This tests model evidence only.
"""
from copy import deepcopy
import hashlib
import math
from pathlib import Path

import pytest

from layer4.config import Layer4Config
from layer4.engine import Layer4Engine
from layer4.models import AtomicSnapshot, Candidate
from layer4.multifidelity import CertifiedMultiFidelityManager
from layer4.native_controller_contract import (
    ModelPredictionScope, MODEL_PREDICTION_SCOPE, model_prediction_scope,
    model_ff_allowed, needs_native_controller, readiness,
)
from layer4.verification import FullFidelityVerifier
from twins_fmi import DomainTwin
from tddt_tdoc.adapters.layer2 import Layer2Adapter
from tddt_tdoc.adapters.layer4 import Layer4Adapter
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.model_compatibility import capture_model_dependencies
from tddt_tdoc.utils import sha256_obj

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def real_replay():
    twins = ROOT / "twins_layer2_v20260820_1233"
    wing = DomainTwin(twins / "fmus/WingMPET.fmu", config=twins / "config/phoenix_drone_v3.yaml",
                      tep_root=twins / "resources/tddt_layer2/WingMPET")
    initial = {"pn": 0., "pe": 0., "pd": -100., "vn": 0., "ve": 0., "vd": 0.,
               "qw": math.sqrt(.5), "qx": 0., "qy": math.sqrt(.5), "qz": 0.,
               "p_rate": 0., "q_rate": 0., "r_rate": 0., "omega_left": 650., "omega_right": 650., "soc": 1.}
    action = {"left_motor_cmd": .82, "right_motor_cmd": .82, "left_elevon_rad": 0., "right_elevon_rad": 0.}
    trace = []
    class RecordingTwin:
        def evaluate_packet(self, *args, **kwargs):
            packet = wing.evaluate_packet(*args, **kwargs)
            outputs = packet.to_dict()["state"]
            trace.append({"vtol_feasible": outputs["vtol_feasible"] >= .5,
                          "outputs": deepcopy(outputs)})
            return packet
    try:
        state, replay = Layer2Adapter._wing27_integrate(RecordingTwin(), initial, action, dt=.1,
                          wind_enu=(0., 0., 0.), air_density=1.225, source="WingMPET/op27-model-scope-integration-test")
    finally:
        wing.close()
    assert replay["substeps"] == 10 and replay["vtol_feasible"] and replay["state"]["vtol_feasible"] == 1.
    assert state["pd"] != initial["pd"]  # This is actual FMU propagation, not a copied source row.
    replay["substep_trace"] = trace
    model_hash = hashlib.sha256((twins / "fmus/WingMPET.fmu").read_bytes()).hexdigest()
    deployment = capture_model_dependencies(TDOCConfig.from_yaml(ROOT / "TDOC_v20260911_2052/configs/tdoc.yaml"))
    return initial, state, replay, model_hash, deployment


@pytest.fixture
def model_case(tmp_path, real_replay):
    initial, state, replay, model_hash, deployment = real_replay
    cfg = Layer4Config.from_yaml(ROOT / "layer4_v20260907_0608/configs/configuration_boulder.yaml")
    cfg.data["verification"]["certificate_bank_path"] = str(tmp_path / "actual_FMU_model_certificates.jsonl")
    cfg.model_prediction_scope = ModelPredictionScope()
    ctx = {"stage": "hover", "flight_phase": "hover", "online_episode_id": "model-scope-test",
           "rtmo_run_context": {"run_id": "model-test", "episode_id": "one"},
           "altitude_agl_m": 100., "effective_climate_fingerprint": "no-wind-model-test",
           "agl_authority": {"valid": True, "simulator_truth_used": False,
                             "source": "declared test initial condition; not flight navigation"}}
    std = {"representations": {"WingMPET": {"canonical_state": {"airspeed_m_s": 0.}}}}
    snap = AtomicSnapshot("model-test-v1", .001, "model-test-snapshot", std, {}, 0., {}, {}, {}, 1., {}, {}, {}, ctx, {}, {})
    vector = [initial[k] for k in initial]
    next_vector = [state[k] for k in initial]
    candidate = Candidate("real-WingMPET-model-prediction", "no-wind-test", snap.version, snap.hash, 1.,
                    [vector, next_vector], [[[0.] * len(vector) for _ in vector]], [{"J_C": 0., "mu": 1.}],
                    [[0.] * 5], {"mission": 0.}, feasible=True, violation_probability=0.)
    actual = deepcopy(replay)
    actual.update(action_consumed=True, outputs=deepcopy(replay["state"]))
    row = {"step": 1, "physical_action_consumed": True, "physical_mission_safe": replay["vtol_feasible"],
           "physical_replay": actual,
           "snapshot": {"std": {"representations": {"WingMPET": {"canonical_state": {
               "airspeed_m_s": replay["state"]["airspeed"]}}, "MicroMetTwin": {"canonical_state": {"wind_ned_m_s": [0., 0., 0.]}}}},
               "context": {**ctx, "altitude_agl_m": -state["pd"]}}}
    verdict = Layer4Adapter._wingmpet_physical_verdict([row])
    feasibility = FullFidelityVerifier(cfg, None)._full_feasibility([row], candidate, snap, None)
    assert verdict["all_rows_vtol_feasible"] and verdict["authoritative"] and feasibility["safe"], feasibility
    evidence = Layer4Adapter._ff_outcome("ATTEMPTED_AND_VERIFIED", "actual WingMPET model replay passed",
          verified=True, attempted=True, executed=True, verification_origin="FF_CURRENT",
          physical_actions_consumed=True, physical_runtime_safe=replay["ok"], physical_mission_safe=feasibility["safe"],
          wingmpet_physical_verdict=verdict, full_feasibility=feasibility,
          model_dependencies=deepcopy(deployment), timing_contract={"execution_segment_s": .1})
    evidence.update(model_prediction_scope(cfg))
    manager = CertifiedMultiFidelityManager(cfg)
    manager.model_dependencies_provider = lambda: deepcopy(deployment)
    parameter_hash = sha256_obj(Layer4Engine._semantic_parameter_payload(cfg))
    return cfg, snap, candidate, evidence, manager, model_hash, parameter_hash


def issue(case):
    _, snapshot, candidate, evidence, manager, model_hash, parameter_hash = case
    return manager.build(candidate, snapshot, None, model_hash, parameter_hash, evidence, require_physical_verdict=True)


def test_native_markers_allow_actual_model_ff_and_durable_certificate_only_under_typed_scope(model_case):
    cfg, snapshot, candidate, _, manager, model_hash, parameter_hash = model_case
    assert needs_native_controller(snapshot) and model_ff_allowed(snapshot, cfg)
    assert readiness(snapshot, candidate, cfg)["native_controller_certified"] is False
    certificate = issue(model_case)
    assert certificate and certificate["bank_persisted"] and manager.path.is_file()
    assert certificate["verification_scope"] == MODEL_PREDICTION_SCOPE and certificate["model_prediction_only"]
    assert not certificate["native_controller_certified"] and not certificate["flight_authorized"]
    assert certificate["physical_issuance_evidence"]["verdict_sources"] == ["WingMPET/op27-model-scope-integration-test"]
    assert manager.find(candidate, snapshot, None, model_hash, parameter_hash)["certificate_id"] == certificate["certificate_id"]


@pytest.mark.parametrize("fault", ["state", "action", "forcing", "model", "parameters", "source_identity", "physical_verdict", "scope"])
def test_actual_model_certificate_rejects_changed_bounds_identity_or_unqualified_physics(model_case, fault):
    cfg, snapshot, candidate, evidence, manager, model_hash, parameter_hash = model_case
    if fault == "physical_verdict":
        evidence["wingmpet_physical_verdict"]["all_rows_vtol_feasible"] = False
        assert issue(model_case) is None and not manager.path.exists()
        return
    certificate = issue(model_case)
    assert certificate is not None
    if fault == "state": candidate.trajectory[0][0] += 100.
    elif fault == "action": candidate.actions[0][0] = 1.
    elif fault == "forcing": snapshot.context["effective_climate_fingerprint"] = "different-forcing"
    elif fault == "model": model_hash = "changed-" + model_hash
    elif fault == "parameters": parameter_hash = "changed-" + parameter_hash
    elif fault == "source_identity":
        manager.model_dependencies_provider = lambda: {"model_id": "different-deployment"}
    else: cfg.model_prediction_scope = {"verification_scope": MODEL_PREDICTION_SCOPE}
    assert manager.find(candidate, snapshot, None, model_hash, parameter_hash) is None


def test_json_model_scope_and_untyped_config_never_install_the_capability(model_case):
    cfg, snapshot, candidate, evidence, manager, model_hash, parameter_hash = model_case
    del cfg.model_prediction_scope
    snapshot.context.update(execution_profile="closed_loop_sitl", model_prediction_scope=MODEL_PREDICTION_SCOPE,
                            model_prediction_only=True, native_controller_certified=True)
    cfg.data["model_prediction_scope"] = MODEL_PREDICTION_SCOPE
    assert not model_ff_allowed(snapshot, cfg) and model_prediction_scope(cfg) == {}
    assert manager.build(candidate, snapshot, None, model_hash, parameter_hash, evidence, require_physical_verdict=True) is None
    assert not manager.path.exists()


def test_tdoc_installs_process_local_scope_only_for_explicit_native_profile(tmp_path):
    for selected in (False, True):
        cfg = TDOCConfig.from_yaml(ROOT / "TDOC_v20260911_2052/configs/tdoc.yaml")
        cfg.data["runtime"]["root"] = str(tmp_path / str(selected))
        cfg.data["layers"]["layer6"]["online"]["execution_profile"] = "closed_loop_sitl" if selected else "qualified_ground"
        adapter = Layer4Adapter(cfg)
        try:
            adapter._ensure()
            assert bool(model_prediction_scope(adapter.engine.config)) is selected
        finally:
            adapter.close()
