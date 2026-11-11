import json
import socket
import threading
from layer6.client import Layer6Client, Layer6RemoteError


def _server(sock, reply):
    data, peer = sock.recvfrom(65535)
    obj = json.loads(data.decode())
    reply["packet"] = obj
    sock.sendto(json.dumps({"ok": True, "transaction_id": obj.get("transaction_id"), "intake_request_id": obj.get("intake_request_id")}).encode(), peer)


def test_external_client_submit_command_roundtrip():
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    host, port = srv.getsockname()
    seen = {}
    t = threading.Thread(target=_server, args=(srv, seen), daemon=True)
    t.start()
    with Layer6Client(f"{host}:{port}", timeout=1.0) as client:
        ack = client.submit_command([0.1, -0.2, 0.55, 0.0], mode="QHOVER", safety={"psf": True, "rta": True})
    t.join(timeout=1)
    srv.close()
    assert ack.ok
    assert seen["packet"]["u_cmd"] == [0.1, -0.2, 0.55, 0.0]
    assert seen["packet"]["C_exec"]["direct_pwm"] is False
    assert seen["packet"]["execution_intent"]["requested_pilot_mode"] == "QHOVER"


def test_external_client_does_not_invent_safety_evidence():
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    host, port = srv.getsockname()
    seen = {}
    t = threading.Thread(target=_server, args=(srv, seen), daemon=True)
    t.start()
    with Layer6Client(f"{host}:{port}", timeout=1.0) as client:
        client.submit_command([0, 0, 0, 0])
    t.join(timeout=1)
    srv.close()
    assert seen["packet"]["S_safe"] == {}
