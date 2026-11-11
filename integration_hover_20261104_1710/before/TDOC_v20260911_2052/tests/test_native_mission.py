"""Unit contract tests; these fixtures never authorize or start a Native session."""
from copy import deepcopy
from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "layer5_v20260911_1510/src"))

from tddt_tdoc import native_mission as native
from tddt_tdoc.utils import sha256_obj


class Config:
    def __init__(self):
        self.values = {"layers.layer6.online.execution_profile": "closed_loop_sitl",
                       "layers.layer6.online.transport": "Layer6Client_GUI_MAVLink"}

    def get(self, key, default=None):
        return self.values.get(key, default)


@pytest.fixture
def decision(monkeypatch):
    # Telemetry's wire/clock validator has separate Native tests. Isolate this
    # consumer's identity, command and lifecycle checks from a real FCU transport.
    def validate(status):
        if status.get("fresh") is not True:
            raise ValueError("stale native status")
    monkeypatch.setattr(native, "_validate_status", validate)
    env = {key: key + "-identity" for key in native.IDENTITY_KEYS}
    env.update(ok=True, handoff_hash="a" * 64, forcing_file_sha256="b" * 64)
    state = {"fresh": True, "session_id": "s" * 32, "monotonic_domain_id": "local-domain",
             "status_quality": {"native_session_id": "native-process-session"},
             "target_system": 1, "target_component": 1, "plant_time_s": 12.,
             "armed": False, "mode": "QHOVER", "mavlink_connected": True,
             "landed_state": "ON_GROUND", "throttle": 0., "environment_contract": env}
    request = {"transaction_id": "tx-current", "stage": "vertical_takeoff"}
    candidate = {"candidate_id": "current-candidate", "snapshot_hash": "c" * 64,
                 "snapshot_version": "v1", "candidate_verified": True, "feasible": True,
                 "verification": {"verified": True, "executed": True}}
    layer4 = {"snapshot_hash": "c" * 64, "snapshot_version": "v1",
              "candidates": [candidate], "verified_candidates": [deepcopy(candidate)]}
    command = [.1, -.1, .6, 0.]
    package = {"u_cmd": command, "A_target": ["left_elevon", "right_elevon", "left_motor", "right_motor"],
               "C_exec": {"direct_pwm": False}, "S_safe": {"psf": True, "rta": True},
               "tau_apply": time.time() - .1, "tau_deadline": time.time() + 30.,
               "rho_decision": .9, "pi_decision": {"decisionID": "current-decision"}, "nu": "v1",
               "execution_intent": {"requested_pilot_mode": "QHOVER", "discrete": ["ARM"]}}
    cid = candidate["candidate_id"]
    gates = {name: {"ok": True} for name in ("tdoc_boundary", "lifecycle", "budget")}
    gates.update({name: {cid: {"ok": True}} for name in
                  ("integrity", "admissibility", "policy_gate", "phoenix_binding")})
    gates.update(FINAL=True, psf={"gate": True}, rta={"source": "advanced"}, governance={"gate": True},
                 solver={cid: {"ok": True, "vp": {"ok": True}, "corrected_actions": [command]}})
    result = {"ok": True, "learning_mode": "ONLINE_JSBSIM", "snapshot_hash": "c" * 64,
              "snapshot_version": "v1", "decision": {"candidate_id": cid, "decision_id": "current-decision"},
              "gates": gates, "control_action": {"first_move": command}, "layer6_package": package,
              "provenance": {"layer4_input_digest": sha256_obj(layer4), "only_first_mpc_move": True}}
    config = Config()
    binding = native.NativeMissionBinding(config, status_source=lambda: deepcopy(state),
              prior_validator=lambda: {"ok": True, "handoff_hash": "a" * 64},
              environment_validator=lambda status: status["environment_contract"]["forcing_file_sha256"] == "b" * 64)
    return binding, result, layer4, request, state


def bind(decision):
    binding, result, layer4, request, _ = decision
    return binding.bind_result(result, layer4_result=layer4, request=request)


def test_current_mpc_command_and_arm_intent_preserved_without_controller_certification(decision):
    binding, original, _, request, _ = decision
    result = bind(decision)
    assert result["layer6_package"]["u_cmd"] == original["layer6_package"]["u_cmd"]
    assert result["layer6_package"]["execution_intent"] == original["layer6_package"]["execution_intent"]
    check = binding.verify_result(result, transaction_id=request["transaction_id"])
    assert check["ok"] and check["execution_contract_verified"]
    assert not check["flight_certified"] and not check["native_controller_certified"]
    assert "native_execution_model_matched" not in check
    assert not check["execution_confirmed"]
    assert "transaction_id" not in original["layer6_package"]


