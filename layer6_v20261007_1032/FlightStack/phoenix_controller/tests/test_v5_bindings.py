from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / 'config' / 'f310_phoenix.yaml').read_text(encoding='utf-8'))


def test_version():
    assert CFG['version'] == 5


def test_gamepad_actions():
    b = CFG['bindings']['gamepad_buttons']
    assert b['a'] == 'qhover'
    assert b['b'] == 'fbwa'
    assert b['x'] == 'qstabilize'
    assert b['y'] == 'qloiter'
    assert b['start'] == 'arm'
    assert b['back'] == 'disarm'
    assert b['lb'] == 'throttle_step_down'
    assert b['rb'] == 'throttle_step_up'


def test_keyboard_actions():
    b = CFG['bindings']['keyboard']
    assert b['1'] == 'qstabilize'
    assert b['2'] == 'qhover'
    assert b['3'] == 'fbwa'
    assert b['4'] == 'qloiter'
    assert b['W'] == 'pitch_positive'
    assert b['R'] == 'throttle_increase'


def test_action_kinds():
    a = CFG['actions']
    assert a['arm']['kind'] == 'arm'
    assert a['disarm']['kind'] == 'disarm'
    assert a['qhover']['value'] == 'QHOVER'
    assert a['fbwa']['value'] == 'FBWA'
    assert a['throttle_step_up']['delta'] > 0
    assert a['throttle_step_down']['delta'] < 0


def test_calibrated_runtime_axes_preserved():
    a = CFG['axes']
    assert a['left_x'] == 0 and a['left_y'] == 1
    assert a['left_trigger'] == 2
    assert a['right_x'] == 3 and a['right_y'] == 4
    assert a['right_trigger'] == 5
