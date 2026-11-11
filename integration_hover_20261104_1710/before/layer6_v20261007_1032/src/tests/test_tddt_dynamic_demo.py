import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("demo_tddt_l5_l7", ROOT / "src" / "demo_tddt_l5_l7.py")
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MOD)


def test_dynamic_layer5_decision_overlays_phase_and_preserves_contract():
    template = {
        "transaction_id": "cycle-42",
        "u_cmd": [0.1, -0.05, 0.55, 0.0],
        "C_exec": {"direct_pwm": False, "pilot_domain": {"a": [-1,1], "e": [-1,1], "t": [0,1], "r": [-1,1]}},
        "S_safe": {"psf": True, "rta": True},
        "rho_decision": 0.9,
        "nu": "fixture",
    }
    p = MOD._build_dynamic_l5_decision(
        template, index=1, phase="arm", mode="QHOVER",
        u_cmd=[0,0,0,0], discrete=("ARM",), ttl_s=10, rate_hz=30,
    )
    assert p["transaction_id"].startswith("tddt-demo-01-arm-")
    assert p["u_cmd"] == [0.0, 0.0, 0.0, 0.0]
    assert p["execution_intent"]["requested_pilot_mode"] == "QHOVER"
    assert p["execution_intent"]["discrete"] == ["ARM"]
    assert p["execution_intent"]["flight_phase"] == "arm"
    assert p["decision"]["FINAL"] is True
    assert p["control_action"]["first_move"] == [0.0, 0.0, 0.0, 0.0]
    assert p["C_exec"]["direct_pwm"] is False
    assert p["S_safe"] == {"psf": True, "rta": True}
    assert p["execution_rate_hz"] == 30.0
    assert p["tau_deadline"] > p["tau_apply"]


def test_parser_defaults_to_dynamic_and_retains_single_mode():
    p = MOD.build_parser()
    args = p.parse_args([])
    assert args.single_decision is False
    assert args.layer7_trace.name == "layer7_input_demo.jsonl"
    args2 = p.parse_args(["--single-decision"])
    assert args2.single_decision is True


def test_dynamic_phase_set_matches_flight_demo_contract():
    names = [x[0] for x in MOD.PHASES]
    assert names == [
        "ground", "arm", "vertical_takeoff", "hover", "vtol_translation",
        "transition", "cruise", "approach", "back_transition",
        "vtol_approach", "final_hover", "descent", "touchdown", "disarm",
    ]
