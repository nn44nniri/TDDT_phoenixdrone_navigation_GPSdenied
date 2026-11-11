# راهنمای جامع بهره‌برداری از دوقلوی اقلیمی MicroMetTwin

## ۱. معرفی

این بسته یک دوقلوی اقلیمی سبک‌وزن بر پایه فرمالیسم **MicroMet** است که به‌صورت یک **FMU بومی مستقل** برای استاندارد **FMI 2.0 Co-Simulation** پیاده‌سازی شده است. هسته محاسباتی با C++17 نوشته شده، با CMake کامپایل می‌شود و فایل نهایی `MicroMetTwin.fmu` شامل باینری بومی Linux x86-64 است.

پایتون در این بسته نقش «مدیر اجرا» را دارد و برای دریافت پیش‌بینی Open-Meteo، ساخت شبکه مکانی، آماده‌سازی ورودی‌های زمانی و جغرافیایی، فراخوانی FMU از طریق FMI، تولید CSV و ساخت گزارش تعاملی استفاده می‌شود. روابط فیزیکی و مکانی شماره‌گذاری‌شده فرمالیسم MicroMet در هسته بومی FMU اجرا می‌شوند.

دوقلو **پیش‌بینی مستقل هواشناسی تولید نمی‌کند**. داده پیش‌بینی کم‌تفکیک را دریافت می‌کند و برای هر مختصات، زمان و ارتفاع، میدان اقلیمی محلی را با درون‌یابی مکانی و اصلاحات توپوگرافی محاسبه می‌کند.

---

## ۲. قابلیت‌های اصلی

بسته می‌تواند:

- داده ساعتی پیش‌بینی را برای مرکز و رأس‌های یک محدوده جغرافیایی از Open-Meteo دریافت کند؛
- پس از پایان دانلود، تمام پردازش‌ها را بدون اتصال اینترنت انجام دهد؛
- شبکه‌ای از تمام سلول‌های داخل چندضلعی منطقه بسازد؛
- برای هر سلول، زمان و ارتفاع، FMU را از طریق رابط استاندارد FMI فراخوانی کند؛
- دما، رطوبت، باد، فشار، بارش، ابر و تابش را محاسبه کند؛
- کمیت‌های میانی فرمالیسم، مانند وزن Barnes، نقطه شبنم، شیب مؤثر و ضریب باد را نیز بازگرداند؛
- تخمین لحظه‌ای وضعیت اقلیمی یک پهپاد را به‌صورت دیکشنری JSON تولید کند؛
- ارتفاع از سطح زمین و ارتفاع از سطح دریا را به‌طور صریح تفکیک کند؛
- درخواست ارتفاع زیر زمین را با خطای ساخت‌یافته رد کند؛
- یک گزارش تعاملی دوبعدی در ارتفاع ثابت و در تمام سطح چندضلعی منطقه تولید کند.

---

## ۳. ساختار پوشه‌ها

```text
climate_twin_vYYYYMMDD_HHmm/
├── CMakeLists.txt
├── README.md
├── README_FA.md
├── requirements.txt
├── modelDescription.xml
├── include/
│   ├── fmi2Functions.h
│   └── micromet.hpp
├── src/
│   ├── fmi2_wrapper.cpp
│   └── micromet.cpp
├── fmu/
│   └── MicroMetTwin.fmu
├── python/
│   ├── fmu_client.py
│   ├── fmi_variables.py
│   ├── climate_io.py
│   ├── climate_errors.py
│   ├── estimate_climate.py
│   └── run_fmu_dataset.py
├── scripts/
│   ├── build_fmu.sh
│   ├── download_openmeteo.py
│   └── generate_plotly_3d_report.py
├── config/
│   ├── region.json
│   └── guid.txt
├── climate_dataset/
├── reports/
├── docs/
│   ├── micromet_formalism.md
│   ├── FMI_CONFORMANCE.md
│   ├── FMI_VARIABLES.md
│   └── FORMULA_COVERAGE.md
└── tests/
```

نقش اجزای اصلی:

- `src/micromet.cpp`: هسته محاسباتی فرمالیسم MicroMet؛
- `src/fmi2_wrapper.cpp`: توابع صادرشده و ماشین حالت FMI 2.0؛
- `modelDescription.xml`: تعریف متغیرها، Value Referenceها و مشخصات FMU؛
- `python/fmu_client.py`: واردکننده FMI و فراخوان باینری FMU از پایتون؛
- `region/region_v1.geojson`: محدوده استاندارد GeoJSON مبتنی بر WGS 84، شامل چندضلعی مرز، مرکز، مبدأ و مقصد؛
- `climate_dataset/`: داده ورودی آفلاین، سلول‌های شبکه و خروجی‌های FMU؛
- `reports/`: گزارش‌های HTML؛
- `docs/FORMULA_COVERAGE.md`: نگاشت روابط (1) تا (33) به پیاده‌سازی FMU؛
- `docs/FMI_CONFORMANCE.md`: جزئیات انطباق رابط با FMI 2.0.

---

## ۴. پیش‌نیازها

### ۴.۱. ابزارهای ساخت

- Linux x86-64؛
- CMake نسخه 3.16 یا جدیدتر؛
- کامپایلر دارای پشتیبانی C++17، مانند GCC یا Clang؛
- ابزار `zip`؛
- Python 3.9 یا جدیدتر.

### ۴.۲. بسته‌های پایتون

```bash
python3 -m pip install -r requirements.txt
```

وابستگی‌های اصلی:

- `pandas>=2.0`
- `plotly>=5.18`

اسکریپت فراخوانی FMI از `ctypes` استاندارد پایتون استفاده می‌کند و به FMPy وابسته نیست.

---

## ۵. ساخت FMU

از ریشه بسته اجرا کنید:

```bash
chmod +x scripts/build_fmu.sh
./scripts/build_fmu.sh
```

این فرمان:

1. پروژه CMake را در حالت Release پیکربندی می‌کند؛
2. فایل بومی `MicroMet.so` را می‌سازد؛
3. ساختار استاندارد FMU را ایجاد می‌کند؛
4. فایل زیر را تولید می‌کند:

```text
fmu/MicroMetTwin.fmu
```

ساختار داخلی FMU:

```text
MicroMetTwin.fmu
├── modelDescription.xml
├── binaries/
│   └── linux64/
│       └── MicroMet.so
└── resources/
    └── micromet_formalism.md
```

