"""The actual PhoenixGUI as a telemetry-only panel for isolated validation.

Its UDP master has no FCU return path. Control widgets are disabled and all
virtual input/output is inhibited; the ordinary production GUI is unchanged.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import time


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--layer6-root', type=Path, required=True)
    p.add_argument('--port', type=int, required=True)
    p.add_argument('--ipc-port', type=int, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--session-id', required=True)
    a = p.parse_args()
    if a.session_id != os.environ.get('TDDT_NATIVE_SESSION_ID') or not 21200 <= a.port <= 21800 or not 22200 <= a.ipc_port <= 22800:
        p.error('owned isolated GUI endpoints/session required')
    root = a.layer6_root.resolve(strict=True)
    sys.path.insert(0, str(root/'src/gui'))
    spec = importlib.util.spec_from_file_location('phoenix_reference_gui_source', root/'src/gui/phoenix_f310_gui.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication, QLabel
    from .native_session import write
    cfg = module.load_config(str(root/'gui/config/f310_phoenix.yaml'))
    cfg['layer6']['virtual_pilot_port'] = a.ipc_port

    class ObservationGUI(module.PhoenixGUI):
        def _send_pilot_command(self, *args):
            return False
        def _poll_virtual_pilot(self):
            return None
        def _dispatch_action(self, *args):
            return None

    app = QApplication(sys.argv)
    gui = ObservationGUI(cfg, 'udpin:127.0.0.1:%s' % a.port, True)
    gui.setWindowTitle('Phoenix validation GUI '+a.session_id[:8]+' - observation only')
    gui.resize(1000, 750)
    gui.move(0, 0)
    for widget in list(gui.action_buttons.values())+[gui.throttle_slider, gui.chk_keyboard, gui.chk_gamepad]:
        widget.setEnabled(False)
    label = QLabel('FCU evaluation only; waiting for this branch telemetry')
    gui.centralWidget().layout().addWidget(label)
    gui.show()
    gui.connect_mavlink()
    last_stamp = [None]
    def observe():
        link = gui.link
        if link is None:
            return
        master = link.master
        msg = master.messages.get('GLOBAL_POSITION_INT')
        if msg is None or msg.time_boot_ms == last_stamp[0]:
            return
        receipt = link.status_observations.get('GLOBAL_POSITION_INT')
        if receipt is None or not receipt['valid'] or receipt['fields']['time_boot_ms'] != msg.time_boot_ms:
            return
        last_stamp[0] = msg.time_boot_ms
        lat, lon = msg.lat/1e7, msg.lon/1e7
        label.setText('FCU evaluation only (not MESA) | %.7f, %.7f | source %.3f s | DISARMED reference' % (lat, lon, msg.time_boot_ms/1000.))
        rc = master.messages.get('RC_CHANNELS')
        if rc is not None:
            def stick(channel):
                return max(-1., min(1., (getattr(rc, 'chan%d_raw' % channel)-1500)/400))
            gui.left_stick.set_xy(stick(1), -stick(2))
            gui.right_stick.set_xy(stick(4), 0.)
        value = {'session_id': a.session_id, 'pid': os.getpid(),
              'visible': gui.isVisible(), 'connected': link.master is not None,
              'platform': app.platformName(), 'mode': link.mode, 'armed': link.armed,
              'source': 'MAVLink/GLOBAL_POSITION_INT; FCU evaluation only',
              'source_fcu_boot_s': msg.time_boot_ms/1000.,
              'received_monotonic_s': receipt['timestamp_monotonic'], 'observed_monotonic_s': time.monotonic(),
              'latitude_deg': lat, 'longitude_deg': lon,
              'control_output_inhibited': True, 'mission_control_forwarding': False}
        write(a.output, value)
        with a.output.with_suffix('.jsonl').open('a') as stream:
            stream.write(json.dumps(value,allow_nan=False)+'\n')
    timer = QTimer(gui)
    timer.timeout.connect(observe)
    timer.start(100)
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
