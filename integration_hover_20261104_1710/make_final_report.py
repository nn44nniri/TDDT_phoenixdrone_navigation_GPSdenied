"""Reproduce this transaction's report from preserved source and Native evidence."""
import difflib
import gzip
import hashlib
import html
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

A = Path(__file__).resolve().parent
R = A.parent
E = A / 'evidence'


def read(p):
    return json.load(gzip.open(p, 'rt') if str(p).endswith('.gz') else open(p))


def dump(p, data):
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def summarize(p):
    d = read(p)
    ds = d.get('future_layers') or {}
    l5 = (ds.get('5') or {}).get('output') or {}
    l6 = (ds.get('6') or {}).get('output') or {}
    l7 = (ds.get('7') or {}).get('output') or {}
    result = l5.get('result') or {}
    snapshot = (d.get('layer3_result') or {}).get('snapshot') or {}
    gate = l6.get('prediction_gate') or {}
    g = gate.get('evidence') or {}
    row = g.get('first_row') or {}
    physical = row.get('physical_replay') or {}
    pred = l5.get('current_first_move_prediction') or {}
    execution = l6.get('execution_evidence') or {}
    target = pred.get('source_plant_time_s')
    if target is not None:
        target += float((pred.get('timing_contract') or {}).get('execution_segment_s', 0.))
    post = execution.get('plant_time_s')
    return {
        'transaction_id': d['transaction_id'], 'cycle_file': str(p.relative_to(A)),
        'pre_release': 'pre-release' in p.name, 'status': d.get('status'),
        'phase': (snapshot.get('context') or {}).get('flight_phase'),
        'reference_time_s': d.get('reference_time_s'),
        'L5_status': l5.get('status'), 'L5_FINAL': (result.get('gates') or {}).get('FINAL'),
        'u_cmd': (result.get('layer6_package') or {}).get('u_cmd'),
        'L5_reason': l5.get('reason'), 'L5_error': l5.get('error'),
        'L6_status': l6.get('status'), 'L6_reason': l6.get('reason'), 'L6_error': l6.get('error'),
        'submission_attempted': l6.get('submission_attempted'),
        'actuation_executed': l6.get('actuation_executed'),
        'native_session_id': (execution.get('provenance') or {}).get('native_session_id'),
        'execution_duration_bounds_s': execution.get('native_execution_duration_bounds_s'),
        'execution_start_bounds_s': execution.get('execution_start_plant_time_bounds_s'),
        'execution_post_plant_time_s': post, 'prediction_target_plant_time_s': target,
        'prediction_execution_offset_s': post-target if post is not None and target is not None else None,
        'L7_status': (ds.get('7') or {}).get('status'), 'L7_reason': l7.get('reason'),
        'L7_validation_reasons': ((l7.get('result') or {}).get('validation') or {}).get('reasons'),
        'L7_native_preparation': l7.get('native_diagnostic_preparation'),
        'feedback_to_next_L2': (d.get('provenance') or {}).get('online_previous_layer7_feedback'),
        'timing_s': (d.get('provenance') or {}).get('component_timing_s'),
        'L2_timing_s': (d.get('provenance') or {}).get('layer2_wall_timings_s'),
        'current_AGL_m': (snapshot.get('context') or {}).get('altitude_agl_m'),
        'predicted_AGL_m': ((row.get('snapshot') or {}).get('context') or {}).get('altitude_agl_m'),
        'prediction_gate': {k: gate.get(k) for k in ('ok', 'status', 'reason', 'error')},
        'feasibility': g.get('feasibility'), 'physical_verdict': g.get('physical_verdict'),
        'physical_initialization': physical.get('initialization'),
        'ground_support_evidence': physical.get('ground_support_evidence'),
        'prediction': {k: pred.get(k) for k in ('candidate_id', 'transaction_id', 'u_cmd',
            'source_snapshot_hash', 'source_reference_time_s', 'source_plant_time_s', 'elapsed_s', 'source')},
    }


