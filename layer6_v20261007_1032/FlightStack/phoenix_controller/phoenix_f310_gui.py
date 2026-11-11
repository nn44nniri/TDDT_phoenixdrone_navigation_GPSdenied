#!/usr/bin/env python3
"""Phoenix VTOL RC GUI v5 for Logitech F310 + keyboard.

Key v5 changes:
- Linux/jstest-gtk F310 mapping matching the user's observed device.
- Independent live LT and RT analog trigger gauges.
- Separate Left Stick, Right Stick and D-Pad visualizations.
- Live digital-button indicators for A/B/X/Y/LB/RB/BACK/START/MODE/L3/R3.
- YAML-driven gamepad/keyboard action bindings with live Flight mode/action feedback.
- START/BACK hold actions drive ARM/DISARM and visually press their GUI action buttons.
- Terminal logging prints each input name and its configured YAML function.
- Pilot-domain MAVLink MANUAL_CONTROL only; ArduPilot keeps stabilization/mixing.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

import pygame
import yaml
from pymavlink import mavutil
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QKeyEvent, QPainter, QPen, QBrush, QColor
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QMainWindow, QMessageBox, QProgressBar, QPushButton, QSlider,
    QVBoxLayout, QWidget,
)

from phoenix_f310_utils import clamp, shape_axis, trigger_fraction


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ValueError("configuration root must be a mapping")
    return cfg


def get_axis_safe(js: pygame.joystick.Joystick, idx: int) -> float:
    return float(js.get_axis(idx)) if 0 <= idx < js.get_numaxes() else 0.0


def get_button_safe(js: pygame.joystick.Joystick, idx: int) -> int:
    return int(js.get_button(idx)) if 0 <= idx < js.get_numbuttons() else 0


def get_hat_safe(js: pygame.joystick.Joystick, idx: int) -> Tuple[int, int]:
    if 0 <= idx < js.get_numhats():
        h = js.get_hat(idx)
        return int(h[0]), int(h[1])
    return 0, 0


class MavlinkLink:
    def __init__(self, cfg: Dict[str, Any], override_connection: Optional[str] = None):
        mcfg = cfg["mavlink"]
        self.connection_string = override_connection or str(mcfg["connection"])
        self.source_system = int(mcfg.get("source_system", 255))
        self.source_component = int(mcfg.get("source_component", 191))
        self.heartbeat_timeout = float(mcfg.get("heartbeat_timeout_seconds", 10))
        self.master = None
        self.target_system = 0
        self.target_component = 1
        self.armed: Optional[bool] = None
        self.mode = "—"

    def connect(self) -> None:
        self.master = mavutil.mavlink_connection(
            self.connection_string,
            source_system=self.source_system,
            source_component=self.source_component,
            autoreconnect=True,
        )
        hb = self.master.wait_heartbeat(timeout=self.heartbeat_timeout)
        if hb is None:
            raise TimeoutError(f"no HEARTBEAT within {self.heartbeat_timeout:.1f}s")
        self.target_system = int(self.master.target_system)
        self.target_component = int(self.master.target_component or 1)
        self._update_heartbeat(hb)

    def _update_heartbeat(self, msg) -> None:
        try:
            self.armed = bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
            mapping = self.master.mode_mapping() if self.master else {}
            reverse = {v: k for k, v in (mapping or {}).items()}
            self.mode = reverse.get(int(msg.custom_mode), str(int(msg.custom_mode)))
        except Exception:
            pass

    def send_manual(self, pitch: int, roll: int, throttle: int, yaw: int) -> None:
        if not self.master:
            return
        self.master.mav.manual_control_send(
            self.target_system,
            int(clamp(pitch, -1000, 1000)),
            int(clamp(roll, -1000, 1000)),
            int(clamp(throttle, 0, 1000)),
            int(clamp(yaw, -1000, 1000)),
            0,
        )

    def set_mode(self, name: str) -> bool:
        if not self.master:
            return False
        mapping = self.master.mode_mapping() or {}
        name = name.upper()
        if name not in mapping:
            return False
        self.master.mav.set_mode_send(
            self.target_system,
            mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
            mapping[name],
        )
        return True

    def arm(self) -> None:
        self._arm(False, True)

    def disarm(self) -> None:
        self._arm(False, False)

    def _arm(self, force: bool, arm: bool) -> None:
        if not self.master:
            return
        self.master.mav.command_long_send(
            self.target_system,
            self.target_component,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
            0,
            1 if arm else 0,
            21196 if force else 0,
            0, 0, 0, 0, 0,
        )

    def drain(self) -> None:
        if not self.master:
            return
        while True:
            msg = self.master.recv_match(blocking=False)
            if msg is None:
                break
            if msg.get_type() == "HEARTBEAT":
                self._update_heartbeat(msg)


class LinkThread(QThread):
    connected = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, cfg, endpoint):
        super().__init__()
        self.cfg = cfg
        self.endpoint = endpoint

    def run(self):
        try:
            link = MavlinkLink(self.cfg, self.endpoint)
            link.connect()
            self.connected.emit(link)
        except Exception as exc:
            self.failed.emit(str(exc))


class StickWidget(QWidget):
    def __init__(self, title: str, x_label: str, y_label: str):
        super().__init__()
        self.x = 0.0
        self.y = 0.0
        self.title = title
        self.x_label = x_label
        self.y_label = y_label
        self.setMinimumSize(220, 230)

    def set_xy(self, x: float, y: float):
        self.x = clamp(float(x), -1.0, 1.0)
        self.y = clamp(float(y), -1.0, 1.0)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        p.drawText(8, 18, self.title)
        p.drawText(8, rect.height() - 8, f"X {self.x_label}: {self.x:+.2f}   Y {self.y_label}: {self.y:+.2f}")
        size = min(rect.width() - 30, rect.height() - 65)
        left = (rect.width() - size) / 2
        top = 30
        p.setPen(QPen(Qt.GlobalColor.black, 2))
        p.setBrush(QBrush(QColor(245, 245, 245)))
        p.drawEllipse(int(left), int(top), int(size), int(size))
        cx = left + size / 2
        cy = top + size / 2
        p.setPen(QPen(QColor(140, 140, 140), 1))
        p.drawLine(int(cx), int(top), int(cx), int(top + size))
        p.drawLine(int(left), int(cy), int(left + size), int(cy))
        px = cx + self.x * size * 0.42
        py = cy - self.y * size * 0.42
        p.setPen(QPen(Qt.GlobalColor.black, 2))
        p.setBrush(QBrush(QColor(60, 145, 230)))
        p.drawEllipse(int(px - 8), int(py - 8), 16, 16)


class DPadWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.x = 0
        self.y = 0
        self.setMinimumSize(180, 230)

    def set_hat(self, x: int, y: int):
        self.x, self.y = int(x), int(y)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.drawText(8, 18, "D-Pad / Hat")
        cx, cy = self.width() // 2, 112
        cell = 42
        labels = {(0, -1): "↓", (0, 1): "↑", (-1, 0): "←", (1, 0): "→", (0, 0): "•"}
        for dx, dy in [(0,1),(-1,0),(0,0),(1,0),(0,-1)]:
            x = cx + dx * cell - cell//2
            y = cy - dy * cell - cell//2
            active = (dx,dy) == (self.x,self.y) and (dx,dy) != (0,0)
            p.setBrush(QBrush(QColor(60,145,230) if active else QColor(235,235,235)))
            p.setPen(QPen(Qt.GlobalColor.black, 1))
            p.drawRect(x, y, cell, cell)
            p.drawText(x, y, cell, cell, Qt.AlignmentFlag.AlignCenter, labels[(dx,dy)])
        p.drawText(8, self.height()-10, f"X={self.x:+d}  Y={self.y:+d}")


class ButtonIndicator(QPushButton):
    def __init__(self, label: str):
        super().__init__(label)
        self.setCheckable(False)
        self.setMinimumHeight(42)
        self.setEnabled(False)
        self.set_pressed(False)

    def set_pressed(self, pressed: bool):
        if pressed:
            self.setStyleSheet("font-weight:bold; background:#45a4ff; color:white; border:2px solid #1366a8;")
        else:
            self.setStyleSheet("font-weight:bold; background:#efefef; color:#222; border:1px solid #aaa;")


class PhoenixGUI(QMainWindow):
    def __init__(self, cfg: Dict[str, Any], endpoint: Optional[str], no_gamepad: bool):
        super().__init__()
        self.cfg = cfg
        self.endpoint = endpoint
        self.no_gamepad = no_gamepad
        self.link: Optional[MavlinkLink] = None
        self.link_thread = None
        self.joystick = None
        self.gamepad_ok = False
        self.keys = set()
        self.throttle = float(cfg["throttle"].get("initial", 0))
        self.prev_buttons: Dict[str, bool] = {}
        self.prev_axis_active: Dict[str, bool] = {}
        self.hold_started: Dict[str, float] = {}
        self.hold_fired: Dict[str, bool] = {}
        self.action_buttons: Dict[str, QPushButton] = {}
        self.last_t = time.monotonic()
        self.lt_fraction = 0.0
        self.rt_fraction = 0.0
        self.setWindowTitle("Phoenix VTOL RC v5 — Logitech F310 / Keyboard")
        self.resize(1220, 820)
        self._build_ui()
        self._init_gamepad()
        hz = int(cfg["controller"].get("update_hz", 30))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.control_tick)
        self.timer.start(max(10, int(1000 / max(1, hz))))

    def _build_ui(self):
        root = QWidget(); self.setCentralWidget(root)
        main = QVBoxLayout(root)

        status = QHBoxLayout()
        self.lbl_mav = QLabel("MAVLink: DISCONNECTED")
        self.lbl_pad = QLabel("F310: —")
        self.lbl_mode = QLabel("Mode: —")
        self.lbl_arm = QLabel("Arm: unknown")
        for w in (self.lbl_mav, self.lbl_pad, self.lbl_mode, self.lbl_arm):
            w.setFrameStyle(QFrame.Shape.Panel | QFrame.Shadow.Sunken)
            w.setMinimumHeight(30)
            status.addWidget(w)
        main.addLayout(status)

        conn = QHBoxLayout()
        self.btn_connect = QPushButton("Connect MAVLink")
        self.btn_connect.clicked.connect(self.connect_mavlink)
        conn.addWidget(self.btn_connect)
        conn.addWidget(QLabel(self.endpoint or str(self.cfg["mavlink"]["connection"])))
        conn.addStretch(1)
        main.addLayout(conn)

        # Three independent physical controls: left analog, right analog, D-pad.
        inputs = QHBoxLayout()
        self.left_stick = StickWidget("Left analog stick", "Roll", "Pitch")
        self.right_stick = StickWidget("Right analog stick", "Yaw", "Y/Aux")
        self.dpad = DPadWidget()
        inputs.addWidget(self.left_stick)
        inputs.addWidget(self.right_stick)
        inputs.addWidget(self.dpad)

        trig_box = QGroupBox("Analog triggers")
        trig_lay = QVBoxLayout(trig_box)
        self.lbl_lt = QLabel("LT  0%")
        self.lt_bar = QProgressBar(); self.lt_bar.setRange(0,1000); self.lt_bar.setFormat("LT  %p%")
        self.lbl_rt = QLabel("RT  0%")
        self.rt_bar = QProgressBar(); self.rt_bar.setRange(0,1000); self.rt_bar.setFormat("RT  %p%")
        trig_lay.addWidget(self.lbl_lt); trig_lay.addWidget(self.lt_bar)
        trig_lay.addWidget(self.lbl_rt); trig_lay.addWidget(self.rt_bar)
        trig_lay.addSpacing(16)
        trig_lay.addWidget(QLabel("Commanded throttle (persistent)"))
        self.throttle_slider = QSlider(Qt.Orientation.Horizontal); self.throttle_slider.setRange(0,1000)
        self.throttle_slider.setValue(int(self.throttle)); self.throttle_slider.valueChanged.connect(self.set_throttle)
        self.throttle_bar = QProgressBar(); self.throttle_bar.setRange(0,1000); self.throttle_bar.setValue(int(self.throttle)); self.throttle_bar.setFormat("Throttle %v / 1000")
        trig_lay.addWidget(self.throttle_slider); trig_lay.addWidget(self.throttle_bar)
        inputs.addWidget(trig_box)
        main.addLayout(inputs)

        # Live digital button state matching jstest-gtk numbering.
        digital = QGroupBox("F310 digital buttons — live state")
        grid = QGridLayout(digital)
        self.indicators = {}
        names = [
            ("a","A / Button 0"),("b","B / Button 1"),("x","X / Button 2"),("y","Y / Button 3"),
            ("lb","LB / Button 4"),("rb","RB / Button 5"),("back","BACK / Button 6"),("start","START / Button 7"),
            ("mode","MODE / Button 8"),("left_stick","L3 / Button 9"),("right_stick","R3 / Button 10"),
        ]
        for i,(key,label) in enumerate(names):
            w = ButtonIndicator(label); self.indicators[key] = w
            grid.addWidget(w, i//6, i%6)
        main.addWidget(digital)

        rc = QGroupBox("Keyboard / on-screen RC controls")
        rcgrid = QGridLayout(rc)
        rcgrid.addWidget(self._moment_button("W\nPitch +", Qt.Key.Key_W), 0,1)
        rcgrid.addWidget(self._moment_button("A\nRoll -", Qt.Key.Key_A), 1,0)
        rcgrid.addWidget(self._moment_button("S\nPitch -", Qt.Key.Key_S), 1,1)
        rcgrid.addWidget(self._moment_button("D\nRoll +", Qt.Key.Key_D), 1,2)
        rcgrid.addWidget(self._moment_button("Q\nYaw -", Qt.Key.Key_Q), 2,0)
        rcgrid.addWidget(self._moment_button("E\nYaw +", Qt.Key.Key_E), 2,2)
        rcgrid.addWidget(self._moment_button("R\nThrottle +", Qt.Key.Key_R), 3,0)
        rcgrid.addWidget(self._moment_button("F\nThrottle -", Qt.Key.Key_F), 3,2)
        main.addWidget(rc)

        modes = QGroupBox("Flight modes / actions")
        mg = QGridLayout(modes)
        # Buttons keep the v4 layout, but their labels/functions come from YAML actions/bindings.
        mode_specs = [("qstabilize","X"),("qhover","A"),("fbwa","B"),("qloiter","Y")]
        for col,(action_id,default_pad) in enumerate(mode_specs):
            adef = self.cfg.get("actions",{}).get(action_id,{})
            label = str(adef.get("label", adef.get("value", action_id.upper())))
            pad_name = self._gamepad_input_for_action(action_id) or default_pad
            b = QPushButton(f"{label}\nF310 {pad_name.upper()}")
            b.clicked.connect(lambda _=False,a=action_id:self._dispatch_action(a,"GUI",a))
            mg.addWidget(b,0,col); self.action_buttons[action_id]=b
        self.btn_arm = QPushButton("ARM"); self.btn_arm.clicked.connect(self.arm_confirm); self.action_buttons["arm"]=self.btn_arm
        self.btn_disarm = QPushButton("DISARM"); self.btn_disarm.clicked.connect(self.disarm_confirm); self.action_buttons["disarm"]=self.btn_disarm
        self.btn_neutral = QPushButton("NEUTRAL ATTITUDE"); self.btn_neutral.clicked.connect(self.neutral_attitude); self.action_buttons["neutral_attitude"]=self.btn_neutral
        self.btn_zero = QPushButton("THROTTLE ZERO"); self.btn_zero.clicked.connect(lambda:self._dispatch_action("throttle_zero","GUI","THROTTLE ZERO")); self.action_buttons["throttle_zero"]=self.btn_zero
        mg.addWidget(self.btn_arm,1,0); mg.addWidget(self.btn_disarm,1,1); mg.addWidget(self.btn_neutral,1,2); mg.addWidget(self.btn_zero,1,3)
        self.lbl_active_input = QLabel("Active input: —")
        self.lbl_active_function = QLabel("Mapped function: —")
        self.lbl_active_input.setFrameStyle(QFrame.Shape.Panel | QFrame.Shadow.Sunken)
        self.lbl_active_function.setFrameStyle(QFrame.Shape.Panel | QFrame.Shadow.Sunken)
        mg.addWidget(self.lbl_active_input,2,0,1,2); mg.addWidget(self.lbl_active_function,2,2,1,2)
        main.addWidget(modes)

        flags = QHBoxLayout()
        self.chk_gamepad = QCheckBox("Use Logitech F310"); self.chk_gamepad.setChecked(not self.no_gamepad)
        self.chk_keyboard = QCheckBox("Enable keyboard W/A/S/D + Q/E + R/F"); self.chk_keyboard.setChecked(True)
        flags.addWidget(self.chk_gamepad); flags.addWidget(self.chk_keyboard); flags.addStretch(1)
        main.addLayout(flags)

        self.lbl_values = QLabel("pitch=+0  roll=+0  throttle=0  yaw=+0   rightY=+0.00   dpad=(0,0)")
        self.lbl_values.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main.addWidget(self.lbl_values)
        help_txt = QLabel(
            "Runtime mapping is loaded from config/f310_phoenix.yaml. "
            "Gamepad and keyboard inputs are resolved through YAML actions/bindings; active input/function is shown above and printed to the terminal."
        )
        help_txt.setWordWrap(True); main.addWidget(help_txt)

    def _moment_button(self, text, key):
        b = QPushButton(text); b.setMinimumHeight(52)
        b.pressed.connect(lambda k=int(key): self._press_keyboard_key(k, source="GUI button"))
        b.released.connect(lambda k=int(key): self._release_keyboard_key(k))
        return b

    def _gamepad_input_for_action(self, action_id: str) -> Optional[str]:
        for name, aid in (self.cfg.get("bindings",{}).get("gamepad_buttons",{}) or {}).items():
            if aid == action_id:
                return str(name)
        return None

    def _action_def(self, action_id: Optional[str]) -> Dict[str, Any]:
        if not action_id:
            return {}
        return dict((self.cfg.get("actions",{}) or {}).get(str(action_id),{}) or {})

    def _action_label(self, action_id: Optional[str]) -> str:
        if not action_id:
            return "Unassigned"
        adef=self._action_def(action_id)
        return str(adef.get("label", action_id))

    def _log_input(self, source: str, input_name: str, action_id: Optional[str]) -> None:
        label=self._action_label(action_id)
        print(f"[INPUT] {source}: {input_name} -> {label} (action={action_id or 'unassigned'})", flush=True)
        if hasattr(self,"lbl_active_input"):
            self.lbl_active_input.setText(f"Active input: {source} {input_name}")
            self.lbl_active_function.setText(f"Mapped function: {label}")

    def _set_action_down(self, action_id: Optional[str], down: bool) -> None:
        if action_id and action_id in self.action_buttons:
            self.action_buttons[action_id].setDown(bool(down))

    def _flash_action(self, action_id: Optional[str], milliseconds: int = 220) -> None:
        if not action_id or action_id not in self.action_buttons:
            return
        self._set_action_down(action_id, True)
        QTimer.singleShot(milliseconds, lambda a=action_id: self._set_action_down(a, False))

    def _dispatch_action(self, action_id: Optional[str], source: str, input_name: str) -> None:
        if not action_id:
            return
        adef=self._action_def(action_id); kind=str(adef.get("kind",""))
        self._log_input(source,input_name,action_id)
        if kind == "mode":
            self._flash_action(action_id)
            self.request_mode(str(adef.get("value", self._action_label(action_id))))
        elif kind == "throttle_step":
            self.set_throttle(self.throttle + float(adef.get("delta",0)))
        elif kind == "throttle_zero":
            self._flash_action(action_id); self.set_throttle(0)
        elif kind == "neutral":
            self._flash_action(action_id); self.neutral_attitude()
        elif kind == "arm":
            self._flash_action(action_id); self._arm_from_binding()
        elif kind == "disarm":
            self._flash_action(action_id); self._disarm_from_binding()

    def _arm_from_binding(self):
        if not self.link:
            print("[ACTION] ARM requested but MAVLink is not connected", flush=True); return
        lim=float(self.cfg["safety"].get("require_throttle_below_for_arm",100))
        if self.throttle>lim:
            print(f"[SAFETY] ARM blocked: throttle={self.throttle:.0f} > {lim:.0f}", flush=True); return
        self.link.arm(); print("[ACTION] ARM sent with normal ArduPilot pre-arm checks", flush=True)

    def _disarm_from_binding(self):
        if not self.link:
            print("[ACTION] DISARM requested but MAVLink is not connected", flush=True); return
        self.link.disarm(); print("[ACTION] DISARM sent", flush=True)

    def _key_token(self, key: int) -> str:
        table={
            int(Qt.Key.Key_W):"W", int(Qt.Key.Key_A):"A", int(Qt.Key.Key_S):"S", int(Qt.Key.Key_D):"D",
            int(Qt.Key.Key_Q):"Q", int(Qt.Key.Key_E):"E", int(Qt.Key.Key_R):"R", int(Qt.Key.Key_F):"F",
            int(Qt.Key.Key_1):"1", int(Qt.Key.Key_2):"2", int(Qt.Key.Key_3):"3", int(Qt.Key.Key_4):"4",
        }
        return table.get(int(key), str(key))

    def _press_keyboard_key(self, key: int, source: str = "Keyboard") -> None:
        token=self._key_token(key)
        action_id=(self.cfg.get("bindings",{}).get("keyboard",{}) or {}).get(token)
        adef=self._action_def(action_id); kind=str(adef.get("kind",""))
        if kind == "mode":
            self._dispatch_action(action_id,source,token)
        else:
            self._log_input(source, token, action_id)
            self.keys.add(int(key))

    def _release_keyboard_key(self, key: int) -> None:
        self.keys.discard(int(key))

    def _init_gamepad(self):
        try:
            pygame.init(); pygame.joystick.init()
            if self.no_gamepad:
                self.lbl_pad.setText("F310: disabled"); return
            count = pygame.joystick.get_count()
            if count < 1: raise RuntimeError("no joystick detected")
            preferred = str(self.cfg["controller"].get("preferred_name_contains","Logitech")).lower()
            selected = None
            for i in range(count):
                js = pygame.joystick.Joystick(i); js.init()
                if selected is None and preferred in js.get_name().lower(): selected = js
            if selected is None:
                idx = int(self.cfg["controller"].get("index",0)); selected = pygame.joystick.Joystick(idx); selected.init()
            self.joystick = selected; self.gamepad_ok = True
            self.lbl_pad.setText(f"F310: {selected.get_name()}  axes={selected.get_numaxes()} buttons={selected.get_numbuttons()} hats={selected.get_numhats()}")
        except Exception as exc:
            self.gamepad_ok = False; self.lbl_pad.setText(f"F310: unavailable ({exc})")

    def connect_mavlink(self):
        if self.link is not None: return
        self.btn_connect.setEnabled(False); self.lbl_mav.setText("MAVLink: CONNECTING…")
        self.link_thread = LinkThread(self.cfg, self.endpoint)
        self.link_thread.connected.connect(self._link_ready); self.link_thread.failed.connect(self._link_failed); self.link_thread.start()

    def _link_ready(self, link):
        self.link = link; self.lbl_mav.setText(f"MAVLink: CONNECTED sys={link.target_system}"); self.btn_connect.setText("Connected")

    def _link_failed(self, msg):
        self.lbl_mav.setText("MAVLink: FAILED"); self.btn_connect.setEnabled(True); QMessageBox.warning(self,"MAVLink connection",msg)

    def set_throttle(self, value):
        self.throttle = clamp(float(value),0,1000)
        self.throttle_slider.blockSignals(True); self.throttle_slider.setValue(int(self.throttle)); self.throttle_slider.blockSignals(False)
        self.throttle_bar.setValue(int(self.throttle))

    def neutral_attitude(self):
        for k in (Qt.Key.Key_W,Qt.Key.Key_A,Qt.Key.Key_S,Qt.Key.Key_D,Qt.Key.Key_Q,Qt.Key.Key_E): self.keys.discard(int(k))

    def request_mode(self, mode):
        if not self.link: return
        if not self.link.set_mode(mode): QMessageBox.warning(self,"Mode",f"Mode {mode} is not available in ArduPlane mode mapping")

    def arm_confirm(self):
        if not self.link: return
        lim=float(self.cfg["safety"].get("require_throttle_below_for_arm",100))
        if self.throttle>lim:
            QMessageBox.warning(self,"ARM blocked",f"Throttle {self.throttle:.0f} > ARM limit {lim:.0f}"); return
        if QMessageBox.question(self,"ARM Phoenix","Request ARM with normal ArduPilot pre-arm checks?")==QMessageBox.StandardButton.Yes: self.link.arm()

    def disarm_confirm(self):
        if self.link and QMessageBox.question(self,"DISARM Phoenix","Request DISARM? Only after touchdown.")==QMessageBox.StandardButton.Yes: self.link.disarm()

    def keyPressEvent(self,e:QKeyEvent):
        if e.isAutoRepeat() or not self.chk_keyboard.isChecked(): return
        self._press_keyboard_key(int(e.key()), source="Keyboard")

    def keyReleaseEvent(self,e:QKeyEvent):
        if not e.isAutoRepeat(): self._release_keyboard_key(int(e.key()))

    def _keyboard_axes(self,dt):
        pitch=(1 if int(Qt.Key.Key_W) in self.keys else 0)-(1 if int(Qt.Key.Key_S) in self.keys else 0)
        roll=(1 if int(Qt.Key.Key_D) in self.keys else 0)-(1 if int(Qt.Key.Key_A) in self.keys else 0)
        yaw=(1 if int(Qt.Key.Key_E) in self.keys else 0)-(1 if int(Qt.Key.Key_Q) in self.keys else 0)
        rate=float(self.cfg["throttle"].get("trigger_rate_per_second",350))
        if int(Qt.Key.Key_R) in self.keys:self.set_throttle(self.throttle+rate*dt)
        if int(Qt.Key.Key_F) in self.keys:self.set_throttle(self.throttle-rate*dt)
        return float(pitch),float(roll),float(yaw)

    def _read_dpad(self):
        if not self.joystick: return (0,0)
        hcfg=self.cfg.get("hat",{})
        if bool(hcfg.get("use_pygame_hat_first",True)) and self.joystick.get_numhats()>int(hcfg.get("index",0)):
            return get_hat_safe(self.joystick,int(hcfg.get("index",0)))
        a=self.cfg["axes"]
        x=get_axis_safe(self.joystick,int(a.get("dpad_x_axis",5)))
        y=get_axis_safe(self.joystick,int(a.get("dpad_y_axis",6)))
        return (0 if abs(x)<0.5 else (1 if x>0 else -1),0 if abs(y)<0.5 else (-1 if y>0 else 1))

    def _gamepad_axes(self,dt):
        if not (self.gamepad_ok and self.joystick and self.chk_gamepad.isChecked()):
            self.lt_fraction=self.rt_fraction=0.0
            return 0.0,0.0,0.0,0.0,(0,0)
        pygame.event.pump()
        c=self.cfg["controller"]; a=self.cfg["axes"]; b=self.cfg["buttons"]
        dz=float(c.get("deadzone",0.08)); expo=float(c.get("expo",0.20))
        roll=shape_axis(get_axis_safe(self.joystick,int(a["left_x"])),dz,expo,bool(c.get("invert_roll",False)))
        pitch=shape_axis(get_axis_safe(self.joystick,int(a["left_y"])),dz,expo,bool(c.get("invert_pitch",True)))
        yaw=shape_axis(get_axis_safe(self.joystick,int(a["right_x"])),dz,expo,bool(c.get("invert_yaw",False)))
        right_y=shape_axis(get_axis_safe(self.joystick,int(a["right_y"])),dz,expo,True)
        released=float(a.get("trigger_released_value",-1.0))
        self.lt_fraction=trigger_fraction(get_axis_safe(self.joystick,int(a["left_trigger"])),released)
        self.rt_fraction=trigger_fraction(get_axis_safe(self.joystick,int(a["right_trigger"])),released)
        rate=float(self.cfg["throttle"].get("trigger_rate_per_second",350))
        self.set_throttle(self.throttle+(self.rt_fraction-self.lt_fraction)*rate*dt)
        # Every digital button is sampled and visualized. Functions are resolved from YAML.
        gbindings=(self.cfg.get("bindings",{}).get("gamepad_buttons",{}) or {})
        now=time.monotonic()
        for name,idx in b.items():
            pressed=bool(get_button_safe(self.joystick,int(idx)))
            if name in self.indicators:self.indicators[name].set_pressed(pressed)
            old=bool(self.prev_buttons.get(name,False))
            rising=pressed and not old
            falling=(not pressed) and old
            action_id=gbindings.get(name)
            adef=self._action_def(action_id); kind=str(adef.get("kind",""))
            physical=f"{name.upper()} / Button {idx}"
            if rising:
                if kind in ("arm","disarm"):
                    self._log_input("Gamepad",physical,action_id)
                    self.hold_started[name]=now; self.hold_fired[name]=False; self._set_action_down(action_id,True)
                elif action_id:
                    self._dispatch_action(action_id,"Gamepad",physical)
                else:
                    self._log_input("Gamepad",physical,None)
            if pressed and kind in ("arm","disarm") and not self.hold_fired.get(name,False):
                required=float(adef.get("hold_seconds", self.cfg.get("safety",{}).get("arm_hold_seconds" if kind=="arm" else "disarm_hold_seconds",1.5)))
                if now-self.hold_started.get(name,now) >= required:
                    self.hold_fired[name]=True
                    if kind=="arm": self._arm_from_binding()
                    else: self._disarm_from_binding()
                    print(f"[HOLD] {physical} -> {self._action_label(action_id)} fired after {required:.2f}s", flush=True)
            if falling and kind in ("arm","disarm"):
                self._set_action_down(action_id,False); self.hold_started.pop(name,None); self.hold_fired.pop(name,None)
            self.prev_buttons[name]=pressed
        dpad=self._read_dpad()
        self._track_gamepad_axes(roll,pitch,yaw,right_y,self.lt_fraction,self.rt_fraction,dpad)
        return pitch,roll,yaw,right_y,dpad

    def _track_gamepad_axes(self, roll, pitch, yaw, right_y, lt, rt, dpad):
        bindings=(self.cfg.get("bindings",{}).get("gamepad_axes",{}) or {})
        dz=float(self.cfg.get("controller",{}).get("deadzone",0.08))
        samples={
            "left_x":(abs(roll)>dz, f"LEFT_X / Axis {self.cfg['axes']['left_x']}", bindings.get("left_x")),
            "left_y":(abs(pitch)>dz, f"LEFT_Y / Axis {self.cfg['axes']['left_y']}", bindings.get("left_y")),
            "right_x":(abs(yaw)>dz, f"RIGHT_X / Axis {self.cfg['axes']['right_x']}", bindings.get("right_x")),
            "right_y":(abs(right_y)>dz, f"RIGHT_Y / Axis {self.cfg['axes']['right_y']}", bindings.get("right_y")),
            "left_trigger":(lt>0.05, f"LT / Axis {self.cfg['axes']['left_trigger']}", bindings.get("left_trigger")),
            "right_trigger":(rt>0.05, f"RT / Axis {self.cfg['axes']['right_trigger']}", bindings.get("right_trigger")),
            "dpad":(tuple(dpad)!=(0,0), f"D-Pad / Hat {self.cfg.get('hat',{}).get('index',0)} {tuple(dpad)}", bindings.get("dpad")),
        }
        for name,(active,physical,action_id) in samples.items():
            old=bool(self.prev_axis_active.get(name,False))
            if active and not old:
                self._log_input("Gamepad axis",physical,action_id)
            self.prev_axis_active[name]=bool(active)

    def control_tick(self):
        now=time.monotonic(); dt=max(0.0,min(0.2,now-self.last_t)); self.last_t=now
        kp,kr,ky=self._keyboard_axes(dt) if self.chk_keyboard.isChecked() else (0.0,0.0,0.0)
        try: gp,gr,gy,gry,dpad=self._gamepad_axes(dt)
        except Exception as exc:
            self.gamepad_ok=False; self.lbl_pad.setText(f"F310: LOST ({exc})"); gp=gr=gy=gry=0.0; dpad=(0,0)
        c=self.cfg["controller"]
        pitch_src=gp if abs(gp)>0.01 else kp; roll_src=gr if abs(gr)>0.01 else kr; yaw_src=gy if abs(gy)>0.01 else ky
        pitch_f=clamp(pitch_src*float(c.get("max_pitch",0.6)),-1,1)
        roll_f=clamp(roll_src*float(c.get("max_roll",0.6)),-1,1)
        yaw_f=clamp(yaw_src*float(c.get("max_yaw",0.5)),-1,1)
        pitch=int(round(pitch_f*1000)); roll=int(round(roll_f*1000)); yaw=int(round(yaw_f*1000)); thr=int(round(self.throttle))
        self.left_stick.set_xy(roll_f,pitch_f)
        self.right_stick.set_xy(yaw_f,gry)
        self.dpad.set_hat(*dpad)
        self.lt_bar.setValue(int(round(self.lt_fraction*1000))); self.rt_bar.setValue(int(round(self.rt_fraction*1000)))
        self.lbl_lt.setText(f"LT {self.lt_fraction*100:5.1f}%   raw axis {self.cfg['axes']['left_trigger']}")
        self.lbl_rt.setText(f"RT {self.rt_fraction*100:5.1f}%   raw axis {self.cfg['axes']['right_trigger']}")
        self.throttle_bar.setValue(thr)
        self.lbl_values.setText(f"pitch={pitch:+d}  roll={roll:+d}  throttle={thr:d}  yaw={yaw:+d}   rightY={gry:+.2f}   dpad={dpad}")
        if self.link:
            try:
                self.link.send_manual(pitch,roll,thr,yaw); self.link.drain()
                self.lbl_mode.setText(f"Mode: {self.link.mode}"); self.lbl_arm.setText("Arm: ARMED" if self.link.armed else "Arm: DISARMED")
            except Exception as exc:
                self.lbl_mav.setText(f"MAVLink: ERROR {exc}"); self.link=None; self.btn_connect.setEnabled(True); self.btn_connect.setText("Reconnect MAVLink")

    def closeEvent(self,event):
        try:
            if self.link:
                for _ in range(5): self.link.send_manual(0,0,int(self.throttle),0); time.sleep(0.02)
        except Exception: pass
        pygame.quit(); event.accept()


def main():
    here=Path(__file__).resolve().parent
    ap=argparse.ArgumentParser(description="Phoenix F310 graphical RC controller v5")
    ap.add_argument("--config",default=str(here/"config"/"f310_phoenix.yaml"))
    ap.add_argument("--connect",help="MAVLink endpoint, e.g. udpin:0.0.0.0:14552")
    ap.add_argument("--no-gamepad",action="store_true")
    args=ap.parse_args(); cfg=load_config(args.config)
    app=QApplication(sys.argv); gui=PhoenixGUI(cfg,args.connect,args.no_gamepad); gui.show(); sys.exit(app.exec())


if __name__=="__main__": main()
