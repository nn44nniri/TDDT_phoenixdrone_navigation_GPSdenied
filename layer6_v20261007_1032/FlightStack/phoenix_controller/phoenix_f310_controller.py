#!/usr/bin/env python3
"""Logitech F310 manual controller for ArduPilot Phoenix tailsitter SITL.

Architecture:
  Logitech F310 -> pygame -> this process -> pymavlink MANUAL_CONTROL -> ArduPilot

The controller intentionally sends pilot-domain inputs only. ArduPilot remains
responsible for QHOVER/QSTABILIZE/FBWA control, tailsitter mixing, elevons, and motors.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import pygame
import yaml
from pymavlink import mavutil


INT16_MAX = 32767

from phoenix_f310_utils import clamp, shape_axis, trigger_fraction

def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ValueError("configuration root must be a mapping")
    return cfg


def get_axis_safe(js: pygame.joystick.Joystick, idx: int) -> float:
    if idx < 0 or idx >= js.get_numaxes():
        return 0.0
    return float(js.get_axis(idx))


def get_button_safe(js: pygame.joystick.Joystick, idx: int) -> int:
    if idx < 0 or idx >= js.get_numbuttons():
        return 0
    return int(js.get_button(idx))


@dataclass
class ButtonLatch:
    previous: bool = False

    def rising(self, current: bool) -> bool:
        out = bool(current and not self.previous)
        self.previous = bool(current)
        return out


@dataclass
class HoldAction:
    started_at: Optional[float] = None
    fired: bool = False

    def update(self, pressed: bool, now: float, hold_s: float) -> bool:
        if not pressed:
            self.started_at = None
            self.fired = False
            return False
        if self.started_at is None:
            self.started_at = now
            return False
        if not self.fired and (now - self.started_at) >= hold_s:
            self.fired = True
            return True
        return False


class MavlinkLink:
    def __init__(self, cfg: Dict[str, Any], override_connection: Optional[str] = None):
        mcfg = cfg["mavlink"]
        self.connection_string = override_connection or str(mcfg["connection"])
        self.source_system = int(mcfg.get("source_system", 254))
        self.source_component = int(mcfg.get("source_component", 191))
        self.heartbeat_timeout = float(mcfg.get("heartbeat_timeout_seconds", 10))
        self.ack_timeout = float(mcfg.get("command_ack_timeout_seconds", 3))
        self.master = None
        self.target_system = 0
        self.target_component = 0

    def connect(self) -> None:
        print(f"[MAV] opening {self.connection_string}")
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
        print(f"[MAV] heartbeat: sys={self.target_system} comp={self.target_component} type={hb.type}")

    def send_manual(self, pitch: int, roll: int, throttle: int, yaw: int) -> None:
        assert self.master is not None
        # ArduPilot MANUAL_CONTROL semantics: x/y/r [-1000,+1000], z [0,1000].
        self.master.mav.manual_control_send(
            self.target_system,
            int(clamp(pitch, -1000, 1000)),
            int(clamp(roll, -1000, 1000)),
            int(clamp(throttle, 0, 1000)),
            int(clamp(yaw, -1000, 1000)),
            0,
        )

    def set_mode(self, mode_name: str) -> bool:
        assert self.master is not None
        mapping = self.master.mode_mapping() or {}
        mode_name = mode_name.upper()
        if mode_name not in mapping:
            print(f"[MODE] {mode_name} not in mode mapping; available subset: {sorted(mapping)[:20]}")
            return False
        mode_id = mapping[mode_name]
        print(f"[MODE] request {mode_name} ({mode_id})")
        self.master.mav.set_mode_send(
            self.target_system,
            mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
            mode_id,
        )
        return True

    def arm(self, force: bool = False) -> None:
        self._arm_disarm(True, force)

    def disarm(self, force: bool = False) -> None:
        self._arm_disarm(False, force)

    def _arm_disarm(self, arm: bool, force: bool) -> None:
        assert self.master is not None
        p2 = 21196 if force else 0
        self.master.mav.command_long_send(
            self.target_system,
            self.target_component,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
            0,
            1 if arm else 0,
            p2,
            0, 0, 0, 0, 0,
        )
        print(f"[ARM] {'ARM' if arm else 'DISARM'} requested{' (FORCE)' if force else ''}")

    def drain_messages(self) -> None:
        if self.master is None:
            return
        while True:
            msg = self.master.recv_match(blocking=False)
            if msg is None:
                break
            t = msg.get_type()
            if t == "STATUSTEXT":
                sev = getattr(msg, "severity", "?")
                txt = getattr(msg, "text", "")
                print(f"[FCU][{sev}] {txt}")
            elif t == "COMMAND_ACK":
                print(f"[ACK] command={msg.command} result={msg.result}")


def pick_joystick(cfg: Dict[str, Any]) -> pygame.joystick.Joystick:
    pygame.joystick.init()
    count = pygame.joystick.get_count()
    if count <= 0:
        raise RuntimeError("no joystick/gamepad detected by pygame")
    preferred = str(cfg["controller"].get("preferred_name_contains", "")).lower()
    fallback_index = int(cfg["controller"].get("index", 0))
    chosen = None
    for i in range(count):
        js = pygame.joystick.Joystick(i)
        js.init()
        name = js.get_name()
        print(f"[GAMEPAD] index={i} name={name!r} axes={js.get_numaxes()} buttons={js.get_numbuttons()} hats={js.get_numhats()}")
        if preferred and preferred in name.lower() and chosen is None:
            chosen = js
    if chosen is None:
        if fallback_index >= count:
            raise RuntimeError(f"configured joystick index {fallback_index} not present (count={count})")
        chosen = pygame.joystick.Joystick(fallback_index)
        chosen.init()
    print(f"[GAMEPAD] selected {chosen.get_name()!r}")
    return chosen


def diagnose() -> int:
    pygame.init()
    pygame.joystick.init()
    count = pygame.joystick.get_count()
    if count == 0:
        print("No joystick detected. Check /dev/input and F310 D/X switch.")
        return 2
    sticks = []
    for i in range(count):
        js = pygame.joystick.Joystick(i)
        js.init()
        sticks.append(js)
        print(f"index={i}: {js.get_name()} axes={js.get_numaxes()} buttons={js.get_numbuttons()} hats={js.get_numhats()}")
    print("\nMove sticks/triggers and press buttons. Ctrl+C to exit. Values print only on change.")
    last = {}
    try:
        while True:
            pygame.event.pump()
            for ji, js in enumerate(sticks):
                for a in range(js.get_numaxes()):
                    v = round(float(js.get_axis(a)), 3)
                    k = (ji, "axis", a)
                    if abs(v - last.get(k, 99.0)) >= 0.05:
                        print(f"js{ji} axis {a}: {v:+.3f}")
                        last[k] = v
                for b in range(js.get_numbuttons()):
                    v = int(js.get_button(b))
                    k = (ji, "button", b)
                    if v != last.get(k, -1):
                        if v:
                            print(f"js{ji} button {b}: PRESSED")
                        last[k] = v
                for h in range(js.get_numhats()):
                    v = tuple(js.get_hat(h))
                    k = (ji, "hat", h)
                    if v != last.get(k):
                        if v != (0, 0):
                            print(f"js{ji} hat {h}: {v}")
                        last[k] = v
            time.sleep(0.02)
    except KeyboardInterrupt:
        return 0


def main() -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Phoenix tailsitter Logitech F310 MAVLink controller")
    parser.add_argument("--config", default=str(here / "config" / "f310_phoenix.yaml"))
    parser.add_argument("--connect", help="override MAVLink connection, e.g. udpin:0.0.0.0:14552 or tcp:127.0.0.1:5760")
    parser.add_argument("--diagnose", action="store_true", help="print raw gamepad axes/buttons; no MAVLink connection")
    parser.add_argument("--dry-run", action="store_true", help="read F310 and print controls; do not connect to ArduPilot")
    args = parser.parse_args()

    if args.diagnose:
        return diagnose()

    cfg = load_config(args.config)
    pygame.init()
    js = pick_joystick(cfg)

    ccfg = cfg["controller"]
    axes = cfg["axes"]
    buttons = cfg["buttons"]
    throttle_cfg = cfg["throttle"]
    safety = cfg["safety"]
    modes = cfg["modes"]
    actions = cfg.get("actions", {}) or {}
    bindings = cfg.get("bindings", {}) or {}
    gamepad_bindings = bindings.get("gamepad_buttons", {}) or {}

    rate_hz = float(ccfg.get("update_hz", 20))
    period = 1.0 / rate_hz
    deadzone = float(ccfg.get("deadzone", 0.08))
    expo = float(ccfg.get("expo", 0.25))
    max_pitch = float(ccfg.get("max_pitch", 0.6))
    max_roll = float(ccfg.get("max_roll", 0.6))
    max_yaw = float(ccfg.get("max_yaw", 0.5))

    throttle = float(throttle_cfg.get("initial", 0))
    throttle_min = float(throttle_cfg.get("minimum", 0))
    throttle_max = float(throttle_cfg.get("maximum", 1000))
    trigger_rate = float(throttle_cfg.get("trigger_rate_per_second", 350))
    bumper_step = float(throttle_cfg.get("bumper_step", 35))

    button_latches = {name: ButtonLatch() for name in buttons}
    arm_hold = HoldAction()
    disarm_hold = HoldAction()
    arm_button_name = next((n for n,a in gamepad_bindings.items() if a == "arm"), "start")
    disarm_button_name = next((n for n,a in gamepad_bindings.items() if a == "disarm"), "back")

    link = None
    if not args.dry_run:
        link = MavlinkLink(cfg, args.connect)
        link.connect()

    running = True
    gamepad_lost = False

    def stop_handler(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    print("\nControls:")
    print("  Left stick  : roll / pitch")
    print("  Right stick X: yaw")
    print("  RT/LT       : throttle increase/decrease")
    print("  RB/LB       : throttle +step/-step fallback")
    print("  A=QHOVER B=FBWA X=QSTABILIZE Y=QLOITER")
    print("  Hold START to ARM; hold BACK to DISARM")
    print("  Ctrl+C exits; no forced arm/disarm is used by default.\n")

    last_t = time.monotonic()
    last_print = 0.0
    try:
        while running:
            loop_start = time.monotonic()
            dt = max(0.0, min(0.2, loop_start - last_t))
            last_t = loop_start
            events = pygame.event.get()
            for ev in events:
                if ev.type == getattr(pygame, "JOYDEVICEREMOVED", -999):
                    gamepad_lost = True
                    raise RuntimeError("gamepad disconnected (JOYDEVICEREMOVED)")
            pygame.event.pump()

            # Fallback disconnect check.
            if not js.get_init():
                gamepad_lost = True
                raise RuntimeError("gamepad disconnected")

            lx = get_axis_safe(js, int(axes["left_x"]))
            ly = get_axis_safe(js, int(axes["left_y"]))
            rx = get_axis_safe(js, int(axes["right_x"]))

            roll_f = shape_axis(lx, deadzone, expo, bool(ccfg.get("invert_roll", False))) * max_roll
            pitch_f = shape_axis(ly, deadzone, expo, bool(ccfg.get("invert_pitch", True))) * max_pitch
            yaw_f = shape_axis(rx, deadzone, expo, bool(ccfg.get("invert_yaw", False))) * max_yaw

            # Trigger-based persistent throttle.
            lt_raw = get_axis_safe(js, int(axes["left_trigger"]))
            rt_raw = get_axis_safe(js, int(axes["right_trigger"]))
            released = float(axes.get("trigger_released_value", -1.0))
            lt = trigger_fraction(lt_raw, released)
            rt = trigger_fraction(rt_raw, released)
            throttle += (rt - lt) * trigger_rate * dt

            # All digital button functions are resolved from YAML bindings.
            now = time.monotonic()
            for name, idx in buttons.items():
                pressed = bool(get_button_safe(js, int(idx)))
                if button_latches[name].rising(pressed):
                    action_id = gamepad_bindings.get(name)
                    adef = actions.get(action_id, {}) if action_id else {}
                    label = str(adef.get("label", action_id or "Unassigned"))
                    print(f"[INPUT] Gamepad: {name.upper()} / Button {idx} -> {label} (action={action_id or 'unassigned'})")
                    kind = str(adef.get("kind", ""))
                    if kind == "mode":
                        mode = str(adef.get("value", modes.get(name, label)))
                        if link: link.set_mode(mode)
                        else: print(f"[DRY] mode {mode}")
                    elif kind == "throttle_step":
                        throttle += float(adef.get("delta", 0))
            throttle = clamp(throttle, throttle_min, throttle_max)

            arm_pressed = bool(get_button_safe(js, int(buttons[arm_button_name])))
            disarm_pressed = bool(get_button_safe(js, int(buttons[disarm_button_name])))

            if arm_hold.update(arm_pressed, now, float(actions.get("arm",{}).get("hold_seconds", safety.get("arm_hold_seconds", 1.5)))):
                arm_limit = float(safety.get("require_throttle_below_for_arm", 100))
                if throttle > arm_limit:
                    print(f"[SAFETY] ARM blocked: throttle={throttle:.0f} > {arm_limit:.0f}")
                elif link:
                    link.arm(force=bool(safety.get("allow_force_arm", False)))
                else:
                    print("[DRY] ARM")

            if disarm_hold.update(disarm_pressed, now, float(actions.get("disarm",{}).get("hold_seconds", safety.get("disarm_hold_seconds", 1.5)))):
                if link:
                    link.disarm(force=bool(safety.get("allow_force_disarm", False)))
                else:
                    print("[DRY] DISARM")

            pitch = int(round(clamp(pitch_f, -1, 1) * 1000))
            roll = int(round(clamp(roll_f, -1, 1) * 1000))
            yaw = int(round(clamp(yaw_f, -1, 1) * 1000))
            thr = int(round(throttle))

            if link:
                link.send_manual(pitch, roll, thr, yaw)
                link.drain_messages()
            elif (now - last_print) >= 0.25:
                print(f"[DRY] pitch={pitch:+5d} roll={roll:+5d} throttle={thr:4d} yaw={yaw:+5d} LT={lt:.2f} RT={rt:.2f}")
                last_print = now

            sleep_s = period - (time.monotonic() - loop_start)
            if sleep_s > 0:
                time.sleep(sleep_s)

    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        gamepad_lost = True
    finally:
        if link and gamepad_lost:
            neutral_s = float(safety.get("disconnect_neutral_seconds", 0.5))
            neutral_thr = int(safety.get("disconnect_neutral_throttle", 500))
            deadline = time.monotonic() + neutral_s
            print(f"[FAILSAFE] sending neutral attitude/throttle={neutral_thr} for {neutral_s:.2f}s, then stopping output")
            while time.monotonic() < deadline:
                try:
                    link.send_manual(0, 0, neutral_thr, 0)
                except Exception:
                    break
                time.sleep(period)
        pygame.quit()
    return 0 if not gamepad_lost else 2


if __name__ == "__main__":
    raise SystemExit(main())