roots = ['TDOC_v20260911_2052/' + n for n in ('src', 'scripts', 'configs', 'tests')]
roots += ['layer6_v20261007_1032/src', 'layer4_v20260907_0608/src', 'layer4_v20260907_0608/configs']
names = {'SETUP.md'}
for directory in roots:
    for base in (R, A / 'before'):
        names.update(str(p.relative_to(base)) for p in (base / directory).rglob('*')
                     if p.is_file() and '__pycache__' not in p.parts and p.suffix in ('.py', '.sh', '.yaml'))
changes = []
patch = []
for rel in sorted(names):
    before, after = A / 'before' / rel, R / rel
    old = before.read_bytes() if before.exists() else b''
    new = after.read_bytes() if after.exists() else b''
    if old == new:
        continue
    changes.append({'path': rel, 'before_sha256': sha(old) if before.exists() else None,
                    'after_sha256': sha(new) if after.exists() else None, 'bytes': len(new)})
    patch.append('diff --git a/' + rel + ' b/' + rel + '\n')
    patch.extend(difflib.unified_diff(old.decode().splitlines(True), new.decode().splitlines(True),
                  fromfile='a/' + rel if before.exists() else '/dev/null',
                  tofile='b/' + rel if after.exists() else '/dev/null'))
(A / 'source_changes.patch').write_text(''.join(patch))
dump(A / 'source_changes.json', changes)

attempts = []
for name in ('serialization_attempt', 'registry_attempt', 'projection_attempt',
             'proof_timeout_attempt', 'ground_corridor_attempt', 'detailed_corridor_attempt', 'final_native'):
    directory = A / name
    if not directory.exists():
        continue
    cycles = [summarize(p) for p in sorted((directory / 'runtime/cycles').glob('workflow*.json.gz'))]
    attempts.append({'name': name, 'cycles': cycles,
                     'executed_cycles': sum(c['L6_status'] == 'EXECUTED' for c in cycles),
                     'L7_OK_cycles': sum(c['L7_status'] == 'OK' for c in cycles)})

shots = []
for p in sorted(E.glob('*/manifest.json')):
    d = read(p)
    s = d.get('GUI_observation_before_capture') or {}
    shots.append({'session_id': d['native_session_id'], 'folder': str(p.parent.relative_to(A)),
        'all_six_captured': d.get('all_six_captured'), 'health_ok': (d.get('health') or {}).get('ok'),
        'mavlink_connected': s.get('mavlink_connected'), 'mode': s.get('mode'), 'armed': s.get('armed'),
        'valid_visual_capture': bool(d.get('all_six_captured') and (d.get('health') or {}).get('ok')
            and s.get('mavlink_connected') and s.get('mode') and s.get('armed') is not None)})

last_observations = {}
for line in (E / 'native_observations.jsonl').open():
    row = json.loads(line)
    if row.get('health'):
        last_observations[row['health']['session_id']] = row
dump(E / 'last_native_observations.json', last_observations)
for sid in last_observations:
    source = R / 'layer6_v20261007_1032/runtime/sessions' / sid
    target = A / 'native_sessions' / sid
    if source.exists() and not target.exists():
        shutil.copytree(source, target)

