from pathlib import Path

import tddt_layer7
from tddt_layer7.config import load_config


def test_packaging_files_exist():
    root = Path(__file__).resolve().parents[1]
    assert (root / "pyproject.toml").is_file()
    assert (root / "setup.py").is_file()


def test_version_bumped():
    assert tddt_layer7.__version__ == "0.3.0"


def test_default_config_resolves_from_project_root(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg = load_config("configs/default.json")
    assert cfg.tdoc_managed is True
    assert cfg.direct_actuation_allowed is False
