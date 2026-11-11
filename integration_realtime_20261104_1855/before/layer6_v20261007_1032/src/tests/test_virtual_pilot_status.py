import json
import socket
import threading

from layer6.virtual_pilot import VirtualPilotClient


def _server(sock, count=1):
    for _ in range(count):
        data, addr = sock.recvfrom(65535)
        msg = json.loads(data.decode())
        if msg.get("type") == "status_request":
            payload = {"type":"status", "schema":"Layer6-GUI-status-v1", "schema_version":1, "request_id":msg["request_id"], "mavlink_connected":True, "mode":"QHOVER", "armed":False, "target_system":1}
            sock.sendto(json.dumps(payload).encode(), addr)


def test_status_roundtrip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    th = threading.Thread(target=_server, args=(s,1), daemon=True); th.start()
    c = VirtualPilotClient(port=port)
    try:
        st = c.status(timeout=1.0)
        assert st["mavlink_connected"] is True
        assert st["mode"] == "QHOVER"
        assert st["armed"] is False
    finally:
        c.sock.close(); s.close()


def test_connect_mavlink_packet():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    got = {}
    def recv():
        data,_ = s.recvfrom(65535); got.update(json.loads(data.decode()))
    th = threading.Thread(target=recv, daemon=True); th.start()
    c = VirtualPilotClient(port=port)
    try:
        c.connect_mavlink(); th.join(1.0)
        assert got.get("type") == "connect_mavlink"
    finally:
        c.sock.close(); s.close()