باینری فعلی مخصوص `linux64` است. برای Windows، macOS یا ARM باید همین کد با ابزار هدف کامپایل و در پوشه باینری متناظر قرار داده شود.

---

## ۶. رابط استاندارد FMI

رابط واقعی FMI داخل باینری FMU پیاده‌سازی شده است، نه داخل اسکریپت پایتون. فایل `src/fmi2_wrapper.cpp` توابع استاندارد Co-Simulation را صادر می‌کند و `python/fmu_client.py` فقط نقش واردکننده و master را دارد.

چرخه اجرا:

```text
fmi2Instantiate
    ↓
fmi2SetupExperiment
    ↓
fmi2EnterInitializationMode
    ↓
fmi2SetReal
    ↓
fmi2ExitInitializationMode
    ↓
fmi2SetReal → fmi2DoStep → fmi2GetReal
    ↓
fmi2Terminate
    ↓
fmi2FreeInstance
```

رابط موارد زیر را کنترل می‌کند:

- GUID و نوع Co-Simulation؛
- ترتیب صحیح حالت‌های چرخه عمر؛
- جلوگیری از اجرای `fmi2DoStep` پیش از مقداردهی اولیه؛
- جلوگیری از نوشتن روی خروجی‌ها؛
- بازنشانی مقادیر پیش‌فرض؛
- وضعیت آخرین زمان موفق اجرای FMU.

جزئیات در `docs/FMI_CONFORMANCE.md` قرار دارد.

---

## ۷. انطباق با فرمالیسم MicroMet

تمام روابط شماره‌گذاری‌شده (1) تا (33) در هسته بومی پیاده‌سازی شده‌اند، شامل:

- وزن‌دهی و تصحیح دوگذره Barnes؛
- اصلاح ارتفاعی دما؛
- فشار بخار اشباع، رطوبت و نقطه شبنم؛
- تبدیل مؤلفه‌های باد؛
- شیب، جهت شیب و انحنای توپوگرافی؛
- اصلاح سرعت و جهت باد با زمین؛
- کسر ابر؛
- هندسه خورشیدی و تابش کوتاه‌موج؛
- گسیلندگی و تابش بلندموج؛
- فشار سطح؛
- تصحیح بارش با ارتفاع.

عملیات دریافت داده، کنترل کیفیت فایل، انتخاب زمان، ساخت grid، اعتبارسنجی AGL/MSL و تولید گزارش در پایتون باقی می‌مانند؛ زیرا این موارد عملیات orchestration هستند و جزء روابط شماره‌گذاری‌شده فیزیکی نیستند.

نگاشت کامل در `docs/FORMULA_COVERAGE.md` مستند شده است.

---

## ۸. تعریف منطقه جغرافیایی

منطقه در `region/region_v1.geojson` تعریف می‌شود:

```json
{
  "name": "sample_728km2_region",
  "center": {
    "latitude": 47.525190299169076,
    "longitude": 44.29738495970912
  },
  "vertices": [
    {"name": "V1", "latitude": 47.35396910622598, "longitude": 44.28084595209614},
    {"name": "V2", "latitude": 47.51372452708679, "longitude": 44.5508911097493},
    {"name": "V3", "latitude": 47.69641029737404, "longitude": 44.314005121798886},
    {"name": "V4", "latitude": 47.53609718702575, "longitude": 44.04377066488155}
  ]
}
```

می‌توان مرکز و رأس‌ها را با مختصات منطقه موردنظر جایگزین کرد. اسکریپت شبکه تمام سلول‌های داخل چندضلعی رأس‌ها را تولید می‌کند، نه فقط پنج نقطه مرکز و رأس‌ها.

---

## ۹. دریافت پیش‌بینی Open-Meteo

### ۹.۱. دریافت ۲۴ ساعت از زمان فعلی

```bash
python3 scripts/download_openmeteo.py \
  --region region/region_v1.geojson \
  --hours 24
```

زمان آغاز پیش‌فرض، ساعت جاری گرد‌شده است.

### ۹.۲. تعیین فایل خروجی

```bash
python3 scripts/download_openmeteo.py \
  --region region/region_v1.geojson \
  --output climate_dataset/openmeteo_24h.csv \
  --hours 24
```

### ۹.۳. دریافت بازه دلخواه

مثال: ساعت ۱۷ روز مشخص تا ۲۴ ساعت بعد:

```bash
python3 scripts/download_openmeteo.py \
  --region region/region_v1.geojson \
  --start "2026-08-04T17:00:00-04:00" \
  --hours 24 \
  --output climate_dataset/openmeteo_custom.csv
```

### ۹.۴. منطقه زمانی

```bash
python3 scripts/download_openmeteo.py \
  --timezone auto \
  --hours 24
```

پس از ذخیره فایل، ساخت grid، اجرای FMU، تخمین لحظه‌ای و گزارش‌گیری به اینترنت نیاز ندارند.

---

## ۱۰. ساخت مجموعه‌داده شبکه‌ای با FMU

اسکریپت `python/run_fmu_dataset.py`:

1. چندضلعی منطقه را می‌خواند؛
2. شبکه مکانی را با تفکیک انتخابی می‌سازد؛
3. ارتفاع زمین هر سلول را تعیین یا تخمین می‌زند؛
4. برای هر زمان، سلول و تراز ارتفاعی، FMU را از طریق FMI فراخوانی می‌کند؛
5. فایل سلول‌ها و مجموعه‌داده کامل را ذخیره می‌کند.

### ۱۰.۱. ساخت لایه‌های ارتفاع از سطح زمین

```bash
python3 python/run_fmu_dataset.py \
  --fmu fmu/MicroMetTwin.fmu \
  --input climate_dataset/example_offline_input_spatial.csv \
  --region region/region_v1.geojson \
  --resolution-m 5000 \
  --elevation-ground-level-levels 100,500,1000 \
  --grid-output climate_dataset/grid_cells.csv \
  --output climate_dataset/micromet_grid_24h.csv
```

### ۱۰.۲. ساخت لایه‌های ارتفاع مطلق از سطح دریا

```bash
python3 python/run_fmu_dataset.py \
  --input climate_dataset/example_offline_input_spatial.csv \
  --resolution-m 1000 \
  --elevation-sea-level-levels 300,500,600,700,800,1200 \
  --output climate_dataset/micromet_grid_msl.csv
```

### ۱۰.۳. تنظیمات توپوگرافی دستی

در نبود اتصال مستقیم DEM می‌توان مقادیر ثابت آزمایشی تعیین کرد:

```bash
python3 python/run_fmu_dataset.py \
  --elevation-ground-level-levels 100 \
  --slope-deg 8 \
  --aspect-deg 210 \
  --curvature 0.1
```

برای کاربرد عملیاتی باید ارتفاع زمین، مشتقات DEM و انحنا برای هر سلول از DEM یا COG خوانده شوند.

### ۱۰.۴. تنظیم شعاع Barnes

```bash
python3 python/run_fmu_dataset.py \
  --barnes-filter-km 25
```

تفکیک کمتر، تعداد سلول‌ها و تعداد فراخوانی‌های FMU را افزایش می‌دهد.

---

## ۱۱. مرجع ارتفاع: AGL و MSL

دو مرجع ارتفاع به‌صورت صریح پشتیبانی می‌شوند:

- **AGL**: ارتفاع از سطح زمین محلی؛
- **MSL**: ارتفاع مطلق از میانگین سطح دریا.

رابط مبهم `--elevation` حذف شده است. رابطه تبدیل:

```text
altitude_msl_m = ground_elevation_m + altitude_agl_m
```

ستون‌های مهم مجموعه‌داده:

```text
ground_elevation_m
altitude_agl_m
altitude_msl_m
vertical_reference
```

نمونه: اگر ارتفاع زمین 120 متر MSL و پهپاد 100 متر AGL باشد، ارتفاع مطلق پهپاد 220 متر MSL خواهد بود.

در نبود DEM واقعی، ارتفاع زمین از ارتفاع نقاط منبع هواشناسی با Barnes تخمین زده می‌شود و یک proxy است. برای عملیات واقعی، مقدار DEM باید جایگزین شود.

---

## ۱۲. تخمین لحظه‌ای وضعیت اقلیمی پهپاد

اسکریپت `python/estimate_climate.py` یک زمان، موقعیت و ارتفاع را دریافت می‌کند و تمام خروجی‌های قابل دسترسی FMU را در یک دیکشنری JSON چاپ می‌کند.

### ۱۲.۱. تخمین در ۱۰۰۰ متر بالاتر از زمین

```bash
python3 python/estimate_climate.py \
  --fmu fmu/MicroMetTwin.fmu \
  --input climate_dataset/example_offline_input_spatial.csv \
  --time "2026-08-04T17:00:00" \
  --latitude 47.525190299169076 \
  --longitude 44.29738495970912 \
  --elevation-ground-level 1000
```

### ۱۲.۲. تخمین در ارتفاع مطلق از سطح دریا

```bash
python3 python/estimate_climate.py \
  --input climate_dataset/example_offline_input_spatial.csv \
  --time "2026-08-04T17:00:00" \
  --latitude 47.525190299169076 \
  --longitude 44.29738495970912 \
  --elevation-sea-level 300
```

### ۱۲.۳. تعیین ارتفاع زمین از DEM

```bash
python3 python/estimate_climate.py \
  --time "2026-08-04T17:00:00" \
  --latitude 47.525190299169076 \
  --longitude 44.29738495970912 \
  --elevation-ground-level 100 \
  --ground-elevation-m 125.4
```

### ۱۲.۴. خروجی فشرده

```bash
python3 python/estimate_climate.py ... --compact
```

خروجی شامل وضعیت مرجع ارتفاع، دما، رطوبت، نقطه شبنم، باد، فشار، بارش، ابر، تابش و کمیت‌های میانی فرمالیسم است.

---

## ۱۳. خطاهای ساخت‌یافته

درخواست‌های نامعتبر به‌جای traceback خام، دیکشنری JSON تولید می‌کنند.

### ۱۳.۱. ارتفاع زیر سطح زمین

اگر:

```text
requested_altitude_msl_m < ground_elevation_msl_m
```

باشد، FMU فراخوانی نمی‌شود و خطای زیر بازگردانده می‌شود:

```json
{
  "ok": false,
  "error": {
    "code": "CLIMATE_ALTITUDE_BELOW_GROUND",
    "message": "Requested MSL altitude is below local ground.",
    "details": {
      "requested_altitude_msl_m": 100.0,
      "ground_elevation_msl_m": 120.05,
      "computed_altitude_agl_m": -20.05
    }
  }
}
```

ابزار با کد خروج `2` پایان می‌یابد.

### ۱۳.۲. وضعیت داخلی فرمالیسم

خروجی‌های تشخیصی FMU:

```text
formula_valid
formula_error_code
enabled_source_count
barnes_second_pass_applied
fmi_time_s
```

کدهای `formula_error_code`:

```text
0 = محاسبه معتبر
1 = هیچ منبع هواشناسی فعال نیست
2 = خروجی غیرمتناهی یا نامعتبر تولید شده است
```

---

## ۱۴. تولید گزارش تعاملی دوبعدی

با وجود نام تاریخی فایل `generate_plotly_3d_report.py`، خروجی فعلی آن یک **گزارش دوبعدی در یک ارتفاع ثابت** است.

گزارش:

- تمام سطح چندضلعی `vertices` را نمایش می‌دهد؛
- محور افقی طول جغرافیایی و محور عمودی عرض جغرافیایی است؛
- رنگ، مقدار پارامتر را نشان می‌دهد؛
- نوار زمانی، تغییرات ساعتی را نمایش می‌دهد؛
- داده‌ها با IDW روی سطح منطقه درون‌یابی و خارج از چندضلعی ماسک می‌شوند.

### ۱۴.۱. گزارش در ارتفاع ۱۰۰ متر AGL

```bash
python3 scripts/generate_plotly_3d_report.py \
  --input climate_dataset/micromet_grid_24h.csv \
  --output-dir reports/parameters_3d \
  --elevation-ground-level 100
```

### ۱۴.۲. گزارش در ارتفاع ۳۰۰ متر MSL

```bash
python3 scripts/generate_plotly_3d_report.py \
  --input climate_dataset/micromet_grid_24h.csv \
  --output-dir reports/parameters_3d \
  --elevation-sea-level 300
```

### ۱۴.۳. انتخاب پارامترها

```bash
python3 scripts/generate_plotly_3d_report.py \
  --input climate_dataset/micromet_grid_24h.csv \
  --output-dir reports/parameters_3d \
  --elevation-ground-level 100 \
  --parameters temperature_local,relative_humidity_local,wind_speed_terrain
```

برای تمام پارامترها:

```bash
--parameters all
```

### ۱۴.۴. کیفیت سطح درون‌یابی

