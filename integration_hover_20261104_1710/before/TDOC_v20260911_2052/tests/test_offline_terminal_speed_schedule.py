from pathlib import Path
import importlib.util


ROOT = Path(__file__).resolve().parents[1]


def _mod():
    spec = importlib.util.spec_from_file_location(
        "wf_terminal_speed", ROOT / "scripts" / "run_training_workflows.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_terminal_speed_reduces_inside_prior_orbit_radius_without_changing_capture_gate():
    module = _mod()
    speed = module._offline_terminal_reposition_airspeed
    assert speed(300.0) == 8.0
    assert speed(200.0) == 8.0
    assert speed(100.0) == 4.0
    assert speed(50.0) == 2.0
    assert speed(8.0) == 2.0
    assert speed(50.0, minimum_m_s=3.0) == 3.0
    assert speed(8.0, minimum_m_s=3.0) == 3.0
    assert module._offline_terminal_phase(
        horizontal_m=8.01, agl_m=12.0, capture_m=8.0, terminal_captured=False
    ) == ("transition_back", "terminal_reposition", False)


def test_vertical_landing_starts_after_horizontal_capture_under_landing_corridor():
    module = _mod()
    phase = module._offline_terminal_phase
    assert phase(
        horizontal_m=6.0, agl_m=91.0, capture_m=8.0, capture_agl_m=12.0,
        terminal_captured=False,
    ) == ("vertical_landing", None, True)
    assert phase(
        horizontal_m=6.0, agl_m=12.0, capture_m=8.0, capture_agl_m=12.0,
        terminal_captured=False,
    ) == ("vertical_landing", None, True)


def test_terminal_reposition_agl_comes_from_unchanged_layer4_floor_and_tube():
    module = _mod()
    from tddt_tdoc.config import TDOCConfig
    cfg = TDOCConfig.from_yaml(ROOT / "configs" / "tdoc.yaml")
    contract = module._offline_terminal_reposition_agl(cfg, 12.0)
    assert contract["min_terrain_clearance_m"] == 30.0
    assert contract["tube_radius_max_m"] == 20.0
    assert contract["transition_min_airspeed_m_s"] == 3.0
    assert contract["resolved_agl_m"] == 50.0
    assert contract["requested_agl_m"] == 12.0
