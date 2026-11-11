# Independent all-formula scenarios

This directory contains two independent FMI scenario runners:

- `mesa_nav_all_formulas_scenario.py`
- `wingmpet_all_formulas_scenario.py`

Edit the `SCENARIO` dictionary at the top of the corresponding file. Each runner discovers all formula operations from the FMU catalog, creates a complete per-operation input dictionary, executes the FMU only through FMI, and builds a result dictionary with `inputs`, `outputs`, and the TDDT Layer-2 packet.

Run from the package root:

```bash
PYTHONPATH=python python3 test-senarios/mesa_nav_all_formulas_scenario.py
PYTHONPATH=python python3 test-senarios/wingmpet_all_formulas_scenario.py
```

Generated dictionary snapshots:

- `MESA_NAV_all_formulas_scenario_output.json`
- `WingMPET_all_formulas_scenario_output.json`

The scripts do not introduce fast/slow loops and do not couple the two twins.


به‌روزرسانی انجام شد. در پوشهٔ جدید `test-senarios` دو اسکریپت کاملاً مستقل اضافه شده‌اند؛ هرکدام فقط FMU مربوط به خود را مدیریت می‌کنند و هیچ coupling یا حلقهٔ تند/کندی ایجاد نمی‌کنند.

### اسکریپت‌های جدید

برای `MESA_NAV.fmu`:

```text
test-senarios/mesa_nav_all_formulas_scenario.py
```

برای `WingMPET.fmu`:

```text
test-senarios/wingmpet_all_formulas_scenario.py
```

در ابتدای هر اسکریپت یک دیکشنری قابل‌ویرایش به نام زیر وجود دارد:

```python
SCENARIO = {
    "name": "...",
    "context_id": "...",
    "stage_id": "...",
    "communication_step": ...,
    "common_inputs": {...},
    "operation_inputs": {...},
}
```

برای آینده می‌توانید ورودی عمومی یا ورودی مختص یک `operation_code` را مستقیماً در همین دیکشنری تغییر دهید. مثلاً:

```python
"operation_inputs": {
    5: {
        "altitude": 1500.0,
        "airspeed": 15.0,
    },
    6: {
        "wind_x": 6.0,
        "wind_y": -2.0,
        "wind_h": 0.5,
    },
}
```

هر اسکریپت کاتالوگ فرمولی FMU را در زمان اجرا می‌خواند و **تمام operationهای موجود** را اجرا می‌کند. برای هر operation ساختار زیر تولید می‌شود:

```python
{
    "inputs": {...},
    "outputs": {...},
    "packet": {
        "state": {...},
        "uncertainty": {...},
        "quality": {...},
        "validity": {...},
        "reliability": {...},
        "context": {...},
        "timestamp": ...,
        "provenance": {...},
    },
    "all_outputs_finite": True,
}
```

بنابراین علاوه بر خروجی خام فرمول‌ها، packet کامل Layer 2 شبکهٔ Dynamic N-Twin نیز قابل مشاهده است.

### نتیجهٔ آزمون

`MESA_NAV.fmu`:

```text
operations       = 22
formula outputs  = 133
all executed     = True
all finite       = True
```

`WingMPET.fmu`:

```text
operations       = 26
formula outputs  = 266
all executed     = True
all finite       = True
```

در مجموع:

[
22+26=48
]

operation و

[
133+266=399
]

خروجی فرمولی از طریق FMI اجرا و دریافت شدند.

هر اسکریپت علاوه بر چاپ دیکشنری کامل، snapshot آن را نیز به JSON ذخیره می‌کند:

```text
test-senarios/MESA_NAV_all_formulas_scenario_output.json
test-senarios/WingMPET_all_formulas_scenario_output.json
```

اجرای مستقیم:

```bash
PYTHONPATH=python \
python3 test-senarios/mesa_nav_all_formulas_scenario.py
```

و:

```bash
PYTHONPATH=python \
python3 test-senarios/wingmpet_all_formulas_scenario.py
```

----
این دو اسکریپت فعلی در `test-senarios` بیشتر نقش **«پنجرهٔ مشاهده و آزمون همهٔ فرمول‌های هر دوقلو»** را دارند، نه اینکه به‌تنهایی یک پیش‌بینی زمانی کامل از آینده انجام دهند.

