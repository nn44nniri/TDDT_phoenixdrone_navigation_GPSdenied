# TDDT Layer-6 — `layer6_v20261007_0937`

این بسته پیاده‌سازی Python لایهٔ ۶ با تعریف **Stateful Temporal Digital-Pilot Translation & Execution Gateway** است.

## مسیر اجرای اصلی

```text
Layer-5 D_exec
  -> validation (time / pilot-domain / safety / direct_pwm=false)
  -> DigitalPilotExecutor (latch + rate adaptation)
  -> localhost UDP Virtual Pilot (127.0.0.1:14660)
  -> Phoenix Layer-6 GUI (visual stick/throttle/mode/action changes)
  -> همان MAVLink link دکمه Connect MAVLink
  -> ArduPilot SITL -> JSBSim
```

GUI عمداً MAVLink executor اصلی باقی مانده است؛ Digital Pilot آن را دور نمی‌زند. وقتی virtual input معتبر است، ورودی L5 روی stick/throttle GUI نمایش داده می‌شود و همان state در چرخهٔ ~30 Hz با `MANUAL_CONTROL` ارسال می‌شود. اگر virtual packet منقضی شود، GUI آن را fail-neutral آزاد می‌کند و کنترل دستی دوباره قابل استفاده است.

## FlightStack launcher یکپارچه

تمام منطق اجرایی `run_flightstack_3term_rev1.sh` به `src/run_flightstack.py` منتقل شده است:

```bash
python3 src/run_flightstack.py start
# or explicitly:
python3 src/run_flightstack.py start --flightstack-dir ./FlightStack
python3 src/run_flightstack.py status
python3 src/run_flightstack.py restart
python3 src/run_flightstack.py stop
```

سه ترمینال به‌ترتیب اجرا می‌شوند:
1. ArduPilot + JSBSim (`sim_vehicle.py`, PhoenixBoulder, `14551/14552`)
2. Environment Adapter (`phoenix-env prepare` سپس `phoenix-env runtime` روی `14551`)
3. GUI خود Layer-6 روی `14552`

### تابع `restart()`

در Python نیز مستقیماً قابل فراخوانی است:

```python
from run_flightstack import restart
restart()
```

`restart()` ابتدا process-groupهای ثبت‌شده را با `SIGINT` (معادل Ctrl+C) می‌بندد، سپس برای dependencyهای شناخته‌شدهٔ ArduPilot cleanup محافظه‌کارانه اجرا می‌کند و هر سه ترمینال/GUI را با تنظیمات ذخیره‌شده دوباره باز می‌کند.

## Layer-5 intake endpoint

Terminal 3 همچنین سرویس `layer6.service` را روی `udp://127.0.0.1:14661` اجرا می‌کند. هر datagram یک JSON مطابق `D_exec` است و پاسخ ACK JSON به sender برمی‌گردد. نمونه:

```bash
python3 src/tools/send_layer5_example.py --mode QHOVER
```


## پایان Demo: restart یا stop

دو گزینهٔ lifecycle به‌صورت **mutually exclusive** در Demo وجود دارند:

```bash
python3 src/demo_layer6.py --seed 42 --restart-at-end --terminal gnome-terminal
```

یا برای بستن کامل اجزای وابسته با همان semantics مربوط به Ctrl+C/SIGINT، بدون بازکردن مجدد:

```bash
python3 src/demo_layer6.py --seed 42 --stop-at-end
```

`--restart-at-end` و `--stop-at-end` همزمان قابل استفاده نیستند. هر دو فقط پس از پایان موفق sequence اجرا می‌شوند.

## API رسمی برای AI / TDDT خارجی

برای اتصال یک AI، TDDT یا برنامهٔ Python خارجی، استفاده از `demo_layer6.py` به‌عنوان API توصیه نمی‌شود؛ Demo شامل mission sequence، randomization، landing/disarm verification و lifecycle management است. API پایدار این نسخه `layer6.client.Layer6Client` است و به endpoint رسمی intake یعنی `udp://127.0.0.1:14661` متصل می‌شود. مسیر اجرا همچنان GUI را دور نمی‌زند:

```text
AI / TDDT Python
  -> Layer6Client
  -> layer6.service :14661
  -> DigitalPilotExecutor
  -> Virtual Pilot
  -> GUI
  -> MAVLink
  -> ArduPilot
```