steps = [
    [0, 'ثبت سورس، prior، forcing و binary', 'PASS', 'hash دقیق سورس/گواهی و حفظ KStore؛ reset اجرا نشد.'],
    [1, 'علت‌دارکردن ingress و دریافت دوبارهٔ نمونهٔ منقضی', 'PASS', 'کد زیرگیت، witness وابسته به thread و retry محدود؛ تغییر هویت/hash فوراً رد می‌شود.'],
    [2, '۶۰ ثانیه مشاهدهٔ واقعی روی زمین', 'PASS', '۱۰۲ مشاهده، ۶۰٫۱۳۸ ثانیه plant، بدون ARM/فرمان، صفر خطای acquisition.'],
    [3, 'واحد/فریم و مقداردهی اولیهٔ bias موجود MESA', 'PARTIAL', 'ورودی bias با IMU سکون مقداردهی شد؛ صحت ناوبری دینامیکی اثبات نشده است.'],
    [4, 'اندازه‌گیری و کاهش هزینهٔ مسیر', 'PARTIAL', 'YAML بر اساس محتوای فعلی cache می‌شود و history از کپی‌های forecast حذف شد؛ cycle هنوز چندثانیه‌ای است.'],
    [5, 'گرم‌کردن اجزا پیش از release', 'PARTIAL', 'آماده‌سازی Layer5 و کالیبراسیون پیش از فرمان انجام می‌شود؛ timing لازم برای handover پیوسته حاصل نشد.'],
    [6, 'state جاری MPC و prediction کوتاه فرمان واقعی', 'PARTIAL', 'MPC context جاری می‌گیرد؛ replay فرمان دقیق و یک proof اجرای Native ساخته شدند؛ هم‌زمانی جفت هنوز رد می‌شود.'],
    [7, 'قرارداد زمان prediction و execution', 'PARTIAL', 'source clock حفظ و تأخیر مصنوعاً پنهان نمی‌شود؛ یک residual هم‌زمان Native هنوز پذیرفته نشده است.'],
    [8, 'تحویل پیوستهٔ کنترل بین cycleها', 'BLOCKED', 'سقف segment برابر ۰٫۱ ثانیه است؛ تداوم پوشش بین cycleهای چندثانیه‌ای پیاده و تأیید نشده است.'],
    [9, 'هویت candidate/command/snapshot و checksum', 'PASS', 'اصل artifact دست‌نخورده و ورودی موقت MPC جداگانه اعتبارسنجی می‌شود؛ فرمان متفاوت prediction را رد می‌کند.'],
    [10, 'دو cycle واقعی L7→L2', 'BLOCKED', 'هیچ دو cycle اجرایی همراه residual و مصرف آن در L2 ثبت نشده است.'],
    [11, 'پرواز محلی ۱، ۵ و ۱۰ ثانیه', 'BLOCKED', 'پروفایل و معیار پذیرش نصب شد؛ حتی سطح ۱ ثانیه تأیید نشده؛ ۵/۱۰ اجرا نشدند.'],
    [12, 'پرواز ۳۰ و ۶۰ ثانیه و فرود', 'BLOCKED', 'به علت رد سطح مقدماتی اجرا نشد؛ touchdown کنترل‌شده تأیید نشده است.'],
]
final = next((a for a in attempts if a['name'] == 'final_native'), {'cycles': []})
gate_cycles = [c for a in attempts if a['name']=='detailed_corridor_attempt' for c in a['cycles'] if c.get('feasibility')]
native_gate = gate_cycles[0] if gate_cycles else None
executed = next((c for c in final['cycles'] if c['L6_status']=='EXECUTED'), None)
result = {
    'generated_at': datetime.now().astimezone().isoformat(), 'mission_status': 'FAIL',
    'all_requested_microsteps_completed': False, 'closed_loop_2_to_7': 'BLOCKED',
    'accepted_local_hover_seconds': 0, 'hover_5_10_30_60_attempted': False,
    'latest_model_id': read(E / 'final_source_before_full_training_completed.json')['model_dependencies']['model_id'],
    'offline_prior_scope': 'Fresh three-cycle diagnostic Offline runs for each model identity; NOT a completed Offline mission',
    'offline_process_exit_code': 2, 'final_online_process_exit_code': 2,
    'compiled_source_modified': False, 'reset_tested': False,
    'tests': (E / 'tests_release_final.log').read_text().strip().splitlines()[-1],
    'source_change_count': len(changes), 'steps': steps, 'attempts': attempts,
    'captures': shots, 'previous_same_source_physical_rejection': native_gate,
    'latest_executed_cycle': executed,
    'native_acceptance': {
        'ARM_from_current_MPC': bool(executed),
        'bounded_L6_execution': bool(executed),
        'same_session_six_panels_captured': any(s['valid_visual_capture'] and s['armed']
            and s['session_id']==(executed or {}).get('native_session_id') for s in shots),
        'L7_residual_accepted': False, 'next_L2_feedback_consumption': False,
        'continuous_hover_accepted': False, 'touchdown_accepted': False,
    },
    'preservation': read(E / 'preservation_final.json'),
    'limits': ['No accepted continuous flight, dynamic navigation or touchdown proof',
        'FCU EKF reports GPS use; no claim of GNSS-denied FCU operation',
        'Physical replay may initialize from execution-only FCU state; this is not MESA re-anchoring',
        'No artificial forecast retiming, TTL/corridor relaxation or independent ARM test',
        'The short replay, proof RPC timing change and local profile do not establish continuous execution authority'],
}
# Exit status is captured by the caller rather than inferred from report contents.
if (E / 'final_online_exit.json').exists():
    result['final_online_process_exit_code'] = read(E / 'final_online_exit.json')['exit_code']