```bash
--raster-size 120 --idw-power 2
```

مقادیر ارتفاع را می‌توان با قالب‌های `100`، `100m` یا `0.1km` وارد کرد.

---

## ۱۵. خروجی‌های اقلیمی

خروجی‌های اصلی عبارت‌اند از:

- دمای محلی هوا؛
- رطوبت نسبی محلی؛
- سرعت و جهت باد زمینه‌ای؛
- سرعت و جهت باد اصلاح‌شده با توپوگرافی؛
- مؤلفه‌های افقی باد؛
- فشار سطح؛
- بارش اصلاح‌شده؛
- کسر ابر؛
- تابش کوتاه‌موج؛
- تابش بلندموج.

خروجی‌های میانی مهم:

- فاصله و وزن Barnes؛
- دمای مرجع؛
- فشار بخار اشباع و واقعی؛
- نقطه شبنم؛
- نرخ کاهش نقطه شبنم؛
- شیب و جهت شیب؛
- انحنای خام و مقیاس‌شده؛
- شیب در جهت باد؛
- ضریب وزن باد؛
- زاویه انحراف باد؛
- زوایای خورشیدی؛
- گذردهی مستقیم و پراکنده؛
- ضرایب گسیلندگی.

نام، واحد، علیت و Value Reference کامل در `modelDescription.xml` و `docs/FMI_VARIABLES.md` قرار دارد.

---

## ۱۶. جریان کامل بهره‌برداری

### گام ۱: نصب وابستگی‌ها

```bash
python3 -m pip install -r requirements.txt
```

### گام ۲: ساخت FMU

```bash
./scripts/build_fmu.sh
```

### گام ۳: دریافت پیش‌بینی

```bash
python3 scripts/download_openmeteo.py \
  --region region/region_v1.geojson \
  --output climate_dataset/openmeteo_24h.csv \
  --hours 24
```

### گام ۴: ساخت شبکه و مجموعه‌داده

```bash
python3 python/run_fmu_dataset.py \
  --input climate_dataset/openmeteo_24h.csv \
  --resolution-m 1000 \
  --dem-dir /media/p1/DigitalTwin/TDDT_navigation/datasets/CRP_map/code/boulder_multilayer_map_builder_v20260814_1214/outputs/region_v1_map \
  --elevation-ground-level-levels 100,200,300,400,500,600,700,800,900,1000 \
  --output climate_dataset/micromet_grid_24h.csv \
  --grid-output climate_dataset/grid_cells.csv
```

### گام ۵: تخمین لحظه‌ای پهپاد

```bash
python3 python/estimate_climate.py \
  --input climate_dataset/openmeteo_24h.csv \
  --dem-dir /media/p1/DigitalTwin/TDDT_navigation/datasets/CRP_map/code/boulder_multilayer_map_builder_v20260814_1214/outputs/region_v1_map \
  --time "2026-08-24T17:00:00" \
  --latitude 40.025 \
  --longitude -105.295615 \
  --elevation-ground-level 100
{
  "fmu_outputs": {
    "Du_Dt": 0.01254945993470705,
    "Dv_Dt": 0.018809623510591202,
    "Dw_Dt": 0.000593674285391909,
    "actual_vapor_pressure": 913.3045621501811,
    "actual_vapor_pressure_local": 899.45548963232,
    "air_density": 0.9458905117242286,
    "aspect_deg": 252.9410858154297,
    "aspect_rad": 4.414654761048774,
    "atmospheric_emissivity": 0.7547046837880967,
    "barnes_distance_m": 961.5174927017155,
    "barnes_second_pass_applied": 1.0,
    "barnes_weight": 0.997691379251508,
    "cloud_fraction": 0.13364226176799157,
    "coefficient_Xs": 0.4435457101004464,
    "coefficient_Ys": 0.11753982064383371,
    "coefficient_Zs": 0.7361627627999442,
    "continuity_residual": -3.8674838620523356e-08,
    "curvature_raw": 0.036900981876598904,
    "curvature_scaled": -0.005776687653064782,
    "dewpoint_lapse_rate": 0.0055072563135641645,
    "dewpoint_local": 5.439378909557604,
    "dewpoint_reference": 15.556483717027152,
    "dewpoint_source": 5.65955517909003,
    "diagnostic_vertical_wind": -1.6794132647365188,
    "diffuse_transmissivity": 0.03590579212480245,
    "direct_transmissivity": 0.46553029622734143,
    "du_dt": 0.0017533303344072083,
    "du_dx": 1.0663205223333527e-05,
    "du_dy": -5.178766494240428e-06,
    "du_dz": -0.006435456441645648,
    "dv_dt": 0.0017749223055848922,
    "dv_dx": -2.7825408418018506e-05,
    "dv_dy": -1.1224584321274378e-05,
    "dv_dz": -0.010022162582996508,
    "dw_dt": 0.0005816343971595889,
    "dw_dx": 5.677199751263906e-07,
    "dw_dy": -2.212611443273005e-06,
    "dw_dz": 5.227042593203279e-07,
    "enabled_source_count": 5.0,
    "fmi_time_s": 3.0,
    "formula_error_code": 0.0,
    "formula_valid": 1.0,
    "longwave_radiation": 351.321031060335,
    "precipitation_local": 0.08494095717074827,
    "precipitation_reference": 0.08348557909078201,
    "raw_relative_humidity_local": 23.99382315695266,
    "raw_shortwave_radiation": 404.0379416358658,
    "raw_surface_pressure_pa": 82172.8092362522,
    "raw_temperature_local": 27.858556928177382,
    "raw_wind_direction_terrain_rad": 0.4395231588802269,
    "raw_wind_speed_terrain": 5.379487190777617,
    "relative_humidity_local": 23.874701470032708,
    "rh700_used": 23.927498595697,
    "saturation_vapor_pressure": 3814.7036655503343,
    "saturation_vapor_pressure_local": 3748.696002919759,
    "saturation_vapor_pressure_source": 3814.7036655503343,
    "shortwave_radiation": 184.72633213675184,
    "slope_deg": 20.172861099243164,
    "slope_in_wind_direction": -0.2094444572582856,
    "slope_in_wind_direction_raw": -0.2094444572582856,
    "slope_rad": 0.3520828457292758,
    "solar_azimuth": 1.5470825271445647,
    "solar_declination": 0.1852992191749256,
    "solar_hour_angle": 1.3089969389957474,
    "solar_incidence": 0.9155601775951919,
    "solar_zenith": 1.252140228734911,
    "source_elevation_barnes": 1802.4858227331029,
    "surface_pressure_forecast_pa": 82528.60598583291,
    "surface_pressure_pa": 82172.8092362522,
    "temperature_local": 28.219864275857432,
    "temperature_reference": 39.79938145210316,
    "temporal_interp_t0_s": 0.0,
    "temporal_interp_t1_s": 3600.0,
    "terrain_dzdx_used": 0.3518010586185044,
    "terrain_dzdy_used": 0.10806597877033612,
    "vertical_interp_lower_level": 10.0,
    "vertical_interp_upper_level": 10.0,
    "wind_3dw_profile_mode": 0.0,
    "wind_3dw_valid": 1.0,
    "wind_direction_background_deg": 19.444640319109926,
    "wind_direction_background_rad": 0.33937299543450905,
    "wind_direction_diversion": 0.10015016344571787,
    "wind_direction_terrain_deg": 32.70543426605294,
    "wind_direction_terrain_rad": 0.5708175112371989,
    "wind_speed_3d": 8.47489677130127,
    "wind_speed_background": 6.140294161922654,
    "wind_speed_terrain": 5.976341294594394,
    "wind_u": -2.0440788443077587,
    "wind_u_3dw": -4.488348187217834,
    "wind_u_terrain": -3.229137516218628,
    "wind_v": -5.790073753692165,
    "wind_v_3dw": -6.989862439328695,
    "wind_v_terrain": -5.0288493883614755,
    "wind_weight": 0.8760960059759071
  },
  "request": {
    "altitude_agl_m": 100.0,
    "altitude_msl_m": 1837.0499267578125,
    "aspect_deg": 252.9410858154297,
    "curvature_raw": -0.005776687653064782,
    "curvature_scaled": -0.005776687653064782,
    "ground_elevation_m": 1737.0499267578125,
    "latitude": 40.025,
    "longitude": -105.295615,
    "slope_deg": 20.172861099243164,
    "terrain_aspect_band": 3,
    "terrain_elevation_band": 1,
    "terrain_slope_band": 2,
    "terrain_source": "/media/p1/DigitalTwin/TDDT_navigation/datasets/CRP_map/code/boulder_multilayer_map_builder_v20260814_1214/outputs/region_v1_map/terrain_obstacles.tif",
    "time": "2026-08-24T17:00:00",
    "vertical_reference": "ground-level (AGL); ground=shared-DEM:/media/p1/DigitalTwin/TDDT_navigation/datasets/CRP_map/code/boulder_multilayer_map_builder_v20260814_1214/outputs/region_v1_map/terrain_obstacles.tif"
  },
  "source_count": 5
}

```