نمونهٔ استفاده:

```python
from layer6 import Layer6Client

with Layer6Client() as l6:
    ack = l6.submit_command(
        [0.10, -0.05, 0.55, 0.00],
        mode="QHOVER",
        safety={"psf": True, "rta": True},
        confidence=0.96,
        provenance={"source": "TDDT-L5"},
    )
    print(ack.transaction_id)
```

برای بستهٔ canonical موجود نیز می‌توان مستقیماً `l6.submit(D_exec)` را فراخوانی کرد. `Layer6Client` به‌طور عمدی evidence مربوط به PSF/RTA را جعل نمی‌کند؛ upstream باید safety evidence واقعی خود را ارسال کند.

## Connect MAVLink

اتصال دستی همچنان با دکمهٔ **Connect MAVLink** در GUI قابل انجام است. علاوه بر آن، Demo پایتونی از همان GUI درخواست اتصال می‌کند؛ GUI همچنان MAVLink executor است و Demo هرگز ArduPilot را دور نمی‌زند. endpoint پیش‌فرض `udpin:0.0.0.0:14552` است. Demo قبل از شروع sequence باید status برگشتی GUI را دریافت و heartbeat واقعی ArduPilot را تأیید کند.

## Demo تصادفی ولی ترتیبی

پس از اجرای stack (نیازی به فشردن دستی Connect نیست؛ Demo در صورت نیاز از خود GUI درخواست Connect می‌کند):

```bash
python3 src/demo_layer6.py --seed 42
```

ترتیب مراحل ثابت و ایمن‌سازی‌شده است: Ground -> QHOVER -> ARM -> VTOL takeoff -> hover -> translation -> FBWA -> cruise -> approach -> back-transition -> QHOVER -> landing -> throttle zero -> DISARM. فقط commandهای پیوسته در دامنه‌های محدود jitter می‌شوند.

برای اجرای سریع‌تر نمایشی:

```bash
python3 src/demo_layer6.py --seed 42 --time-scale 0.2
```

### فرود و DISARM تأییدشده

نسخهٔ 1205 قبل از DISARM صرفاً به `THROTTLE_ZERO` اکتفا نمی‌کند. GUI از MAVLink وضعیت `EXTENDED_SYS_STATE.landed_state`، `VFR_HUD.groundspeed/climb` و `GLOBAL_POSITION_INT.relative_alt` را جمع‌آوری و به Demo برمی‌گرداند. Demo ابتدا تا `ON_GROUND` یا fallback محافظه‌کارانهٔ نزدیک زمین صبر می‌کند و سپس DISARM معمولی را با retry محدود می‌فرستد و `armed=False` را تأیید می‌کند. Force-disarm به‌صورت خودکار استفاده نمی‌شود.

گزینه‌های مرتبط:

```bash
python3 src/demo_layer6.py --seed 42 --landing-timeout 20 --disarm-retries 3
```

## قرارداد L5

`u_cmd=[aileron,elevator,throttle,rudder]` با دامنه‌های `[-1,1],[-1,1],[0,1],[-1,1]`. `direct_pwm=true` رد می‌شود. `tau_apply/tau_deadline` و PSF/RTA در صورت وجود دوباره اعتبارسنجی می‌شوند.

## خروجی L7

`layer6/feedback.py` مدل canonical برای `x_obs`, `y_real`, `u_cmd_pilot`, `u_applied_pilot`, execution health, timing و provenance را فراهم می‌کند. در این نسخهٔ integration، acquisition کامل telemetry باید از پیام‌های MAVLink واقعی stack تغذیه شود؛ state تخمینی actuator به‌عنوان measured truth علامت‌گذاری نشده است.

## نصب GUI/وابستگی‌ها

FlightStack اصلی طبق `scripts/install_flightstack_rev1.sh` آماده می‌شود. این بسته GUI خود را حمل می‌کند؛ بنابراین Terminal 3 دیگر از `FlightStack/phoenix_controller` استفاده نمی‌کند.

```bash
python3 -m pip install -r requirements.txt
```

## تست

```bash
pytest -q
python3 -m compileall -q src
```

## نکات ایمنی