اسکریپت `mesa_nav_all_formulas_scenario.py` تمام ۲۲ عملیات MESA-NAV را از کاتالوگ FMU کشف و اجرا می‌کند؛ اسکریپت `wingmpet_all_formulas_scenario.py` نیز تمام ۲۶ عملیات WingMPET را همین‌طور اجرا می‌کند. برای هر operation، ورودی‌ها، خروجی‌ها و packet کامل Layer 2 شامل `state`, `uncertainty`, `quality`, `validity`, `reliability`, `context`, `timestamp`, `provenance` ثبت می‌شود.  

## این دو دوقلو دقیقاً چه چیزی باید به ما بدهند؟

### `MESA_NAV.fmu`

وظیفهٔ اصلی این Twin این است که پاسخ دهد:

> **«پهپاد اکنون احتمالاً کجاست، چگونه حرکت می‌کند، باد و بایاس‌های حسگر چه هستند، و چقدر به این تخمین اعتماد داریم؟»**

ورودی‌های آیندهٔ آن می‌توانند شامل مواردی مانند:

```text
IMU
barometer
pitot
magnetometer
airspeed
wind prior
pressure
terrain/map context
previous navigation state
dt
```

باشند.

خروجی آن باید عمدتاً شامل این دسته‌ها باشد:

* موقعیت تخمینی؛
* سرعت تخمینی؛
* وضعیت جهت‌گیری یا خطاهای آن؛
* باد تخمینی؛
* biasهای حسگر؛
* residualها؛
* NIS و سازگاری اندازه‌گیری؛
* covariance/uncertainty؛
* وضعیت مشاهده‌پذیری؛
* اعتبار لنگرهای محیطی؛
* reliability و validity تخمین.

برای مثال سناریوی فعلی MESA-NAV ورودی‌های مشترکی مثل `air_density=1.10`، `airspeed=13`، فشار `90000 Pa` و prior باد NED را به عملیات مناسب می‌دهد. 

بنابراین MESA-NAV اساساً **estimator** است، نه شبیه‌ساز مسیر آینده.

---

### `WingMPET.fmu`

وظیفهٔ این Twin متفاوت است. باید پاسخ دهد:

> **«اگر پهپاد از این وضعیت با این سرعت، باد، ارتفاع، جرم، محدودیت‌ها و فرمان‌ها ادامه دهد، حرکت، مسیر، زمان، انرژی و ایمنی آن چه خواهد شد؟»**

ورودی‌های نمونهٔ فعلی آن شامل:

```text
altitude
terrain_height
air_density
pressure
temperature
humidity
airspeed
wind_x
wind_y
wind_h
dt
```

است. 

در اجرای واقعی، موارد دیگری نیز از پیکربندی Phoenix و operationهای خاص تأمین می‌شوند، مانند:

* جرم؛
* مساحت و دهانه بال؛
* ضرایب آیرودینامیکی؛
* بانک و ضریب برآ؛
* RPM؛
* مشخصات ملخ؛
* موتور؛
* باتری؛
* SoC؛
* فاصله زمین و موانع.

خروجی WingMPET شامل چیزهایی مانند:

* موقعیت و سرعت بعدی؛
* سرعت زمینی؛
* lift و drag؛
* dynamic pressure؛
* stall margin؛
* turn radius؛
* thrust؛
* propeller power؛
* motor current؛
* battery current/voltage؛
* SoC؛
* انرژی تجمعی؛
* زمان پرواز؛
* طول مسیر؛
* clearance؛
* feasibility؛
* objective/cost

است.

یعنی WingMPET همان Twinی است که برای **what-if simulation** آینده مناسب‌تر است.

---

# پس آینده چگونه تخمین زده می‌شود؟

نکتهٔ اصلی همین‌جاست: اجرای فعلی همهٔ فرمول‌ها فقط می‌گوید:

```text
برای این مجموعه ورودی
→ هر فرمول چه خروجی‌ای می‌دهد؟
```

اما «آینده» نیاز به یک زنجیرهٔ زمانی دارد.

مثلاً فرض کنیم اکنون داریم:

[
t_0=0
]

و حالت فعلی پهپاد:

[
X(t_0)
]

باشد.

WingMPET یک گام آینده را محاسبه می‌کند:

[
X(t_1)
======

F_{\rm Wing}
\left(
X(t_0),
U(t_0),
E(t_0),
\Delta t
\right)
]

که در آن:

* (X): حالت پهپاد؛
* (U): ورودی/فرمان؛
* (E): محیط مثل باد و اتمسفر؛
* (\Delta t): گام زمانی.

