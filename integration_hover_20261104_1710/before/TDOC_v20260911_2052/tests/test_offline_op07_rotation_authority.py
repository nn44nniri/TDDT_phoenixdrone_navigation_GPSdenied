from pathlib import Path
import importlib.util


ROOT = Path(__file__).resolve().parents[1]


def _mod():
    spec = importlib.util.spec_from_file_location(
        "wf_op07_rotation_authority", ROOT / "scripts" / "run_training_workflows.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_op07_hold_releases_rotation_not_advanced_by_its_model():
    module = _mod()
    plant = object.__new__(module.OfflineWingMPETPlant)
    rotation = {
        "qw": 0.956738,
        "qx": -0.185800,
        "qy": -0.096320,
        "qz": 0.202121,
        "p_rate": -0.295,
        "q_rate": 0.224,
        "r_rate": -0.198,
    }
    plant.state = {"x": 1.0, "y": 2.0, "h": 100.0, **rotation}
    plant.cf = {"x": 1.0, "y": 2.0, "h": 100.0, **rotation}

    assert plant._release_unmodeled_rotation() is True
    for state in (plant.state, plant.cf):
        assert state == {"x": 1.0, "y": 2.0, "h": 100.0}


def test_release_is_idempotent_and_does_not_change_translation():
    module = _mod()
    plant = object.__new__(module.OfflineWingMPETPlant)
    plant.state = {"x": 3.0, "y": 4.0, "h": 101.0}
    plant.cf = dict(plant.state)

    assert plant._release_unmodeled_rotation() is False
    assert plant.state == plant.cf == {"x": 3.0, "y": 4.0, "h": 101.0}
