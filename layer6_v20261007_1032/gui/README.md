# Phoenix F310 Controller v5

Graphical/manual RC controller for the Phoenix dual-rotor tailsitter SITL stack:

`Logitech F310 / keyboard -> pygame + PyQt6 -> pymavlink MANUAL_CONTROL -> ArduPlane -> SIM_JSBSim -> phoenix_v3`

## What v5 changes

v5 keeps the v4 architecture and calibrated F310 axis mapping, but makes `config/f310_phoenix.yaml` the authoritative source for **input -> function** associations.

The `Flight modes / actions` panel now follows the physical controller/keyboard:

- F310 A visually presses QHOVER and requests QHOVER.
- F310 B visually presses FBWA and requests FBWA.
- F310 X visually presses QSTABILIZE and requests QSTABILIZE.
- F310 Y visually presses QLOITER and requests QLOITER.
- F310 START/Button 7 visually holds ARM down; after the configured hold time it sends normal ARM.
- F310 BACK/Button 6 visually holds DISARM down; after the configured hold time it sends DISARM.
- Keyboard 1/2/3/4 visually press QSTABILIZE/QHOVER/FBWA/QLOITER respectively.
- Stick, trigger, D-Pad and keyboard-axis activity is shown in the `Active input` / `Mapped function` fields inside the same panel.
- Every gamepad button press and every keyboard key press prints a terminal line such as:

```text
[INPUT] Gamepad: START / Button 7 -> ARM (action=arm)
[INPUT] Keyboard: 2 -> QHOVER (action=qhover)
[INPUT] Gamepad axis: RT / Axis 5 -> Throttle increase (action=throttle_increase)
```

## Calibrated F310 mapping preserved from v4

The supplied Linux `jstest-gtk` view exposes kernel axes as ABS_X/ABS_Y/ABS_RX/ABS_RY/ABS_Z/ABS_RZ/HAT, while pygame/SDL exposes six analog axes and one hat in a different order. The runtime GUI uses the pygame/SDL mapping already validated in v4:

| pygame runtime input | Function |
|---|---|
| axis 0 | Left stick X / Roll |
| axis 1 | Left stick Y / Pitch |
| axis 2 | LT |
| axis 3 | Right stick X / Yaw |
| axis 4 | Right stick Y / Aux display |
| axis 5 | RT |
| hat 0 | D-Pad X/Y |

Digital buttons remain:

| Button | Physical input | Default action |
|---:|---|---|
| 0 | A | QHOVER |
| 1 | B | FBWA |
| 2 | X | QSTABILIZE |
| 3 | Y | QLOITER |
| 4 | LB | Throttle step down |
| 5 | RB | Throttle step up |
| 6 | BACK | DISARM hold |
| 7 | START | ARM hold |
| 8 | MODE | Unassigned |
| 9 | L3 | Unassigned |
| 10 | R3 | Unassigned |

## YAML-driven actions and bindings

The two relevant sections are:

```yaml
actions:
  qhover:
    label: QHOVER
    kind: mode
    value: QHOVER
  arm:
    label: ARM
    kind: arm
    hold_seconds: 1.5
  throttle_step_up:
    label: Throttle + step
    kind: throttle_step
    delta: 35

bindings:
  gamepad_buttons:
    a: qhover
    b: fbwa
    x: qstabilize
    y: qloiter
    lb: throttle_step_down
    rb: throttle_step_up
    back: disarm
    start: arm
  keyboard:
    W: pitch_positive
    S: pitch_negative
    A: roll_negative
    D: roll_positive
    Q: yaw_negative
    E: yaw_positive
    R: throttle_increase
    F: throttle_decrease
    '1': qstabilize
    '2': qhover
    '3': fbwa
    '4': qloiter
```

The old `modes:` section is retained for backward compatibility, but v5 GUI/CLI action dispatch uses `actions` + `bindings` as the authoritative mapping.

## Installation on Debian 13

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip joystick evtest libgl1 libegl1 libxkbcommon-x11-0

tar -xzf phoenix_f310_controller_v5.tar.gz
cd /media/p1/DigitalTwin/TDDT_navigation/arduPilot/gamepad/phoenix_f310_controller_v5
source ~/venv-ardupilot-debian13/bin/activate
./scripts/install.sh
```

## Verify controller mapping

```bash
jstest-gtk
```

For raw pygame/SDL diagnosis:

```bash
python3 ../src/gui/phoenix_f310_controller.py --diagnose
```

## Terminal 1 — ArduPilot + JSBSim

```bash
python3 ./Tools/autotest/sim_vehicle.py \
  -v ArduPlane \
  -f jsbsim:phoenix_v3 \
  -l 40.010000,-105.300000,1798.37,53 \
  --add-param-file=Tools/autotest/default_params/phoenix_v3.parm \
  --console \
  --map \
  --out=udp:127.0.0.1:14551 \
  --out=udp:127.0.0.1:14552
```

## Terminal 2 — Environment Adapter

```bash
phoenix-env runtime \
  --dataset-tar /path/to/internet_datasets.tar.gz \
  --region /path/to/region_v1.geojson \
  --time 2025-05-20T12:00:00Z \
  --cache runtime/dataset_cache \
  --mavlink udpin:0.0.0.0:14551 \
  --update-hz 1
```

## Terminal 3 — v5 GUI

```bash
cd phoenix_f310_controller_v5
source ~/venv-ardupilot-debian13/bin/activate
./scripts/run_gui.sh --connect udpin:0.0.0.0:14552
```

Then click **Connect MAVLink**.

## Expected visual behavior

Pressing F310 START/Button 7 immediately makes the GUI ARM button look physically pressed. Keep START held for the configured 1.5 seconds and the controller sends ARM using normal ArduPilot pre-arm checks; releasing START releases the visual ARM button.

Pressing BACK/Button 6 similarly drives the GUI DISARM button and fires DISARM after its configured hold duration.

Pressing A/B/X/Y briefly depresses the corresponding mode button in `Flight modes / actions`. Keyboard 1/2/3/4 does the same.

Axis movement is shown in `Active input` / `Mapped function`; an axis message is printed only when it crosses into an active region, so the terminal is not flooded every control tick.

## Operational guide

The supplied `GUIDE_USING.md` is included under `docs/GUIDE_USING.md`. Its default operational assignments are represented in the v5 YAML bindings: A=QHOVER, B=FBWA, X=QSTABILIZE, Y=QLOITER, START=ARM hold, BACK=DISARM hold, LB/RB throttle steps, LT/RT continuous throttle decrease/increase, Left Stick Roll/Pitch, and Right Stick X Yaw.

## Safety

The gamepad ARM action does not force-arm. It respects the configured throttle limit and normal ArduPilot pre-arm checks. DISARM should only be used after touchdown. Direct motor RPM/elevon commands are not sent; ArduPilot remains responsible for stabilization, tailsitter mixing and actuator output.