سپس خروجی مرحلهٔ قبل باید ورودی مرحلهٔ بعد شود:

[
X(t_2)
======

F_{\rm Wing}
\left(
X(t_1),
U(t_1),
E(t_1),
\Delta t
\right).
]

و به همین صورت:

[
X(t_{k+1})
==========

F_{\rm Wing}
\left(
X(t_k),
U(t_k),
E(t_k),
\Delta t
\right).
]

این همان rollout زمانی است.

## یک مثال ساده

فرض کنید می‌خواهیم ۶۰ ثانیه آینده را با:

```text
dt = 1 s
```

شبیه‌سازی کنیم.

در لحظه صفر:

```python
state = {
    "x": 0.0,
    "y": 0.0,
    "h": 1000.0,
    "va_x": 13.0,
    "va_y": 0.0,
    "va_h": 0.0,
}
```

باد:

```python
wind = {
    "wind_x": 3.0,
    "wind_y": 1.0,
    "wind_h": 0.0,
}
```

WingMPET برای گام اول می‌دهد:

```text
state(t=1)
energy(t=1)
SoC(t=1)
clearance(t=1)
...
```

این خروجی به ورودی گام بعدی تبدیل می‌شود:

```text
state(t=1)
      ↓
WingMPET
      ↓
state(t=2)
      ↓
WingMPET
      ↓
state(t=3)
...
```

تا:

[
t=60s.
]

نتیجه دیگر یک خروجی منفرد نیست؛ یک trajectory کامل است:

[
{
X(t_0),X(t_1),...,X(t_{60})
}.
]

---

# MESA-NAV چگونه آینده را تخمین می‌زند؟

MESA-NAV کمی متفاوت عمل می‌کند.

این Twin معمولاً یک **پیش‌بینی کوتاه‌مدت حالت** انجام می‌دهد:

[
\hat X^{-}_{k+1}
================

f
\left(
\hat X^{+}_k,
IMU_k,
\Delta t
\right)
]

یعنی بر اساس:

* حالت فعلی؛
* IMU؛
* airspeed؛
* مدل باد؛
* مدل مکانیکی؛

می‌گوید در گام بعدی وضعیت احتمالاً کجا خواهد بود.

بعد وقتی اندازه‌گیری جدید می‌رسد:

[
z_{k+1},
]

آن prediction تصحیح می‌شود:

[
\hat X^{+}_{k+1}
================

\hat X^{-}_{k+1}
+
K r.
]

پس MESA-NAV دو مفهوم آینده دارد:

```text
Prediction
    ↓
predicted navigation state

Measurement arrives
    ↓
Correction
    ↓
estimated actual state
```

در نتیجه:

* **MESA-NAV آینده را برای حفظ تخمین ناوبری پیش‌بینی می‌کند.**
* **WingMPET آینده را برای بررسی رفتار فیزیکی و مسیر شبیه‌سازی می‌کند.**

---

# ارتباط منطقی این دو Twin

در اجرای واقعی، این دو باید مستقل بمانند ولی دادهٔ آن‌ها در Layer 3 به یکدیگر مرتبط شود:

```text
Sensors / physical UAV
        ↓
MESA-NAV
        ↓
estimated current state
        ↓
Layer 3
        ↓
WingMPET
        ↓
future simulation
```

مثلاً MESA-NAV می‌گوید:

```python
{
    "x": 1532.1,
    "y": 847.8,
    "h": 1025.3,
    "vn": 11.8,
    "ve": 4.1,
    "vd": -0.2,
    "wind_n": 3.2,
    "wind_e": 0.8,
    "position_uncertainty_m": 8.3
}
```

این وضعیت می‌تواند نقطهٔ شروع WingMPET شود.

WingMPET سپس می‌گوید:

```python
{
    "after_30_s": {
        "x": ...,
        "y": ...,
        "h": ...,
        "energy_J": ...,
        "SoC": ...,
        "terrain_clearance_m": ...,
        "feasible": ...
    }
}
```

پس رابطهٔ مفهومی این است:

[
\boxed{
\text{MESA-NAV}
===============

\text{Where am I?}
}
]

و:

[
\boxed{
\text{WingMPET}
===============

\text{What happens if I continue this way?}
}
]

---

# نقش دو اسکریپت فعلی

اسکریپت‌های فعلی هنوز این زنجیرهٔ زمانی را ایجاد نمی‌کنند.

