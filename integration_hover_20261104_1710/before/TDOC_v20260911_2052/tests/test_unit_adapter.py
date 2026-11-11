from __future__ import annotations
import os
from pathlib import Path
import pytest
from tddt_tdoc import TDOCConfig
from tddt_tdoc.unit_adapter import UnitAdapterBinding


def test_layer3_adapter_explicitly_binds_tdoc_owned_iso_root():
    text=(Path(__file__).parents[1]/"src/tddt_tdoc/adapters/layer3.py").read_text(encoding="utf-8")
    assert "bind_layer3_config" in text
    assert "cfg.paths.tdoc_root" not in text or "bind_layer3_config(cfg)" in text
    assert "Layer3FMURuntime(" not in text


def test_unit_adapter_uses_single_authoritative_root(tmp_path, monkeypatch):
    expected=tmp_path/UnitAdapterBinding.EXPECTED_BASENAME
    expected.mkdir()
    for rel in UnitAdapterBinding.REQUIRED_FILES:
        p=expected/rel; p.parent.mkdir(parents=True,exist_ok=True); p.write_text("# test\n",encoding="utf-8")
    class Cfg:
        def path_value(self,key):
            return expected if key=="TDDT_UNIT_ADAPTER_ROOT" else None
    b=UnitAdapterBinding(Cfg())
    monkeypatch.setattr(b,"_probe_subprocess",lambda root:{"ok":True,"version":"2.2.0","version_ok":True,"api_ok":True,"smoke_ok":True,"missing_symbols":[]})
    pf=b.preflight(refresh=True)
    assert pf["root"] == str(expected)
    assert "legacy_tdoc_root" not in pf
    assert "alias_consistent" not in pf
    assert pf["ok"] is True


def test_activate_does_not_set_tddt_tdoc_root(tmp_path, monkeypatch):
    expected=tmp_path/UnitAdapterBinding.EXPECTED_BASENAME
    expected.mkdir()
    for rel in UnitAdapterBinding.REQUIRED_FILES:
        p=expected/rel; p.parent.mkdir(parents=True,exist_ok=True); p.write_text("# test\n",encoding="utf-8")
    class Cfg:
        def path_value(self,key):
            return expected if key=="TDDT_UNIT_ADAPTER_ROOT" else None
    b=UnitAdapterBinding(Cfg())
    monkeypatch.setattr(b,"preflight",lambda refresh=False:{"ok":True,"root":str(expected)})
    # Avoid a real import; only verify environment semantics at source level.
    text=(Path(__file__).parents[1]/"src/tddt_tdoc/unit_adapter.py").read_text(encoding="utf-8")
    assert 'os.environ["TDDT_TDOC_ROOT"]' not in text


def test_real_unit_adapter_api_when_configured():
    root=os.environ.get("TDDT_UNIT_ADAPTER_ROOT")
    if not root or not Path(root).exists():
        pytest.skip("external ISO adapter not configured")
    cfg=TDOCConfig.from_yaml(Path(__file__).parents[1]/"configs/tdoc.yaml")
    pf=UnitAdapterBinding(cfg).preflight(refresh=True)
    assert pf["ok"] is True
    assert pf["api_probe"]["version"] == "2.2.0"
    assert pf["api_probe"]["missing_symbols"] == []
    assert pf["api_probe"]["smoke_ok"] is True


def test_unit_adapter_not_vendored():
    root=Path(__file__).parents[1]
    assert not (root/"src/tdoc_unit_converter").exists()
    assert not any(p.name.startswith("TDOC_ISO_Universal_Unit_Conversion_Standard") and p.is_dir() for p in root.rglob("*"))


def test_legacy_tdoc_root_environment_is_not_consumed_by_binding():
    text=(Path(__file__).parents[1]/"src/tddt_tdoc/unit_adapter.py").read_text(encoding="utf-8")
    assert 'path_value("TDDT_TDOC_ROOT")' not in text