dump(E / 'FINAL_RESULT.json', result)


def esc(value):
    return html.escape(str(value))


def pre(value):
    return '<pre dir="ltr">' + esc(json.dumps(value, indent=2, ensure_ascii=False, default=str)) + '</pre>'


def link(path, label):
    return '<a href="' + esc(path) + '">' + esc(label) + '</a>'


parts = ['''<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8">
<title>نتیجهٔ اصلاح میکروگام‌های TDDT و آزمون Native</title><style>
body{font:16px/1.9 sans-serif;background:#f3f5f8;color:#172337;margin:0}main{max-width:1180px;margin:auto;padding:30px;background:white}
h1,h2{color:#173f6c}table{border-collapse:collapse;width:100%;margin:20px 0}td,th{border:1px solid #c7d2df;padding:9px;vertical-align:top}th{background:#e8eff7}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#edf1f6;padding:15px;text-align:left;font-size:13px;line-height:1.6}
.fail{background:#ffe7e7;padding:18px;border-right:6px solid #b52235}.note{background:#fff3cd;padding:15px}.gallery{display:grid;grid-template-columns:1fr 1fr;gap:12px}.gallery img{width:100%;border:1px solid #c7d2df}a{color:#075db0}code{direction:ltr;unicode-bidi:embed}small{color:#53657a}</style><main>
<h1>اصلاح رابط‌ها و نتیجهٔ واقعی آزمون محلی Native</h1>''', '<p>' + esc(result['generated_at']) + '</p>',
'''<p class="fail"><strong>خواستهٔ کامل محقق نشد: مأموریت FAIL و حلقهٔ اجرایی پیوستهٔ ۲ تا ۷ BLOCKED است.</strong>
اصلاحات واقعی در سورس ثبت و آزموده شدند؛ اما حتی hover یک‌ثانیه‌ای همراه بازخورد معتبر و فرود تأیید نشده است.
بنابراین برای مراحل ۵، ۱۰، ۳۰ یا ۶۰ ثانیه PASS صادر نمی‌شود.</p>
<p>در اجرای نهایی، نشست <code>1dc72d00</code>، فرمان همین MPC از مسیر GUI/ArduPilot به QHOVER/ARM و یک segment معتبر
<code>EXECUTED</code> با کران زمانی ۳۹ تا ۸۱ میلی‌ثانیه رسید. این ARM مستقل یا replay بستهٔ قدیمی نبود.
Layer7 فراخوانی شد، ولی prediction برای ۵۸٫۳۰۸ و observation برای ۶۵٫۱۸۷ ثانیه بود؛ اختلاف ۶٫۸۷۹ ثانیه باعث رد جفت‌سازی شد.
در تکرار قبلی همین سورس، گیت فیزیکی ground corridor رد کرده بود. هیچ‌یک شاهد hover پذیرفته‌شده نیست.</p>
<p>proof لایه۶ فقط مدت و هویت segment ورودی را اثبات می‌کند؛ تثبیت فیزیکی پهپاد یا ایمنی فاصلهٔ بعد از cancel را اثبات نمی‌کند.
در تصویر Console نهایی، attitude و سرعت پس از segment تغییر کرده‌اند و شاهد hover پایدار نیستند.</p>
<p>این گزارش از log، CycleResult، نمونه‌های IMU و تصویرهای همین transaction ساخته شده است. گزارش‌های گذشته به‌عنوان evidence پذیرش این اجرا استفاده نشده‌اند.</p>
<h2>وضعیت تک‌تک میکروگام‌ها</h2><table><tr><th>گام</th><th>کار</th><th>وضعیت</th><th>شاهد و محدودیت</th></tr>''']
parts += ['<tr>' + ''.join('<td>' + esc(x) + '</td>' for x in step) + '</tr>' for step in steps]
parts += ['</table><h2>تغییرات ثبت‌شده در سورس اصلی</h2><ol>',
'''<li>ingress لایه۶ علت دقیق رد clock، environment، binding و freshness را حفظ می‌کند. witness هر thread مستقل است؛ نمونهٔ منقضی دوباره دریافت می‌شود، ولی تغییر hash/هویت fail-closed باقی می‌ماند.</li>
<li>یک کالیبراسیون پنج‌ثانیه‌ای با IMU واقعی سکون در شروع نشست، ورودی‌های موجود bias در MESA را مقداردهی می‌کند. receipt در حافظه معتبر است؛ JSON ذخیره‌شده مجوز نصب bias نمی‌دهد. هیچ نمونهٔ خامی تغییر نکرده است.</li>
<li>receipt داخلی پیش از عبور از مرز JSON لایه۳ به evidence سریال‌پذیر تبدیل شد. کپی history خام از packetهای محاسباتی لایه۳ و contextهای فرضی حذف شده؛ اصل evidence در CycleResult می‌ماند.</li>
<li>Layer5 برای اجرای MPC، context وضعیت فعلی لایه۳ را دریافت می‌کند. artifact اولیه، hash و زمان گواهی قبلی تغییر نمی‌کنند. ورودی موقت MPC checksum جداگانه دارد؛ indexهای صرفاً candidate_id به candidate کامل تبدیل نمی‌شوند.</li>
<li>برای همان candidate، transaction و u_cmd نهایی، replay کوتاه WingMPET/Layer3 ایجاد و فیزیکی بررسی می‌شود. این replay به‌تنهایی مجوز اجرا نیست. گیت ردشده به Layer6 نسبت داده می‌شود و FINAL واقعی Layer5 به خطای MPC بازبرچسب‌گذاری نمی‌شود. جزئیات AGL، پشتیبانی زمین و verdict نگه داشته می‌شوند.</li>
<li>Layer7 فقط prediction همان فرمان را جفت می‌کند. زمان هدف از source clock واقعی prediction می‌آید و به زمان ارسال دیرتر منتقل نمی‌شود. آزمون تأخیر یک‌ثانیه‌ای باید رد شود.</li>
<li>cache پارس YAML بر اساس bytes فعلی است؛ هر بار محتوا خوانده و hash می‌شود. تغییر هم‌اندازه با mtime مشابه نیز دیده می‌شود. در RPC وضعیت و proof، زمان هر درخواست با بودجهٔ کلی محدود شده تا پاسخ GUI بی‌جهت با request_id جدید رها نشود؛ مهلت کلی و مدت مجاز فرمان افزایش نیافتند.</li>
<li>پروفایل اختیاری takeoff→hover→landing در CLI و workflow اضافه شد. شمارش hover نیازمند اجرای واقعی، Layer7 معتبر و پوشش پیوسته است. مأموریت عادی یازده‌مرحله‌ای حفظ شده است. راه استفاده در SETUP.md آمده است.</li></ol>''',
    '<p>' + link('source_changes.patch', 'patch تغییرات همین transaction') + ' — ' +
    link('source_changes.json', 'فهرست فایل‌ها و hash قبل/بعد') + ' — ' + link('../SETUP.md', 'SETUP.md') + '</p>',
    '<h2>آزمون سکون و bias؛ با تفکیک از پرواز</h2>',
    '''<p>۱۰۲ مشاهدهٔ معتبر در ۶۰٫۱۳۸ ثانیهٔ plant، بدون درخواست ARM یا actuation ثبت شد. ۳۰۵۰ نمونهٔ retained IMU محفوظ است.
    gyro_y سکون برابر ‎−۰٫۰۰۲ rad/s بود. در replay نگه‌داشته‌شدهٔ حدود ۵۶ ثانیه با FMU اصلی و بدون FCU pose،
    خطای افقی حالت بدون bias حدود ۵۶۹ متر و با bias اندازه‌گیری‌شده ۵٫۶۶ متر و خطای عمودی ۰٫۹۰ متر شد.
    این کاهش، شاهد بهبود initialization است؛ ۵٫۶۶ متر از معیار افقی پنج‌متری بیشتر است و شاهد hover یا ناوبری دینامیکی نیست.</p>''',
    '<p>' + link('evidence/ground_result.json', 'نتیجهٔ acquisition زمین') + ' — ' +
    link('evidence/ground_bias_replay.json', 'مقایسهٔ سه replay') + ' — ' +
    link('evidence/ground_imu.json.gz', 'نمونه‌های واقعی IMU') + '</p>',
    '<h2>نتیجهٔ اجرای واقعی نسخه‌ها</h2><table><tr><th>اجرا</th><th>cycleهای ثبت‌شده</th><th>EXECUTED</th><th>L7 OK</th><th>علت‌های ثبت‌شده</th></tr>']