### گام ۶: ساخت گزارش

#### گزارش ۲بعدی
```bash
python3 scripts/generate_plotly_2d_report.py \
  --input climate_dataset/micromet_grid_24h.csv \
  --dem-dir /media/p1/DigitalTwin/TDDT_navigation/datasets/CRP_map/code/boulder_multilayer_map_builder_v20260814_1214/outputs/region_v1_map \
  --output-dir reports/parameters_3d \
  --elevation-ground-level 100
```

#### گزارش ۳بعدی

```bash
python3 scripts/generate_plotly_3d_report.py \
  --input climate_dataset/micromet_grid_24h.csv \
  --dem-dir /media/p1/DigitalTwin/TDDT_navigation/datasets/CRP_map/code/boulder_multilayer_map_builder_v20260814_1214/outputs/region_v1_map \
  --output-dir reports/parameters_3d \
  --elevation-ground-level 100
```
#### گزارش ۳بعدی باد
```bash
python3 scripts/generate_plotly_3dw_report.py \
  --input climate_dataset/openmeteo_24h.csv \
  --region region/region_v1.geojson \
  --fmu fmu/MicroMetTwin.fmu \
  --time "2026-08-24T12:30:00" \
  --levels-agl 10,80,180 \
  --grid-n 5 \
  --dem-dir /media/p1/DigitalTwin/TDDT_navigation/datasets/CRP_map/code/boulder_multilayer_map_builder_v20260814_1214/outputs/region_v1_map \
  --output reports/parameters_3d/micromet_3dw_wind_report.html
```

## ارزیابی MicroMet در برابر مرجع آنلاین

```bash
python3 scripts/evaluate_micromet.py \
  --input climate_dataset/openmeteo_24h.csv \
  --region region/region_v1.geojson \
  --dem-dir /media/p1/DigitalTwin/TDDT_navigation/datasets/CRP_map/code/boulder_multilayer_map_builder_v20260814_1214/outputs/region_v1_map \
  --fmu fmu/MicroMetTwin.fmu \
  --elevation-ground-level 10 \
  --points 9 \
  --time-step 1 \
  --output-dir ./reports/evaluation
```

---

## ۱۷. اجرای آزمایش‌ها

آثار آزمون نسخه در پوشه `tests/` قرار دارند. موارد مهم:

```text
tests/BUILD_TEST_REPORT_v1823.md
tests/fmi_exported_symbols_v1823.txt
tests/fmi_state_machine_output_v1823.txt
tests/estimate_fmi_standard_v1823.json
tests/grid_dataset_smoke_v1823.txt
```

آزمون رابط ctypes:

```bash
python3 tests/test_fmi_ctypes.py
```

آزمون ماشین حالت FMI:

```bash
python3 tests/test_fmi_state_machine_v1823.py
```

---

## ۱۸. محدودیت‌های فعلی

- باینری همراه بسته فقط برای Linux x86-64 ساخته شده است؛
- دوقلو جایگزین یک مدل دینامیکی کامل جوی مانند WRF یا CFD نیست؛
- باد عمودی، آشفتگی، gust، جدایش جریان و گردابه پشت کوه مستقیماً حل نمی‌شوند؛
- کیفیت خروجی تابع کیفیت و تراکم داده پیش‌بینی ورودی است؛
- در نمونه همراه، اگر DEM واقعی تأمین نشود، ارتفاع زمین proxy و پارامترهای توپوگرافی ساده‌شده هستند؛
- چگالی هوا در روابط شماره‌گذاری‌شده سند MicroMet این بسته تعریف نشده و خروجی رسمی اصلی نیست؛
- گزارش مکانی از IDW برای نمایش استفاده می‌کند؛ این درون‌یابی نمایشی با محاسبه فیزیکی داخل FMU یکسان نیست.

