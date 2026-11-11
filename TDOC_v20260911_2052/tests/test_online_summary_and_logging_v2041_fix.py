from pathlib import Path
ROOT=Path(__file__).parents[1]

def test_online_summary_is_atomic_run_scoped_and_mirrored():
    s=(ROOT/'scripts/run_training_workflows.py').read_text()
    assert "TDOC-training-workflows-v6" in s
    assert "run_id" in s and "uuid.uuid4" in s
    assert "ONLINE_TRAINING_WORKFLOW_SUMMARY.json" in s
    assert "os.replace(tmp,target)" in s
    assert "_write_workflow_summary(overall,report_root,online_training=bool(args.online_training))" in s

def test_native_config_uses_gui_mavlink_and_no_ptlib_log_authority():
    from tddt_tdoc.config import TDOCConfig
    from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter
    cfg=TDOCConfig.from_yaml(ROOT/'configs/tdoc.yaml')
    assert cfg.get('layers.layer6.online.transport')=='Layer6Client_GUI_MAVLink'
    assert cfg.get('layers.layer6.online.direct_pwm') is False
    assert cfg.get('layers.layer6.online.execution_rate_hz')==30.0
    src=(ROOT/'src/tddt_tdoc/adapters/layer6.py').read_text()
    assert 'Layer6/GUI/MAVLink' in src
    assert '_get_client' in src and 'gui_status' in src
    assert '_ptlib_log_target' not in src
    assert '_open_runtime_session' not in src