for attempt in attempts:
    errors = sorted({str(c[k]) for c in attempt['cycles'] for k in ('L5_error', 'L6_error', 'L6_reason') if c.get(k)})
    log_path = ('evidence/online_local_1s.log' if attempt['name']=='final_native'
                else attempt['name']+'/online_local_1s.log')
    name_html = link(log_path, attempt['name']) if (A/log_path).exists() else esc(attempt['name'])
    parts.append('<tr><td>' + name_html + '</td><td>' + str(len(attempt['cycles'])) +
                 '</td><td>' + str(attempt['executed_cycles']) + '</td><td>' + str(attempt['L7_OK_cycles']) +
                 '</td><td dir="ltr">' + '<br>'.join(esc(x) for x in errors) + '</td></tr>')
parts += ['''</table><p>prior هر مدل از یک اجرای تشخیصی Offline سه‌cycle‌ای همان نسخه ساخته شد. این اجراها با exit=2 و مأموریت ناقص پایان یافتند؛
    هویت گواهی/SETD پذیرفته شد، ولی پرواز کامل Offline و پوشش تمام فازها اثبات نشد. هیچ گواهی قدیمی با hash جدید بازبرچسب‌گذاری نشد.</p>''',
    '<h2>مانع زمانی در آخرین اجرای Native</h2>']