---

## ۱۹. توصیه برای استفاده عملیاتی با پهپاد

برای استفاده دقیق در ناوبری و شبیه‌سازی پرواز:

1. ارتفاع زمین هر سلول را از DEM واقعی بخوانید؛
2. `terrain_dzdx` و `terrain_dzdy` را از DEM محاسبه کنید؛
3. همسایه‌های لازم رابطه انحنا را برای هر سلول تأمین کنید؛
4. ارتفاع سنسور و ارتفاع پیش‌بینی باد را یکسان‌سازی کنید؛
5. داده سنسور پهپاد را فقط برای تصحیح بایاس به‌کار ببرید؛
6. وضعیت `formula_valid` و کدهای خطا را پیش از مصرف خروجی بررسی کنید؛
7. خروجی باد MicroMet را به‌عنوان باد افقی توپوگرافی‌اصلاح‌شده، نه حل سه‌بعدی کامل جریان، تفسیر کنید.

---

## ۲۰. مستندات تکمیلی

- فرمالیسم: `docs/micromet_formalism.md`
- پوشش روابط: `docs/FORMULA_COVERAGE.md`
- انطباق FMI: `docs/FMI_CONFORMANCE.md`
- فهرست متغیرها: `docs/FMI_VARIABLES.md`
- گزارش آزمون: `tests/BUILD_TEST_REPORT_v1823.md`

## گزارش ارتفاع دلخواه با ارزیابی مستقیم FMU

اسکریپت `scripts/generate_plotly_3d_report.py` دیگر برای ارتفاع درخواستی به وجود دو لایه عمودی در فایل `micromet_grid_24h.csv` وابسته نیست. فایل شبکه فقط مختصات افقی سلول‌ها و ارتفاع زمین را فراهم می‌کند؛ سپس برای هر سلول و هر زمان، مقدار ارتفاع درخواستی به MSL تبدیل و `MicroMetTwin.fmu` مستقیماً از طریق FMI فراخوانی می‌شود.

بنابراین درخواست زیر حتی وقتی مجموعه‌داده بسته فقط لایه `100 m AGL` داشته باشد، معتبر است:

```bash
python3 scripts/generate_plotly_3d_report.py \
  --input climate_dataset/micromet_grid_24h.csv \
  --fmu fmu/MicroMetTwin.fmu \
  --output-dir reports/parameters_3d \
  --elevation-ground-level 200
```

برای هر سلول رابطه زیر اعمال می‌شود:

```text
altitude_msl_m = ground_elevation_m + requested_agl_m
```

سپس همه پارامترهای اقلیمی در همان ارتفاع و زمان توسط FMU محاسبه می‌شوند. عبارت `vertical_sampling_mode=direct_fmu_evaluation` در داده داخلی گزارش، این مسیر محاسباتی را مشخص می‌کند. تنها زمانی یک سلول ماسک می‌شود که ارتفاع MSL درخواستی واقعاً پایین‌تر از ارتفاع زمین همان سلول باشد؛ نبود لایه عمودی در CSV دیگر به‌عنوان «زیر سطح زمین» تفسیر نمی‌شود.


### انتخاب خودکار فایل پیش‌بینی گزارش

هنگامی که گزینه `--forecast-input` نوشته نشود، اسکریپت گزارش به‌ترتیب زیر فایل forcing آفلاین را پیدا می‌کند:

1. فایل provenance کنار مجموعه‌داده با نام `micromet_grid_24h.csv.meta.json`؛
2. فایل `climate_dataset/openmeteo_24h.csv` که توسط دستور دانلود راهنما ساخته شده است؛
3. فایل نمونه `climate_dataset/example_offline_input_spatial.csv` فقط برای اجرای نمونه بسته.

اسکریپت `run_fmu_dataset.py` از این نسخه به بعد فایل provenance را خودکار تولید می‌کند. بنابراین فایل پیش‌بینی استفاده‌شده برای ساخت grid و گزارش یکسان باقی می‌ماند. گزینه صریح زیر همچنان برای انتخاب فایل دیگر معتبر است:

```bash
--forecast-input climate_dataset/openmeteo_24h.csv
```

این اصلاح مانع وابستگی ناخواسته گزارش به فایل نمونه می‌شود و توالی دستورات اصلی README بدون افزودن گزینه اضافی اجرا خواهد شد.

## گزارش سه‌بعدی اقلیم روی DEM

اسکریپت `scripts/generate_plotly_3d_report.py` سطح واقعی DEM را به‌عنوان بستر سه‌بعدی نمایش می‌دهد و میدان اقلیمی دوبعدی را در ارتفاع انتخابی روی آن قرار می‌دهد. برای ارتفاع AGL، ارتفاع هندسی هر نقطه از صفحه اقلیمی برابر است با:

```text
climate_altitude_msl = terrain_elevation_msl + requested_altitude_agl
```

فایل `terrain_obstacles.tif` در بسته نقشه چندلایه، ارتفاع زمین را در باند نخست نگه می‌دارد. مسیر پیش‌فرض DEM برابر `outputs/region_v1_map` است و با `--dem` یا `--dem-dir` قابل تغییر است.

```bash
python3 scripts/generate_plotly_3d_report.py \
  --input climate_dataset/micromet_grid_24h.csv \
  --dem-dir ../boulder_multilayer_map_builder_v20260814_0850/outputs/region_v1_map \
  --output-dir reports/parameters_3d \
  --elevation-ground-level 100
```

خروجی شامل سطح DEM، صفحه نیمه‌شفاف اقلیمی، انتخاب پارامتر و لغزنده ۲۴ ساعته است. در صورت وجود فایل پیش‌بینی و FMU، پارامترها مستقیماً در ارتفاع درخواست‌شده توسط FMI محاسبه می‌شوند.

### Toggle the DEM terrain layer

The 3D report now includes a **Show DEM terrain** checkbox in the control panel. Clear the checkbox to hide the terrain/background surface while retaining the climate sheet and evaluation-cell markers. Re-enable it to restore the DEM surface. The setting is preserved while changing the selected climate parameter or forecast time.


## محدوده استاندارد GeoJSON و WGS 84

از این نسخه، فایل مرجع و یگانه محدوده جغرافیایی بسته عبارت است از:

```text
region/region_v1.geojson
```