آن‌ها عمداً تمام operationها را یک‌بار اجرا می‌کنند:

```text
Operation 1
Operation 2
Operation 3
...
Operation 22 / 26
```

و برای هرکدام:

```python
{
    "inputs": {...},
    "outputs": {...},
    "packet": {...}
}
```

می‌سازند. این رفتار در کد صریح است: operationهای FMU کشف می‌شوند و برای هر operation یک `evaluate_packet()` مستقل اجرا می‌شود. 

بنابراین این دو اسکریپت برای این موارد بسیار مناسب‌اند:

* شناخت API؛
* دیدن تمام ورودی‌های Twin؛
* دیدن تمام خروجی‌ها؛
* تست هر فرمول؛
* ساخت یک scenario آزمایشی؛
* بررسی uncertainty/quality/validity/reliability؛
* بررسی اینکه تغییر یک ورودی روی یک فرمول چه اثری دارد.

اما هنوز برای این هدف ساخته نشده‌اند:

> **«از حالت فعلی، ۲۴۰ ثانیه آیندهٔ پرواز را گام‌به‌گام شبیه‌سازی کن.»**

---

## یک تفاوت بسیار مهم

در خروجی‌های فعلی می‌بینید:

```text
quality = 1.0
reliability = 0.9
```

این به معنی «۹۰٪ احتمال درست‌بودن آینده» نیست.

برای مثال packet فعلی صراحتاً reliability را با نوع:

```text
verification_prior_no_field_evidence
```

ثبت می‌کند. 

یعنی این reliability فعلاً از verification و perturbation مصنوعی آمده، نه از دادهٔ پرواز واقعی. همین محدودیت در طراحی Layer 2 نیز صریحاً ذکر شده است. 

---

# معماری مناسب برای «دیدن آینده»

در مرحلهٔ بعد باید روی همین دو Twin، بدون تغییر FMUها، سه نوع runner در Python ساخته شود:

```text
1. MESA_NAV State Estimation Runner
       ↓
current best state

2. WingMPET Forward Simulation Runner
       ↓
t+1 ... t+N

3. Scenario/What-if Runner
       ↓
Scenario A
Scenario B
Scenario C
```

برای نمونه:

```text
Current state
      ↓
 ┌────┼────────────┐
 ↓    ↓            ↓
No wind       Tailwind       Crosswind
 ↓               ↓              ↓
WingMPET      WingMPET       WingMPET
 ↓               ↓              ↓
future A       future B       future C
```

و بعد مقایسه کنیم:

```text
arrival time
energy
path length
minimum clearance
SoC
stall margin
risk
validity
uncertainty
```

این دقیقاً جایی است که WingMPET از «محاسبه‌گر فرمول‌ها» به یک **digital-twin forward simulator** تبدیل می‌شود.

### جمع‌بندی

| Twin                 | سؤال اصلی                                        | ورودی اصلی                                | خروجی اصلی                    |
| -------------------- | ------------------------------------------------ | ----------------------------------------- | ----------------------------- |
| MESA-NAV             | اکنون واقعاً کجا و در چه وضعیتی هستم؟            | حسگرها + prior + نقشه                     | state estimate + uncertainty  |
| MESA-NAV propagation | یک گام بعد احتمالاً کجا هستم؟                    | state + IMU + dt                          | predicted state               |
| WingMPET             | اگر این وضعیت ادامه یابد چه می‌شود؟              | state + environment + aircraft + controls | future physical state         |
| WingMPET rollout     | در (N) گام آینده چه اتفاقی می‌افتد؟              | خروجی هر گام → ورودی گام بعد              | trajectory/time/energy/safety |
| دو اسکریپت فعلی      | آیا همهٔ فرمول‌ها قابل دسترسی و قابل مشاهده‌اند؟ | `SCENARIO`                                | dictionary همهٔ operationها   |

پس **دو اسکریپت فعلی ابزار مشاهده و آزمون جامع Twinها هستند؛ نه هنوز موتور rollout آینده**. مرحلهٔ منطقی بعدی این است که همین FMUهای مستقل را بدون دستکاری، از Python در یک زنجیرهٔ زمانی قرار دهیم تا `MESA_NAV` حالت جاری را تخمین بزند و `WingMPET` از آن حالت، چند ثانیه یا چند دقیقه آینده را در چند سناریوی متفاوت شبیه‌سازی کند.