if executed:
    parts += ['''<p>بستهٔ FINAL همان MPC اجرا و proof آن پذیرفته شد. source زمان prediction برابر ۵۸٫۲۰۸ ثانیه است؛
        پیش‌بینی صد میلی‌ثانیه جلوتر به ۵۸٫۳۰۸ مربوط است. شروع واقعی ارسال در بازهٔ ۶۵٫۰۸۷ تا ۶۵٫۱۰۸ و observation پس از اجرا
        در ۶۵٫۱۸۷ ثبت شد. جابه‌جایی timestamp prediction به زمان ارسال، این فاصله را حل نمی‌کند.
        Layer7 به‌درستی جفت زمانی را رد کرد؛ «missing output/state pair» در این اجرا پیامد رد هم‌زمانی است و به معنی نبودن خود replay نیست.
        بدون residual معتبر، feedback به L2 و ادامهٔ مأموریت پذیرفته نمی‌شود.</p>''',
        pre({k:executed[k] for k in ('native_session_id','u_cmd','prediction','prediction_target_plant_time_s',
            'execution_start_bounds_s','execution_duration_bounds_s','execution_post_plant_time_s',
            'prediction_execution_offset_s','L7_validation_reasons','timing_s')})]
parts += ['<h2>مانع فیزیکی مشاهده‌شده در تکرار قبلی همین نسخه</h2>']
if native_gate:
    parts += ['<p>در ' + esc(native_gate['transaction_id']) + '''، Layer5 به FINAL رسید؛ گیت بعدی با علت
        <code>takeoff_ground_corridor</code> رد شد. AGL جاری برابر ''' + esc(native_gate['current_AGL_m']) +
        ' و AGL replay برابر ' + esc(native_gate['predicted_AGL_m']) +
        ''' متر بود؛ حد پایین قرارداد صفر است. گیت پشتیبانی زمین به علت AGL منفی false شد، replay از op27 استفاده کرد و تمام substepهای آن feasible نبودند.
        فرمان ارسال نشد. این داده‌ها اثبات نمی‌کنند مدل باد یا طراحی پهپاد معیوب است؛ فقط علت دقیق رد این ورودی و این قرارداد را نشان می‌دهند.</p>''',
        pre({k: native_gate[k] for k in ('current_AGL_m', 'predicted_AGL_m', 'u_cmd', 'feasibility', 'physical_verdict', 'ground_support_evidence', 'timing_s')})]