فایل قدیمی `config/region.json` دیگر توسط گردش‌کار اصلی استفاده نمی‌شود. مختصات GeoJSON طبق RFC 7946 با ترتیب `[longitude, latitude]` و بر پایه WGS 84 تفسیر می‌شوند. اسکریپت‌ها `FeatureCollection`، `Feature`، `Polygon` و `MultiPolygon` را می‌پذیرند و در `FeatureCollection` ابتدا عارضه‌ای با `id=region-boundary` یا `feature_role=region_boundary` را انتخاب می‌کنند.

دریافت داده پیش‌بینی ۲۴ ساعت آینده برای همین محدوده:

```bash
python3 scripts/download_openmeteo.py \
  --region region/region_v1.geojson \
  --output climate_dataset/openmeteo_24h.csv \
  --hours 24
```

اعتبارسنجی محدوده و مشاهده درخواست‌ها بدون دانلود:

```bash
python3 scripts/download_openmeteo.py \
  --region region/region_v1.geojson \
  --output climate_dataset/openmeteo_24h.csv \
  --hours 24 \
  --dry-run
```

ساخت شبکه اقلیمی دقیقاً درون چندضلعی GeoJSON:

```bash
python3 python/run_fmu_dataset.py \
  --input climate_dataset/openmeteo_24h.csv \
  --region region/region_v1.geojson \
  --resolution-m 1000 \
  --elevation-ground-level-levels 100,200,300 \
  --output climate_dataset/micromet_grid_24h.csv \
  --grid-output climate_dataset/grid_cells.csv
```

گزارش دوبعدی نیز به‌طور پیش‌فرض مرز همین GeoJSON را برای برش میدان اقلیمی استفاده می‌کند.

## Structured error for a request time outside the forecast range

`python/estimate_climate.py` does not emit a Python traceback when `--time` is outside the time interval stored in the offline forecast CSV. It prints a machine-readable JSON dictionary and exits with status code `2`.

Example error code:

```text
CLIMATE_TIME_OUTSIDE_FORECAST_RANGE
```

The error details include the requested time, forecast start and end times, input file, execution context, and affected forecast source points.

## ارزیابی MicroMet در برابر مرجع آنلاین

اسکریپت `scripts/evaluate_micromet.py` خروجی `estimate_climate.py` و FMU را در چند نقطه داخل `region/region_v1.geojson` محاسبه می‌کند و با پیش‌بینی نقطه‌ای آنلاین Open-Meteo در همان مختصات و زمان مقایسه می‌کند.

```bash
python3 scripts/evaluate_micromet.py \
  --input climate_dataset/openmeteo_24h.csv \
  --region region/region_v1.geojson \
  --fmu fmu/MicroMetTwin.fmu \
  --elevation-ground-level 10 \
  --points 9 \
  --time-step 1 \
  --output-dir ./reports/evaluation
```

خروجی‌ها:

- `evaluation/micromet_evaluation_records.csv`: تمام مقایسه‌های نقطه‌ای؛
- `evaluation/micromet_evaluation_summary.json`: MAE، RMSE، Bias و ضریب همبستگی؛
- `evaluation/micromet_evaluation_report.html`: گزارش تعاملی با نمودار مقدار MicroMet در برابر مرجع و خط ۱:۱.

پارامترهای مقایسه‌شده شامل دما، رطوبت نسبی، سرعت و جهت باد، فشار سطحی، بارش و تابش کوتاه‌موج است. برای جهت باد، خطا به‌صورت زاویه‌ای دایروی در بازه کوتاه‌ترین اختلاف محاسبه می‌شود.

> این ارزیابی، مقایسهٔ «پیش‌بینی با پیش‌بینی» است و اعتبارسنجی مستقل در برابر مشاهدهٔ ایستگاهی محسوب نمی‌شود؛ زیرا ورودی MicroMet نیز ممکن است از Open-Meteo تأمین شده باشد. برای اعتبارسنجی علمی مستقل، از داده‌های ایستگاهی، رادیوسوند یا حسگرهای میدانی استفاده شود. همچنین ارتفاع مرجع Open-Meteo برای دما و رطوبت ۲ متر و برای باد ۱۰ متر است؛ بنابراین `--elevation-ground-level 10` برای باد هم‌ارتفاع است، ولی مقایسه دما و رطوبت در این حالت دارای اختلاف ارتفاع ۸ متر است.

## کالیبراسیون دقت و خروجی‌های خام

در این نسخه یک لایه کالیبراسیون شفاف و قابل‌غیرفعال‌سازی پس از اجرای روابط رسمی MicroMet اضافه شده است. روابط شماره‌گذاری‌شده فرمالیسم حفظ شده‌اند و خروجی‌های خام نیز در کنار خروجی‌های کالیبره‌شده از طریق FMI ارائه می‌شوند. جزئیات در فایل `docs/ACCURACY_IMPROVEMENT.md` آمده است.

برای غیرفعال‌کردن کالیبراسیون و دریافت خروجی مستقیم فرمالیسم، ورودی زیر را صفر کنید:

```text
calibration_enabled = 0
```

## افزونه MicroMet-3DW

در این نسخه افزونه مستقل `MicroMet-3DW Extension` بدون تغییر روابط اصلی MicroMet اضافه شده است. اسکریپت `python/estimate_climate.py` اکنون بردار باد سه‌بعدی، گرادیان سرعت، مشتق زمانی، مشتق مادی و شاخص‌های سازگاری پیوستگی را برمی‌گرداند. اسکریپت `scripts/generate_plotly_3dw_report.py` برای یک زمان مشخص، میدان برداری باد را روی کل شبکه نمونه‌برداری‌شده منطقه GeoJSON/DEM و ترازهای AGL خواسته‌شده تولید می‌کند.

پیش‌بینی موجود در بسته فعلی تا 2026-08-07T11:00:00 است؛ زمان‌های بعد از آن بدون فایل پیش‌بینی جدید عمداً extrapolate نمی‌شوند.

### یادداشت نگه‌داری نسخه v20260807_1052
اسکریپت `generate_plotly_3dw_report.py` اکنون شبکه افقی گزارش را از تقاطع امن Region درخواستی و پوشش واقعی raster مربوط به DEM می‌سازد. در نتیجه بزرگ‌تر بودن Region از DEM یا وجود حاشیه‌های NoData باعث توقف کل گزارش 3DW نمی‌شود. در این اصلاح، فرمالیسم MicroMet، فرمالیسم MicroMet-3DW و فایل دودویی FMU تغییر نکرده‌اند.

## زمین مشترک TDDT — نسخه 20260807_1150