- ARM اجباری نیست؛ pre-arm checks ArduPilot حفظ شده است.
- فرمان raw PWM/motor/elevon از L5 پذیرفته نمی‌شود.
- QLOITER فقط با position estimate معتبر مناسب است.
- Demo برای SITL/JSBSim است و envelope پرواز واقعی محسوب نمی‌شود.

## Runtime diagnostics for terminal-launch failures

`src/run_flightstack.py` now supports explicit diagnostics without changing the Layer-6 execution architecture.

Normal launch:

```bash
python3 src/run_flightstack.py start
```

Verbose launch diagnostics:

```bash
python3 src/run_flightstack.py start --verbose
```

Full debug output, including the exact terminal-emulator command lines:

```bash
python3 src/run_flightstack.py start --debug
```

To force a specific terminal emulator:

```bash
python3 src/run_flightstack.py start --debug --terminal gnome-terminal
```

The launcher records every run under:

```text
runtime/logs/run_flightstack_YYYYMMDD_HHMMSS.log
```

The diagnostics include the selected terminal executable, `DISPLAY`, `WAYLAND_DISPLAY`, `DBUS_SESSION_BUS_ADDRESS`, desktop/session variables, launch PID, startup check, return code, stdout/stderr from a terminal client that exits immediately, and the exact command in `--debug` mode. `status --verbose` also reports the detected terminal candidates and graphical-session state.

If no terminal window appears, the recommended first command is:

```bash
python3 src/run_flightstack.py start --debug --terminal gnome-terminal
```

and then inspect the log path printed by the launcher.

### GNOME/X11 terminal launch compatibility

On desktop sessions where `gnome-terminal` immediately returns status 0 after handing the request to its terminal server, Layer-6 does not use `--wait`. It uses the same launch form as the proven shell launcher and verifies each new terminal using `runtime/pids/terminalN.pid`. For diagnostics:

```bash
python3 src/run_flightstack.py start --debug --terminal gnome-terminal
```

A successful launch reports `terminal child handshake received` for Terminal 1, 2, and 3.


## v20261006_1033: src layout and direct Python demo

All Python source files in this Layer-6 package are now located under `src/`.
The runtime shell helpers/configuration remain in `scripts/` and `gui/`, but they invoke Python from `src/`.

Primary launcher:

```bash
python3 src/run_flightstack.py start --debug --terminal gnome-terminal
```

Direct Python demo (no `.sh` wrapper is required):

```bash
python3 src/demo_layer6.py --seed 42
```

Fast demo:

```bash
python3 src/demo_layer6.py --seed 42 --time-scale 0.2
```

To request a full Layer-6 restart after the successful demo sequence:

```bash
python3 src/demo_layer6.py --seed 42 --restart-at-end --terminal gnome-terminal
```

`--restart-at-end` calls the same Python `restart()` lifecycle function used by
`src/run_flightstack.py`; it sends SIGINT/Ctrl+C semantics to the dependent
ArduPilot/JSBSim/MAVProxy/environment/GUI/service processes and relaunches the stack.


## v20261006_1144: verified Demo execution path

در این نسخه Demo دیگر موفقیت را فقط از روی حرکت پنل GUI استنتاج نمی‌کند. کانال Virtual Pilot دوطرفه شده است:

```text
demo_layer6.py
  -> GUI Virtual Pilot UDP 127.0.0.1:14660
  -> GUI requests/owns MAVLink connection
  -> ArduPilot SITL
  -> HEARTBEAT / armed / mode evidence
  -> GUI status reply
  -> demo verification gate
```

Demo در شروع، `mavlink_connected` را از GUI می‌خواند. اگر false باشد، پیام `connect_mavlink` به GUI می‌فرستد و تا heartbeat صبر می‌کند. سپس تغییرهای QHOVER/FBWA و ARM/DISARM فقط در صورت مشاهدهٔ state واقعی ArduPilot تأیید می‌شوند. در صورت رد شدن ARM توسط pre-arm checks، Demo متوقف می‌شود و status آخر را چاپ می‌کند. `COMMAND_ACK` و `STATUSTEXT` نیز در terminal مربوط به GUI چاپ می‌شوند.

اجرای توصیه‌شده:

```bash
python3 src/demo_layer6.py --seed 42 --restart-at-end --terminal gnome-terminal
```