parts += ['''<h2>کار لازم برای عبور واقعی؛ مواردی که هنوز انجام نشده‌اند</h2><ol>
<li><strong>مرز تماس زمین و AGL:</strong> باید با قرارداد مصوب مشخص شود وقتی IMU/MESA و DEM، AGL منفی می‌دهند ولی FCU زمین را گزارش می‌کند، آغاز takeoff چگونه از نظر فیزیکی معتبر می‌شود. صرف clamp کردن AGL، جابه‌جایی مبدأ، کپی pose از FCU به MESA یا حذف گیت راه‌حل پذیرفته‌شده نیست. replay کوتاه، مقدار اولیهٔ فیزیکی execution-only را نیز مصرف می‌کند؛ آن state به MESA تزریق نمی‌شود و نباید «تمام initialization از MESA» نامیده شود.</li>
<li><strong>تداوم زمان و فرمان:</strong> segment صد میلی‌ثانیه‌ای با cycle چندثانیه‌ای پوشش پیوسته نمی‌سازد. تحویل فرمان با زمان اعمال تعریف‌شده، پیش‌بینی از anchor معتبر تا همان زمان، و evidence پوشش فاصلهٔ بین تصمیم‌ها لازم است. این زمان‌بندی در این اصلاح پیاده و در Native تأیید نشده است؛ افزایش TTL یا انتقال timestamp پیش‌بینی انجام نشد.</li>
<li><strong>تکرارپذیری proof:</strong> مشکل رهاکردن زودهنگام RPC اصلاح شد و در آخرین نشست، proof معتبر دریافت شد. این یک segment پذیرفته‌شده است؛ اثبات چند cycle پیوسته هنوز لازم است. خطاهای آینده state/failure و شمار timeout را حفظ می‌کنند.</li>
<li><strong>انطباق معنا و پاسخ QHOVER:</strong> پذیرش packet و تغییر stick معادل شناسایی پاسخ فیزیکی نیست. هم‌ارزی نگاشت فرمان مدل WingMPET با lean/climb semantics خلبان QHOVER باید با پارامترهای همان نشست و پاسخ واقعی تأیید شود. اصلاح تصادفی gain یا throttle و ادعای موفقیت از ARM انجام نشده است.</li>
<li><strong>بستن چرخهٔ بازخورد:</strong> پس از عبور موارد بالا، باید دو proof اجرایی هم‌زمان با prediction همان فرمان و مصرف residual در L2 ثبت شود. سپس به‌ترتیب ۱، ۵، ۱۰، ۳۰ و ۶۰ ثانیه و فرود آزموده شود. مراحل وابسته هنوز اجرا/پذیرش نشده‌اند.</li></ol>
<p class="note">این موارد، کار باقی‌مانده‌اند؛ گزارش به معنی رفع تمام ناسازگاری‌ها یا تکمیل مأموریت نیست. کنترل، باد، مدل دینامیکی و estimator بازطراحی نشده‌اند.</p>
<h2>آزمون‌ها، prior و build</h2>''',
    '<p>' + esc(result['tests']) + ' در آخرین مجموعهٔ آزمون مرزی مرتبط. تعداد مجموعه‌های تکراری با یکدیگر جمع نشده‌اند.</p>',
    '<p>' + link('evidence/tests_release_final.log', 'خروجی آزمون نهایی') + ' — ' +
    link('evidence/tests_prediction_clock.log', 'آزمون رد prediction با تأخیر') + ' — ' +
    link('evidence/final_prior_before_online.json', 'prior پیش از Online') + '</p>',
    '''<p>هیچ سورس کامپایل‌شونده یا binary تغییر نکرد؛ hash هر سه FMU، ArduPilot و JSBSim ثابت ماند.
    rebuild جدید انجام نشد. reset واقعی نیز آزموده نشد و برای پایداری پس از reset، PASS صادر نمی‌شود.
    مدل فعلی <code>''' + esc(result['latest_model_id']) + '</code> است. دانش و forcing در طول Online byte-exact حفظ شدند.</p>',
    pre(result['preservation']),
    '''<h2>تصاویر واقعی و اتصال شش پنل</h2><p>تصویرهای زیر از پنجره‌های واقعی همان نشست و با manifest هویت گرفته شده‌اند.
    captureهای زودهنگام با mode نامشخص برای پذیرش تصویری استفاده نشده‌اند. تصویرهای ARM نشست 57aa مربوط به همان نسخه‌اند؛ به نسخهٔ نهایی تعمیم داده نمی‌شوند.
    FCU در log استفاده از GPS را گزارش کرده است؛ ادعای GNSS-denied برای خود EKF آردوپایلوت مطرح نیست.</p>''']