در این نسخه MicroMet همان `terrain_obstacles.tif` مورد استفاده در مسیر داده MESA-NAV و WingMPET را به‌عنوان حقیقت زمین استفاده می‌کند. خود FMU همچنان یک مؤلفه native/FMI مستقل از مسیر فایل باقی مانده است؛ ماژول `python/terrain_truth.py` فایل GeoTIFF را به‌صورت پنجره‌ای و با کنترل CRS نمونه‌برداری می‌کند و مقادیر زمین را به‌صورت scalar از طریق FMI به MicroMet می‌دهد.

نگاشت زمین:

- باند ۱: ارتفاع زمین MSL؛
- باند ۲: شیب؛
- باند ۳: جهت شیب؛
- باند ۱: مبنای محاسبه گرادیان DEM و curvature.

CLIهای اصلی TDDT اکنون به‌طور پیش‌فرض `require-dem` هستند. بنابراین نبود DEM دیگر به‌صورت خاموش به Barnes proxy و شیب/جهت/انحنای صفر منجر نمی‌شود. fallback قدیمی فقط با `--terrain-policy prefer-dem` به‌صورت صریح قابل استفاده است.

### قاعده پوشش Grid و Terrain در نسخه v20260807_1154

اسکریپت `python/run_fmu_dataset.py` اکنون دامنه افقی محاسبات را از تقاطع ناحیه GeoJSON استاندارد RFC 7946 و پوشش معتبر raster زمین مشترک می‌سازد. نقاط کاندید خارج از `terrain_obstacles.tif` یا واقع بر سلول‌های NoData پیش از هر فراخوان FMU حذف می‌شوند. تعداد این نقاط در provenance با فیلدهای `horizontal_candidate_cells`، `horizontal_valid_terrain_cells`، `skipped_outside_terrain` و `skipped_terrain_nodata` ثبت می‌شود. اگر تقاطع معتبر خالی باشد، تولید dataset با خطای صریح extent/CRS متوقف می‌شود و به terrain اقلیمی تخمینی fallback نمی‌کند.


## واحدها و schema معنایی FMI

تمام متغیرهای `Real` در `modelDescription.xml` اکنون واحد صریح FMI دارند؛ کمیت‌های بدون‌بُعد با `unit="1"` مشخص شده‌اند. فایل `docs/fmi_semantic_schema.json` نگاشت `value/unit/frame/timestamp/source/quality/uncertainty/validity` را تعریف می‌کند. خروجی عددی قدیمی بدون تغییر است و با گزینه `--with-metadata` در `estimate_climate.py` رکوردهای معنایی نیز افزوده می‌شوند. مقدار `uncertainty=null` یعنی عدم قطعیت هنوز کمی‌سازی نشده است، نه اینکه صفر باشد.

---

## به‌روزرسانی Dynamic N-Twin Layer 2 — 2026-08-12

برای هماهنگی با `TDDT_Dynamic_NTwin_Layer2_details_FA_v2.md`، دوقلوی اقلیمی به‌صورت یک Domain Twin مستقل دارای قرارداد علمی، metadata معرفتی، validity envelope، reliability profile و TEP اختصاصی شده است. خروجی Layer 2 شامل `state/U/Q/V/R/context/timestamp/provenance` است و با گزینه `--tddt-layer2` در `estimate_climate.py` قابل دریافت است.

این بسته عمداً هیچ منطق Layer 3 شامل `STD(t)`، topology، coupling، وزن شبکه یا `Center of Consistency` را داخل MicroMet قرار نمی‌دهد. TEP اولیه از نوع `TEP_sim` است و field validation محسوب نمی‌شود. به‌روزرسانی posterior فقط در مسیر کند، versioned و field-backed انجام می‌شود.

مستندات: `docs/TDDT_DYNAMIC_NTWIN_LAYER2_ALIGNMENT.md` و `docs/TDDT_LAYER2_V2_COMPLETENESS_AUDIT.md`.

## به‌روزرسانی TEP v2 شبکهٔ Dynamic N-Twin

اسکریپت `test-senarios/micromet_all_formulas_scenario.py` یک رابط مستقل مبتنی بر دیکشنری برای تنظیم و اجرای تمام مسیر پیاده‌سازی‌شدهٔ دوقلو فراهم می‌کند. سازندهٔ TEP نیز اکنون perturbation ensemble را مستقیماً روی FMU اجرا می‌کند، covariance کامل، bounds/centroid سلول‌ها، reliability منظم‌شده، held-out validation، provenance و VERSION را تولید می‌کند. این TEP همچنان `simulation prior` است و به‌معنی field validation یا operational reliability نیست.

## تکمیل انطباق Dynamic N-Twin Layer 2 / TEP — 2026-08-12
قرارداد علمی Climate Twin، metadata همراه state در packet، آزمون‌های واقعی TEP و مسیر posterior کنترل‌شده با field evidence مطابق اسناد Layer 2 و TEP v2 تکمیل شدند. رفتار عددی MicroMet/3DW تغییر نکرده است. جزئیات در `docs/TDDT_LAYER2_V2_COMPLETENESS_AUDIT.md` و `docs/TDDT_TEP_V2_CONFORMANCE_AUDIT.md` آمده است.


### Air density output / خروجی چگالی هوا

`MicroMetTwin.fmu` exports `air_density` (`kg/m3`, VR=246), derived from the final operational pressure, temperature and relative humidity. It is an operational thermodynamic extension and does not alter the original MicroMet equations (1-33).


## TDDT uncertainty extension 2026-08-21
The Layer-2 TEP now quantifies `wind_u_3dw`, `wind_v_3dw`, `diagnostic_vertical_wind`, `wind_speed_3d`, and `air_density` with per-cell std/variance/full covariance; runtime gates preserve conservative behavior when 3DW is invalid.

### ایمنی builder تولیدی TEP (v20260824_0944)

نام تاریخی `python/build_tddt_layer2_tep_v2.py` فقط به‌عنوان ورودی سازگاری deprecated حفظ شده است. این فایل دیگر builder شش‌خروجی قدیمی را در خود ندارد و به `build_tddt_layer2_tep_v3.py` هدایت می‌شود؛ همچنین `python/build_tddt_layer2_tep.py` نیز مستقیماً v3 را اجرا می‌کند. بنابراین هیچ entry point باقی‌مانده‌ای نمی‌تواند `resources/tddt_layer2/tep` را به schema قدیمی شش‌خروجی تنزل دهد.