برای افزایش زمان انتظار در سیستم کندتر:

```bash
python3 src/demo_layer6.py --seed 42 --connect-timeout 20 --verify-timeout 15
```

## Update 2026-10-07 — unified Client/Demo lifecycle

`src/demo_layer6.py` is now only a compatibility entry point. Its complete
implementation lives in `src/layer6/client.py`, so Demo, AI and TDDT callers use
one canonical path: `Layer6Client -> Layer-6 intake -> DigitalPilotExecutor ->
Virtual Pilot -> GUI -> MAVLink -> ArduPilot`.

All previous demo switches are retained and a new `--start-if-needed` switch is
available:

```bash
python3 src/demo_layer6.py --start-if-needed --seed 42
```

It can be combined with the existing lifecycle action:

```bash
python3 src/demo_layer6.py \
  --start-if-needed \
  --restart-at-end \
  --terminal gnome-terminal
```

The same capability exists in the library API:

```python
from layer6 import Layer6Client

with Layer6Client(start_if_needed=True, terminal="gnome-terminal") as l6:
    l6.ensure_mavlink()
    l6.submit_command([0.0, 0.0, 0.5, 0.0], mode="QHOVER")
```

and in the direct client CLI:

```bash
python3 src/layer6/client.py \
  --start-if-needed \
  --terminal gnome-terminal \
  --mode QHOVER \
  --u 0 0 0.5 0
```

### Restart/stop terminal ownership

The launcher now writes a stop marker before SIGINT. The three owned terminal
shells check this marker after their primary process returns and exit instead of
entering the diagnostic `exec bash` tail. For legacy/stuck shells, `stop()` then
escalates only the recorded terminal process groups through HUP, TERM and, as a
last resort, KILL. This prevents restart from leaving the old three windows open
while new windows are launched.

## TDDT L5 → L6 → L7 interface demo

A second, independent Python demo validates the interface contract rather than a flight mission:

```bash
python3 src/demo_tddt_l5_l7.py \
  --start-if-needed \
  --layer5-file config/layer5_decision_example.json \
  --layer7-output runtime/layer7_input_demo.json \
  --terminal gnome-terminal
```

To receive one Layer-5 packet over UDP:

```bash
python3 src/demo_tddt_l5_l7.py \
  --start-if-needed \
  --layer5-listen 127.0.0.1:14662 \
  --layer7-output runtime/layer7_input_demo.json \
  --terminal gnome-terminal
```

Optionally forward the generated `Layer7Input` to a Layer-7 UDP endpoint:

```bash
python3 src/demo_tddt_l5_l7.py \
  --layer5-file config/layer5_decision_example.json \
  --layer7-endpoint 127.0.0.1:14671
```

See `docs/LAYER6_FORMALISM_COMPLIANCE_v1.md` for the formalism-to-code mapping.

## Dynamic TDDT L5 -> L6 -> L7 demo (v20261007_1032)

`src/demo_tddt_l5_l7.py` now defaults to a **dynamic full-flight Layer-5 producer**.  It uses the Layer-5 JSON supplied by `--layer5-file` as a contract/template, generates a fresh `D_exec` for every flight phase (Ground, ARM, VTOL takeoff, Hover, Transition, Cruise, Approach, Landing, DISARM), submits each transaction through `Layer6Client`, waits for ArduPilot execution verification, and only then reconstructs and emits the corresponding canonical `Layer7Input`.

Typical run:

```bash
python3 src/demo_tddt_l5_l7.py \
  --start-if-needed \
  --layer5-file config/layer5_decision_example.json \
  --layer7-output runtime/layer7_input_demo.json \
  --layer7-trace runtime/layer7_input_demo.jsonl \
  --terminal gnome-terminal
```

- `runtime/layer7_input_demo.json` is the **latest** phase output and is overwritten after each phase.
- `runtime/layer7_input_demo.jsonl` contains **one Layer7Input per Layer-5 decision** for the complete dynamic mission.
- `--layer7-endpoint HOST:PORT` sends every phase output to Layer-7 over UDP.
- `--single-decision` restores the older one-packet contract-test behavior.
- The same ARM/mode/landing/DISARM verification semantics used by `demo_layer6.py` are applied here.