@pytest.mark.parametrize("changed", ["command", "snapshot", "FINAL", "solver", "identity", "intent"])
def test_reject_mutation_after_registration(decision, changed):
    binding, _, _, request, _ = decision
    result = bind(decision)
    if changed == "command": result["layer6_package"]["u_cmd"][2] = .7
    elif changed == "snapshot": result["snapshot_hash"] = "wrong"
    elif changed == "FINAL": result["gates"]["FINAL"] = False
    elif changed == "solver": result["gates"]["solver"]["current-candidate"]["vp"]["ok"] = False
    elif changed == "identity": result["layer6_package"]["transaction_id"] = "tx-other"
    else: result["layer6_package"]["execution_intent"]["discrete"] = []
    assert not binding.verify_result(result, transaction_id=request["transaction_id"])["ok"]


@pytest.mark.parametrize("changed", ["forcing", "handoff", "session", "stale", "clock"])
def test_recheck_current_environment_and_session_before_execution(decision, changed):
    binding, _, _, request, state = decision
    result = bind(decision)
    if changed == "forcing": state["environment_contract"]["forcing_file_sha256"] = "changed"
    elif changed == "handoff": state["environment_contract"]["handoff_hash"] = "d" * 64
    elif changed == "session": state["session_id"] = "z" * 32
    elif changed == "stale": state["fresh"] = False
    else: state["plant_time_s"] = 11.
    assert not binding.verify_result(result, transaction_id=request["transaction_id"])["ok"]


@pytest.mark.parametrize("changed", ["unverified", "foreign_input", "command", "gate", "fixture", "profile", "airborne_arm", "nonzero_prearm_throttle"])
def test_reject_unbound_sources_and_unsafe_arm_conditions(decision, changed):
    binding, result, layer4, _, state = decision
    if changed == "unverified": layer4["verified_candidates"] = []
    elif changed == "foreign_input": layer4["snapshot_version"] = "other"
    elif changed == "command": result["layer6_package"]["u_cmd"] = [0., 0., .7, 0.]
    elif changed == "gate": result["gates"]["rta"]["source"] = "baseline"
    elif changed == "fixture": layer4["provenance"] = {"test_fixture": True}
    elif changed == "profile": binding.config.values["layers.layer6.online.execution_profile"] = "qualified_ground"
    elif changed == "airborne_arm": state["landed_state"] = "IN_AIR"
    else: state["throttle"] = .1
    if changed in ("unverified", "foreign_input", "fixture"):
        result["provenance"]["layer4_input_digest"] = sha256_obj(layer4)
    with pytest.raises(ValueError):
        bind(decision)


def test_readiness_does_not_authorize_an_unregistered_decision(decision):
    binding, result, _, request, _ = decision
    ready = binding.operational_readiness()
    assert ready["ok"] and not ready["ARM_authorized"] and not ready["flight_certified"]
    assert not binding.verify_result(result, transaction_id=request["transaction_id"])["ok"]


def projected(decision):
    from tddt_tdoc.rtmo.artifact_gate import RTMOL4ArtifactGate
    _, result, layer4, _, _ = decision
    source = RTMOL4ArtifactGate._execution_projection(layer4)
    # Production acquire() adds admission metadata after the projection digest.
    source["provenance"]["rtmo_l4_artifact_reuse"] = {"reuse": True, "artifact_age_s": .1}
    layer4.clear()
    layer4.update(source)
    result["provenance"]["layer4_input_digest"] = sha256_obj(layer4)
    return source


def test_production_execution_projection_verified_id_index_is_accepted(decision):
    source = projected(decision)
    assert source["verified_candidates"] == [{"candidate_id": "current-candidate"}]
    binding, _, _, request, _ = decision
    result = bind(decision)
    assert binding.verify_result(result, transaction_id=request["transaction_id"])["execution_contract_verified"]


@pytest.mark.parametrize("mutation", ["candidate", "forged_verified_id", "missing_projection", "projection_hash", "forged_selected_id"])
def test_projected_candidate_and_verified_index_remain_hash_bound(decision, mutation):
    source = projected(decision)
    _, result, layer4, _, _ = decision
    if mutation == "candidate":
        source["candidates"][0]["feasible"] = False
    elif mutation == "forged_verified_id":
        source["verified_candidates"][0]["candidate_id"] = "forged"
    elif mutation == "missing_projection":
        source["provenance"].pop("rtmo_execution_projection")
    elif mutation == "projection_hash":
        source["provenance"]["rtmo_execution_projection"]["sha256"] = "f" * 64
    else:
        old = "current-candidate"
        source["candidates"][0]["candidate_id"] = "forged"
        source["verified_candidates"][0]["candidate_id"] = "forged"
        result["decision"]["candidate_id"] = "forged"
        for key in ("integrity", "admissibility", "policy_gate", "phoenix_binding", "solver"):
            result["gates"][key]["forged"] = result["gates"][key].pop(old)
    layer4.clear()
    layer4.update(source)
    result["provenance"]["layer4_input_digest"] = sha256_obj(layer4)
    with pytest.raises(ValueError):
        bind(decision)
