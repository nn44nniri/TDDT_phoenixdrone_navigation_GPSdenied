from __future__ import annotations
from pathlib import Path
import yaml
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter

ROOT=Path(__file__).parents[1]

class _Cfg:
    package_root=ROOT

class _L5: pass

def test_current_online_policy_references_native_gui_and_preserves_layer5_command_authority():
    from tddt_tdoc.config import TDOCConfig
    cfg=TDOCConfig.from_yaml(ROOT/'configs/tdoc.yaml')
    assert cfg.get('layers.layer6.online.transport')=='Layer6Client_GUI_MAVLink'
    assert cfg.get('layers.layer6.online.direct_pwm') is False
    obj=yaml.safe_load((ROOT/'configs/online_guidance_sequence.yaml').read_text())
    assert obj['reference']['package']=='layer6_v20261007_1032'
    assert obj['reference'].get('random_demo_commands_authoritative') is not True
    assert obj['reference'].get('random_command_envelope_import') is not True
    assert Layer6OnlineAdapter.SOURCE=='Layer6/GUI/MAVLink'
    assert Layer6OnlineAdapter.MODES==('ONLINE_JSBSIM',)


def test_native_layer6_has_only_layer5_final_and_no_ptlib_operational_demo_limits():
    import inspect
    src=inspect.getsource(Layer6OnlineAdapter.invoke)
    assert 'gates' in src and 'FINAL' in src
    assert 'verify_native_execution_evidence' in src
    assert 'native_execution_model_not_verified' in src
    assert 'Layer5 FINAL requires complete layer6_package' in src
    assert 'direct_pwm' in src or 'DecisionEnvelope' in src
    assert '_make_ptlib' not in src
    assert 'random_demo_command_authority' not in src
    assert 'min_throttle' not in src and 'max_throttle' not in src

