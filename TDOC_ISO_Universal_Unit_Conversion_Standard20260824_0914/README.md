# TDOC ISO Universal Unit Conversion Standard v2 — Python Library

کتابخانهٔ مستقل پایتون برای تبدیل واحدهای تعریف‌شده در `TDOC_ISO_Universal_Unit_Conversion_Standard_v2.md` با حفظ تفکیک معماری سند میان **UnitConverter** و **Semantic Adapters**.

## نصب

```bash
python -m pip install .
```

برای توسعه/آزمون:

```bash
python -m pip install pytest
pytest -q
```

## API اصلی

```python
from tdoc_unit_converter import convert, UnitConverter
convert(36, "km/h", "m/s")          # 10.0
convert(20, "degC", "K")            # 293.15
convert(900, "hPa", "Pa")           # 90000
convert(200, "Wh", "J")             # 720000
```

`UnitConverter.convert(...)` یک `ConversionResult` شامل value/unit/quantity_kind/canonical_unit/conversion_class/uncertainty/audit برمی‌گرداند.

## هوانوردی و تبدیل‌های معنایی

```python
from tdoc_unit_converter.adapters import ned_to_enu, msl_to_agl, mach_to_tas
ned_to_enu((N,E,D))                   # (E,N,-D)
msl_to_agl(1500, terrain_msl=900)     # 600 m
mach_to_tas(0.3, temperature_k=288.15)
```

Adapterهای قبلی حفظ شده‌اند و Registry هوانوردی v2 اکنون با API قابل فراخوانی تکمیل شده است: NED↔ENU و FRD↔FLU، WGS84/ECEF/UTM و CRS/datum، مراجع عمودی، QNH/QFE/QNE، True/Magnetic/Grid، wind FROM/TO و head/cross/tail، IAS/CAS/EAS/TAS/Mach و pressure semantics، quaternion conventions/covariance، UTC/GPS/TAI/mission/FMI time و log-pressure.

`pyproj` فقط برای CRS/UTM/datum به‌صورت **lazy optional dependency** استفاده می‌شود تا runtime پایه برای edge سبک بماند:

```bash
python -m pip install '.[geodesy]'
```

## قواعد ایمنی معنایی

- ابعاد/quantity-kind ناسازگار رد می‌شوند؛ برای مثال `m→s` و `N*m torque→J energy`.
- `Mach→m/s` بدون سرعت صوت یا دمای لازم رد می‌شود.
- `MSL↔AGL` نیازمند ارتفاع terrain در همان vertical datum است.
- تبدیل frame در UnitConverter مخفی نمی‌شود.
- دمای مطلق و اختلاف دما دو quantity-kind متفاوت هستند.

## CLI

```bash
tdoc-convert 36 'km/h' 'm/s'
tdoc-convert 20 degC K
```

## ساختار

- `src/tdoc_unit_converter/registry.py`: رجیستری واحدها
- `converter.py`: تبدیل strict و uncertainty scaling
- `adapters.py`: تبدیل‌های معنایی/هوانوردی
- `errors.py`: خطاهای استاندارد
- `tests/`: آزمون‌ها
- `doc/`: سند مبنا


## مثال پیشرفته

```bash
PYTHONPATH=src python examples/advance_usage.py
```

این مثال UTM، quaternion، UTC/GPS/TAI، QNH/QFE/QNE، IAS/CAS/EAS/TAS، pressure semantics، True/Grid، wind components و log-pressure را پوشش می‌دهد.


## 2.2 adapter completion
The remaining TDOC v2 registry gaps are implemented with explicit `DataSemanticAdapter`, Euler/rate attitude APIs, and bidirectional geodesy/local-frame/body-navigation APIs. Core transforms remain pure Python; `pyproj` is still optional/lazy only for general projected CRS/datum work.

## Stage-7 optional strict boundary for TDDT Layer-3 through Layer-7

See `README_STAGE7_REPAIR_AND_SITE_VALIDATION_FA.md`. Use `tdoc_unit_converter.boundary.convert_boundary_envelope` only when the producer supplies explicit source/role/semantic/unit/frame/epoch/covariance/transaction/snapshot declarations. This is a **diagnostic conversion contract**, not Native Pilot certification or actuator binding. Existing converter/adapters remain compatible.
