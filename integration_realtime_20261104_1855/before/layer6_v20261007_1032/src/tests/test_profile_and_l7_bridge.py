from pathlib import Path

from layer6.feedback import build_layer7_input

ROOT = Path(__file__).resolve().parents[2]


def test_no_script_writes_user_shell_profiles():
    offenders=[]
    for p in ROOT.rglob('*'):
        if not p.is_file() or p.suffix not in {'.sh','.py'} or p == Path(__file__):
            continue
        text=p.read_text(encoding='utf-8',errors='ignore')
        # Mentions are allowed for documentation/invariant comments; actual write/edit patterns are not.
        bad = ('sed -i' in text and ('.bashrc' in text or '.profile' in text)) or \
              ('>> "$HOME/.bashrc"' in text) or ('>> "$HOME/.profile"' in text) or \
              ('> "$HOME/.bashrc"' in text) or ('> "$HOME/.profile"' in text)
        if bad: offenders.append(str(p.relative_to(ROOT)))
    assert offenders == []


def test_layer7_evidence_preserves_pilot_domain_and_current_state():
    l5={
        'transaction_id':'cycle-42',
        'u_cmd':[0.1,-0.05,0.55,0.0],
        'C_exec':{'direct_pwm':False},
        'S_safe':{'psf':True,'rta':True},
    }
    status={
        'mavlink_connected':True,'mode':'QHOVER','armed':True,'landed_state':'IN_AIR',
        'airspeed_m_s':12.5,'groundspeed_m_s':11.9,'vertical_speed_m_s':0.2,'heading_deg':45.0,
        'latitude_deg':40.0,'longitude_deg':-105.0,'altitude_msl_m':1700.0,'relative_altitude_m':20.0,
        'attitude':{'roll':0.1,'pitch':-0.02,'yaw':0.7,'roll_rate':0.01,'pitch_rate':0.02,'yaw_rate':0.03},
        'battery':{'voltage_v':22.2,'current_a':4.1,'remaining_percent':88},
        'applied_pilot_command':{'aileron':0.1,'elevator':-0.05,'throttle':0.55,'rudder':0.0},
        'servo_output_raw':{'servo1_raw':1500},
        'telemetry_buffer_samples':120,'telemetry_latest_types':['ATTITUDE','VFR_HUD'],
    }
    out=build_layer7_input(l5,status,transaction_id='cycle-42',intake_ack={'ok':True})
    ev=out['ExecutionEvidence']
    assert ev['u_cmd_pilot']['throttle']==0.55
    assert ev['u_applied_pilot']['throttle']==0.55
    assert ev['x_obs']['mode']=='QHOVER'
    assert ev['current_state']['state_quality']['telemetry_buffer_samples']==120
    assert out['PredictionEvidence'] is None
    assert out['NetworkSnapshot'] is None


def test_independent_tddt_demo_is_python_and_uses_client():
    p=ROOT/'src'/'demo_tddt_l5_l7.py'
    text=p.read_text(encoding='utf-8')
    assert 'from layer6 import Layer6Client' in text
    assert 'from demo_layer6' not in text
    assert 'import demo_layer6' not in text
    assert '--layer5-file' in text
    assert '--layer5-listen' in text
    assert '--layer7-output' in text
    assert '--layer7-endpoint' in text
