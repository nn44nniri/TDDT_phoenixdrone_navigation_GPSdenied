from __future__ import annotations
from pathlib import Path
from tddt_tdoc import TDOCConfig
from tddt_tdoc.registry import LayerRegistry

def test_config_version():
    cfg=TDOCConfig.from_yaml(Path(__file__).parents[1]/"configs/tdoc.yaml")
    assert cfg.orchestration_version=="TDOC-runtime-v1"
    assert cfg.get("package.version")=="2026.9.11.2052"
    assert cfg.layer_enabled(2) and cfg.layer_enabled(3) and cfg.layer_enabled(4)
    assert cfg.layer_enabled(5) and cfg.layer_enabled(6) and cfg.layer_enabled(7)

def test_registry_has_all_seven_layers():
    cfg=TDOCConfig.from_yaml(Path(__file__).parents[1]/"configs/tdoc.yaml")
    r=LayerRegistry(cfg); d=r.build()
    assert set(d)==set(map(str,range(1,8)))
    assert d["1"]["implementation"]=="physical/event ingress"
    assert d["5"]["implemented"] is True
    assert d["6"]["implemented"] is True
    assert d["7"]["implemented"] is True
