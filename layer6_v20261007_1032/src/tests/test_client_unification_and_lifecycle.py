from pathlib import Path
import inspect

from layer6.client import Layer6Client, build_demo_parser
import run_flightstack as runner

ROOT = Path(__file__).resolve().parents[2]


def test_demo_entrypoint_is_fully_client_backed():
    text = (ROOT / "src" / "demo_layer6.py").read_text()
    assert "layer6.client import demo_main" in text
    assert "DigitalPilotExecutor" not in text
    assert "DecisionEnvelope" not in text


def test_start_if_needed_exists_in_demo_and_library_api():
    opts = {a.dest for a in build_demo_parser()._actions}
    assert "start_if_needed" in opts
    assert "start_if_needed" in inspect.signature(Layer6Client).parameters


def test_ensure_stack_started_starts_only_when_missing(monkeypatch):
    c = Layer6Client()
    state = {"started": False, "calls": 0}
    def status(timeout=0.4):
        state["calls"] += 1
        return {"type": "status"} if state["started"] else None
    def start():
        state["started"] = True
    monkeypatch.setattr(c, "gui_status", status)
    monkeypatch.setattr(c, "start_stack", start)
    try:
        st = c.ensure_stack_started(timeout=0.3)
        assert st == {"type": "status"}
        assert state["started"] is True
    finally:
        c.close()


def test_runner_delegates_stop_to_owned_native_session():
    from layer6 import native_session
    text = Path(runner.__file__).read_text()
    owned = Path(native_session.__file__).read_text()
    assert "native.stop" in text
    assert "native_session_id" in owned or "session_id" in owned
    assert "start_ticks" in owned
    assert "signal.SIGTERM" in owned and "signal.SIGKILL" in owned
    assert "pkill" not in text and "pkill" not in owned


def test_demo_rate_hz_is_forwarded_to_service():
    text = (ROOT / "src" / "layer6" / "client.py").read_text()
    service = (ROOT / "src" / "layer6" / "service.py").read_text()
    assert "execution_rate_hz=rate_hz" in text
    assert "requested_rate=obj.get('execution_rate_hz')" in service

def test_wait_fcu_ready_ignores_initialising_then_accepts_stable_manual(monkeypatch):
    c = Layer6Client()
    seq = [
        {"mavlink_connected": True, "target_system": 1, "mode": "INITIALISING", "armed": False},
        {"mavlink_connected": True, "target_system": 1, "mode": "MANUAL", "armed": False},
        {"mavlink_connected": True, "target_system": 1, "mode": "MANUAL", "armed": False},
        {"mavlink_connected": True, "target_system": 1, "mode": "MANUAL", "armed": False},
    ]
    def status(timeout=0.5):
        return seq.pop(0) if seq else {"mavlink_connected": True, "target_system": 1, "mode": "MANUAL", "armed": False}
    monkeypatch.setattr(c, "gui_status", status)
    monkeypatch.setattr("layer6.client.time.sleep", lambda *_: None)
    try:
        st = c.wait_fcu_ready(timeout=1.0, stable_samples=3)
        assert st["mode"] == "MANUAL"
    finally:
        c.close()


def test_mode_retry_re_submits_through_canonical_client(monkeypatch):
    c = Layer6Client()
    calls = {"submit": 0, "wait": 0}
    monkeypatch.setattr(c, "submit_command", lambda *a, **k: calls.__setitem__("submit", calls["submit"] + 1))
    def wait(pred, timeout=1, interval=0.1):
        calls["wait"] += 1
        st = {"mavlink_connected": True, "mode": "MANUAL" if calls["wait"] == 1 else "QHOVER"}
        return st
    monkeypatch.setattr(c, "wait_for_status", wait)
    try:
        st = c._verify_mode_with_retry(
            mode="QHOVER", command=[0,0,0,0], discrete=["THROTTLE_ZERO"],
            hold_s=0, verify_timeout=0.1, transaction_prefix="t", rate_hz=30, retries=3,
        )
        assert st["mode"] == "QHOVER"
        assert calls["submit"] == 1
    finally:
        c.close()


def test_arm_retry_re_submits_normal_arm_and_confirms(monkeypatch):
    from layer6.client import Layer6Client
    c = Layer6Client.__new__(Layer6Client)
    submitted=[]
    seq=[
        {"mavlink_connected":True,"armed":False,"mode":"QHOVER","last_command_ack":{"command":400,"result":4},"last_statustext":"PreArm: waiting"},
        {"mavlink_connected":True,"armed":True,"mode":"QHOVER","last_command_ack":{"command":400,"result":0},"last_statustext":None},
    ]
    c.gui_status=lambda timeout=0.2: seq[0]
    def wait_for_status(pred, *, timeout=8.0, interval=0.15):
        return seq.pop(0) if seq else {"mavlink_connected":True,"armed":True,"mode":"QHOVER"}
    c.wait_for_status=wait_for_status
    c.submit_command=lambda *args, **kwargs: submitted.append((args,kwargs))
    monkeypatch.setattr('time.sleep', lambda *_: None)
    st=c._verify_arm_with_retry(command=[0,0,0,0],hold_s=0,verify_timeout=0.01,
                                transaction_prefix='demo-arm',rate_hz=30,retries=3,retry_delay=0)
    assert st['armed'] is True
    assert submitted and submitted[0][1]['discrete']==['ARM']
    assert submitted[0][1]['provenance']['reason']=='arm_retry'


def test_status_format_includes_ack_and_statustext():
    from layer6.client import _fmt_status
    s=_fmt_status({"mavlink_connected":True,"target_system":1,"mode":"QHOVER","armed":False,
                   "throttle":0.0,"landed_state":"ON_GROUND","groundspeed_m_s":0.0,
                   "relative_altitude_m":0.0,"vertical_speed_m_s":0.0,
                   "last_command_ack":{"command":400,"result":4},"last_statustext":"PreArm: test",
                   "mavlink_endpoint":"udp:test"})
    assert 'command' in s and 'PreArm: test' in s