for shot in shots:
    if not shot['valid_visual_capture']:
        continue
    folder = shot['folder']
    parts += ['<h3>' + esc(shot['session_id']) + ' — ' + esc(shot['mode']) + ' / ' +
              ('ARMED' if shot['armed'] else 'DISARMED') + '</h3>',
              '<p>' + link(folder + '/manifest.json', 'manifest پنجره‌ها و telemetry') + '</p><div class="gallery">']
    for role in ('gui', 'map', 'ardupilot', 'jsbsim', 'environment', 'console'):
        parts.append('<figure><a href="' + folder + '/' + role + '.png"><img loading="lazy" src="' + folder +
                     '/' + role + '.png"></a><figcaption>' + role + '</figcaption></figure>')
    parts.append('</div>')
parts += ['<h2>بستهٔ قابل بازبینی</h2><p>' + link('evidence/FINAL_RESULT.json', 'نتیجهٔ ماشین‌خوان و جزئیات cycleها') + ' — ' +
          link('evidence/online_local_1s.log', 'log آخرین Online') + ' — ' +
          link('evidence/last_native_observations.json', 'آخرین سلامت پنل‌های هر نشست') + ' — ' +
          link('evidence/visual_inspection.json', 'ثبت بررسی تصاویر واقعی نشست نهایی') + '</p>',
          '<p>CycleResult و گزارش‌های نهایی در <code>final_native/</code> و log/MAVLink خام نشست‌ها در <code>native_sessions/</code> نگه داشته شده‌اند.</p></main></html>']
(A / 'FINAL_NATIVE_REPORT_FA.html').write_text('\n'.join(parts))
print(json.dumps({k: result[k] for k in ('mission_status', 'closed_loop_2_to_7', 'source_change_count', 'tests')}, ensure_ascii=False))
