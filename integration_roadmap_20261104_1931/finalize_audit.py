"""Generate reviewable Persian results from this run's actual evidence only."""
from datetime import datetime
from zoneinfo import ZoneInfo
import difflib,gzip,hashlib,html,json,shutil
from pathlib import Path

HERE=Path(__file__).resolve().parent;ROOT=HERE.parent;TDOC=ROOT/'TDOC_v20260911_2052';E=HERE/'evidence'
DATA=HERE/'final_native'
def read(p):
    p=Path(p);return json.loads(gzip.decompress(p.read_bytes()) if p.suffix=='.gz' else p.read_bytes())
def safe(p,default=None):
    try:return read(p)
    except (OSError,ValueError):return default
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def esc(x):return html.escape(str(x))
def dump(x):return '<pre dir="ltr">'+esc(json.dumps(x,ensure_ascii=False,indent=2))+'</pre>'

changes=[];patch=[]
pairs=[]
for name in ('src','scripts','configs'):
    oldroot=HERE/'before'/name;newroot=TDOC/name
    for old in oldroot.rglob('*'):
        if old.is_file() and old.suffix in ('.py','.yaml','.sh'):
            pairs.append((old,newroot/old.relative_to(oldroot)))
pairs.append((HERE/'before/external/multifidelity.py',ROOT/'layer4_v20260907_0608/src/layer4/multifidelity.py'))
pairs.append((HERE/'before/external/configuration_boulder.yaml',ROOT/'layer4_v20260907_0608/configs/configuration_boulder.yaml'))
for name,relative in [('segments.py','src/layer6/segments.py'),('phoenix_f310_gui.py','src/gui/phoenix_f310_gui.py')]:
    pairs.append((HERE/'before/external/layer6'/name,ROOT/'layer6_v20261007_1032'/relative))
for old,new in pairs:
    if new.is_file() and old.read_bytes()!=new.read_bytes():
        changes.append({'file':str(new.relative_to(ROOT)),'before_sha256':sha(old),'after_sha256':sha(new)})
        patch.extend(difflib.unified_diff(old.read_text().splitlines(True),new.read_text().splitlines(True),
            fromfile=str(old.relative_to(ROOT)),tofile=str(new.relative_to(ROOT))))
HERE.joinpath('source_changes.patch').write_text(''.join(patch));HERE.joinpath('source_changes.json').write_text(json.dumps(changes,indent=2))
summary=safe(DATA/'reports/online/ONLINE_JSBSIM_SUMMARY.json',{})
episodes=summary.get('episodes') or [];records=summary.get('records') or []
cycles=[];bootstrap=[]
for p in sorted((DATA/'runtime/cycles').glob('workflow-online*.json.gz')):
    x=read(p);ds=x.get('future_layers') or {};l5=((ds.get('5') or {}).get('output') or {}).get('result') or {}
    l6=(ds.get('6') or {}).get('output') or {};l4=x.get('layer4_result') or {};mesa=(x.get('layer2_packets') or {}).get('MESA_NAV') or {}
    row={'transaction_id':x['transaction_id'],'L5_FINAL':(l5.get('gates') or {}).get('FINAL'),
        'L4_verified_count':len(l4.get('verified_candidates') or []),'L6_status':l6.get('status') or (ds.get('6') or {}).get('status'),
        'actuation_executed':l6.get('actuation_executed') is True,'L7_status':(ds.get('7') or {}).get('status'),
        'L6_error':l6.get('error') or l6.get('reason'),
        'L6_execution_evidence':l6.get('execution_evidence'),
        'L7_output':(ds.get('7') or {}).get('output'),
        'L7_feedback_routing':(ds.get('7') or {}).get('feedback_routing'),
        'L2_runtime_meta':x.get('provenance',{}).get('layer2_runtime_meta'),
        'previous_L7_feedback':x.get('provenance',{}).get('online_previous_layer7_feedback'),
        'L5_package':l5.get('layer6_package'),
        'timings_s':x['provenance'].get('component_timing_s'),
        'L2_timings_s':x['provenance'].get('layer2_wall_timings_s'),
        'first_stop':x['provenance'].get('decision_gate_trace'),
        'MESA_state':mesa.get('state'),'IMU_provenance':mesa.get('provenance',{}).get('online_imu_ingress'),
        'initial_alignment':mesa.get('provenance',{}).get('tdoc_simulation_state_alignment')}
    (bootstrap if 'pre-release' in p.name else cycles).append(row)
events=[]
for p in (DATA/'runtime/events/invocations/chunks').glob('*.gz'):
    events.extend(json.loads(line) for line in gzip.decompress(p.read_bytes()).decode().splitlines() if line.strip())
blockers=[]
for x in events:
    if 'online' not in x.get('transaction_id',''):continue
    if x.get('reason') in ('no_valid_l4_artifact','invoke_exception') or x.get('status')=='DEADLINE_EXCEEDED':
        details=x.get('details') or {};gate=details.get('gate') or {}
        blockers.append({'transaction_id':x['transaction_id'],'layer':x.get('source'),'reason':x.get('reason'),
            'gate_reason':details.get('gate_reason'),'age_s':gate.get('artifact_age_s'),
            'position_delta_m':gate.get('nearest_position_delta_m'),'velocity_delta_m_s':gate.get('nearest_velocity_delta_m_s'),
            'elapsed_s':details.get('elapsed_s'),'deadline_s':details.get('deadline_s'),'error':details.get('error')})
images=[]
for p in sorted(E.glob('*/manifest.json')):
    m=safe(p,{})
    if m.get('all_six_captured'):images.append((p,m))
mission=bool(episodes and all(e.get('terminal_reached') for e in episodes) and images
    and any(c['actuation_executed'] and c['L7_status']=='OK' for c in cycles))
result={'generated_at':datetime.now(ZoneInfo('America/Toronto')).isoformat(),
    'mission_status':'PASS' if mission else 'FAIL','mission_episodes':episodes,'cycles':cycles,'bootstrap':bootstrap,
    'records':records,'blockers':blockers,'source_changes':changes,
    'compiled_sources_changed':False,'compiled_rebuild_required':False,'reset_performed':False,
    'preservation':safe(E/'preservation_final.json',{}),
    'tests':['260 selected TDOC regressions passed before final transport/hash changes',
             '16 Layer4 certificate regressions passed','112 segment transport tests passed',
             '55 current-prediction-snapshot/storage tests passed','111 prediction gate/model identity tests passed',
             '90 current prediction/historical-evidence tests passed','87 one-candidate FF budget tests passed',
             '85 final-source ingress/health/Layer7/roadmap tests passed'],
    'SS_recovery':safe(E/'SS_RECOVERY_RESULT.json'),
    'full_320_cycle_offline_summary':safe(HERE/'attempt1_full_prior/reports/offline/mission/MISSION_SUMMARY.json')}
result['final_blockers']=safe(E/'FINAL_BLOCKERS.json',{})
result['executed_feedback_cycles']=sum(c['actuation_executed'] and c['L7_status']=='OK' for c in cycles)
result['two_executed_feedback_cycles']=result['executed_feedback_cycles']>=2
result['ten_executed_feedback_cycles']=result['executed_feedback_cycles']>=10
result['historical_native_execution']={
    'session_id':'dcaa9a82cdc340999f1dbdac627d04fa',
    'cycle_file':'executed_missing_prediction/runtime/cycles/workflow-online_jsbsim-e0001-c0002.json.gz',
    'L5_FINAL':True,'L6_status':'EXECUTED','L7_status':'REJECTED',
    'duration_bounds_s':{'lower_s':0.03900000000000148,'upper_s':0.08099999999999596},
    'meaning':'actual MPC-derived ARM and bounded execution, not a closed feedback cycle'}
result['next_cycle_feedback_consumed']=any(
    ((c.get('previous_L7_feedback') or {}).get('layer2_ingress') or {}).get('consumed') is True for c in cycles)
milestones=[
    ('ثبت سورس، forcing و هویت prior','PASS' if result['preservation'].get('source_unchanged_during_final_online') else 'BLOCKED'),
    ('هم‌زمانی شش پنل یک نشست','PASS' if images else 'BLOCKED'),
    ('bootstrap جاری L2→L3→L4→L5 FINAL','PASS' if any(c['L5_FINAL'] for c in bootstrap) else 'BLOCKED'),
    ('ARM/QHOVER توسط MPC واقعی','PASS — نشست dcaa9a82'),
    ('segment اجرایی با proof معتبر','PASS — نشست dcaa9a82'),
    ('residual معتبر همان candidate در L7','PASS' if result['executed_feedback_cycles'] else 'BLOCKED'),
    ('مصرف feedback در L2 cycle بعد','PASS' if result['next_cycle_feedback_consumed'] else 'BLOCKED'),
    ('دو cycle اجرایی همراه بازخورد','PASS' if result['two_executed_feedback_cycles'] else 'BLOCKED'),
    ('حداقل ده cycle اجرایی همراه بازخورد','PASS' if result['ten_executed_feedback_cycles'] else 'BLOCKED'),
    ('رسیدن به مقصد و touchdown',result['mission_status']),
    ('build/Reset برای تغییر سورس کامپایل‌شونده','N/A — چنین تغییری انجام نشد؛ reset واقعی آزموده نشد'),
]
result['microstep_acceptance']=milestones
(E/'FINAL_RESULT.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
rows=''.join('<tr>'+''.join('<td>'+esc(c.get(k))+'</td>' for k in ('transaction_id','L4_verified_count','L5_FINAL','L6_status','L7_status'))+'</tr>' for c in cycles)
gallery=''
for p,m in images:
    gallery+='<h3>'+esc(m['native_session_id'])+'</h3><p><a href="'+str(p.relative_to(HERE))+'">manifest نشست و سلامت پنل‌ها</a></p><div class="gallery">'
    for role in ('ardupilot','jsbsim','environment','gui','map','console'):
        path=p.parent/(role+'.png')
        gallery+='<figure><img src="'+str(path.relative_to(HERE))+'"><figcaption>'+role+'</figcaption></figure>'
    gallery+='</div>'
body='''<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8"><title>گزارش اجرای میکروگام‌های TDDT</title>
<style>body{font:16px/1.9 Tahoma,Arial;background:#eef2f6;color:#142637}main{max-width:1180px;margin:24px auto;background:white;padding:30px}h1,h2{color:#16456b}table{border-collapse:collapse;width:100%}td,th{border:1px solid #bbcbd9;padding:8px}pre{direction:ltr;text-align:left;white-space:pre-wrap;overflow-wrap:anywhere;background:#edf3f8;padding:16px;max-height:650px;overflow:auto}.notice{background:#fff0d5;padding:16px}.fail{background:#ffe2e2;padding:18px}.gallery{display:grid;grid-template-columns:1fr 1fr;gap:16px}img{width:100%}figure{margin:8px 0}code{direction:ltr;unicode-bidi:embed}</style><main>
<h1>اجرای میکروگام‌های سبک‌وزن برای حلقهٔ Native ۲ تا ۷</h1>'''
body+='<p>'+esc(result['generated_at'])+'</p><p class="fail"><strong>وضعیت مأموریت: '+result['mission_status']+'</strong>. معیار، اجرای واقعی تصمیم MPC و بازخورد لایه۷ و رسیدن به touchdown است. بازبودن پنل‌ها، FINAL غیر اجرایی و ARM مستقل معیار تکمیل مأموریت نیستند.</p>'
body+='''<p class="notice"><strong>پیشرفت واقعی:</strong> در نشست dcaa9a82 فرمان همین حلقهٔ MPC باعث QHOVER/ARM شد و یک segment با proof معتبر لایه۶ EXECUTED ثبت شد؛ مدت اجرا با کران ۳۹ تا ۸۱ میلی‌ثانیه برای سقف ۱۰۰ میلی‌ثانیه اندازه‌گیری شد. در آن نسخه، لایه۷ به دلیل نبود پیش‌بینی فیزیکی candidate انتخابی و استفادهٔ نامناسب از آزمون freshness جاری رد کرد. اصلاحات بعدی و نتیجهٔ آخرین اجرا در جدول زیر جداگانه آمده‌اند. این ARM آزمون مستقل gamepad یا command replay نبود. <a href="executed_missing_prediction/runtime/cycles/workflow-online_jsbsim-e0001-c0002.json.gz">CycleResult واقعی آن اجرا</a></p>'''
body+='<h2>پذیرش میکروگام‌ها</h2><table><tr><th>گام</th><th>وضعیت مبتنی بر evidence</th></tr>'+''.join('<tr><td>'+esc(a)+'</td><td>'+esc(b)+'</td></tr>' for a,b in milestones)+'</table>'
body+='''<h2>تغییرات اعمال‌شده</h2><ol>
<li>در لایه۲ همهٔ گام‌های buffered IMU با FMU و کنترل TEP اجرا می‌شوند؛ فقط packet نهایی به دیکشنری تبدیل می‌شود. زمان setup، climate، دریافت observation/history، propagation و feedback ثبت می‌شود.</li>
<li>اسکن قابلیت بانک گواهی لایه۴ به‌جای صدها برداشت تکراری هویت، یک برداشت در آغاز و بازبینی مستقل در پایان دارد. مصرف واقعی گواهی همچنان کنترل مستقل دارد.</li>
<li>Full-Fidelity غیر اجرایی از هویت مقید به همان محاسبه استفاده می‌کند و پیش از تحویل دوباره hash می‌شود. ContextVar مانع انتقال این وضعیت به تصمیم زندهٔ هم‌زمان می‌شود. کپی buffer تکراری IMU در ردیف‌های فرضی حذف شده است؛ anchor و داده‌های state/TEP حفظ می‌شوند. خوانندهٔ فقط‌خواندنی نقشه/CRP با کنترل هویت فایل‌ها reuse می‌شود؛ engine، FMU و state هر replay همچنان ایزوله است.</li>
<li>roll/pitch اولیه، فقط یک بار در حالت grounded، disarmed، throttle صفر و IMU کم‌حرکت از support force اندازه‌گیری‌شده گرفته می‌شود. مبدأ و yaw مأموریت حفظ می‌شوند؛ GPS و attitude کنترلر وارد MESA نمی‌شوند. نمونه‌ها و نرخ‌های خام تغییر نمی‌کنند.</li>
<li>نشست آماده و استفاده‌نشدهٔ نخست بدون restart پذیرفته می‌شود. اپیزودهای بعدی همچنان reset نشست دارند.</li>
<li>علت توقف واقعی L2/L3 پیش از توقف‌های تبعی L4/L5 گزارش می‌شود. شکست متوالی bootstrap همان حد موجود توقف بی‌عملی را رعایت می‌کند و علت‌های Full-Fidelity را ثبت می‌کند.</li></ol>
<ol start="7"><li>ورود passive از on_ground، پس از bootstrap معتبر و مشاهدهٔ تازه، هم‌نشست، grounded و disarmed انجام می‌شود؛ چرخهٔ اضافی چندثانیه‌ای حذف شده است. این مشاهده مجوز actuation یا تازه‌سازی مصنوعی state ناوبری نمی‌دهد؛ cycle کنترلی بعدی همهٔ گیت‌های عادی را دارد.</li>
<li>پاسخ segment لایه۶ فقط نام‌های canonical را حمل می‌کند. حذف aliasهای تکراری، wire evidence و clock proof را حفظ می‌کند. خطای عبور از سقف datagram با اندازهٔ واقعی گزارش می‌شود.</li>
<li>در reuse معتبر، snapshot اصلی پیش‌بینی از حافظهٔ همان artifact gate تحویل می‌شود. hash پیش‌بینی با snapshot اصلی و hash وضعیت شبکه با snapshot جاری مستقل بررسی می‌شوند. تاریخ و hash پیش‌بینی تغییر نمی‌کند. flags ذخیره‌شده به‌تنهایی مجوز نیستند.</li>
<li>رد قرارداد ورودی لایه۷ به‌عنوان FAILED ذخیره می‌شود و evidence اجرای پیشین لایه۶ را از CycleResult حذف نمی‌کند.</li>
<li>close کردن دوبارهٔ workspace فشردهٔ KStore idempotent شد. sync پس از بسته‌شدن یا ناپدیدشدن پوشهٔ موقت رد می‌شود تا حذف موقت به حذف دانش پایدار منجر نشود.</li></ol>
<p>در acquisition بومی، بررسی کامل processها و پنل‌ها یک بار و پیش از آماده‌سازی history انجام می‌شود. تلاش‌های catch-up هویت نشست و observation تازه را دوباره بررسی می‌کنند؛ gate پیش از actuation نیز برقرار است. زمان initial status، component health، history prepare، final status و catch-up جداگانه در <code>native_acquisition_timing.stages_s</code> ثبت می‌شود. ۸۵ آزمون مربوط به retention، سلامت، لایه۷ و مرزهای این تغییر موفق شد. <a href="evidence/tests_ingress_prework.log">نتیجهٔ آزمون‌ها</a></p>
<h2>آزمایش تنظیم سبک‌تر و بازگردانی</h2><p>horizon و reduced_scenarios هر دو از ۶ به ۳ کاهش یافتند. اجرای واقعی، رد candidateها با <code>vertical_takeoff_trajectory_collective_outside_source_braking_envelope</code> را نشان داد. این تنظیم پذیرفته نشد؛ هر دو مقدار به ۶ بازگشتند. بودجهٔ انتخاب Full-Fidelity از top_k=2 به 1 کاهش یافت؛ مجموعهٔ شش‌سناریویی و افق شش‌گامی حفظ شده است. هر candidate اجرایی همچنان باید Full-Fidelity و FINAL معتبر داشته باشد. حدود موتور، TTL سی‌ثانیه‌ای و envelope موقعیت/سرعت تغییر نکردند.</p>
<h2>شواهد اندازه‌گیری و محدودیت‌ها</h2><p>پروفایل اولیهٔ ۱۰۰۰ نمونه، ۱۵٫۴۹ ثانیه هزینهٔ تبدیل packet در مجموع ۱۷٫۲۵ ثانیه نشان داد. در لایه۴، اسکن بانک هویت ۴۳۲ بار تکرار می‌شد. اجرای Native نخست پس از اصلاح، L4 را در ۲۰٫۷۶ ثانیه تحویل داد، ولی artifact هنگام مصرف ۳۸٫۰۴ ثانیه عمر داشت و رد شد. اجرای بعدی با تنظیم ناموفقِ horizon=3، به Full-Fidelity معتبر نرسید؛ زمان کوتاه آن شاهد موفقیت prediction نیست.</p>
<p>بازپخش IMU واقعی از زمان مقداردهی اولیه، drift افقی ده‌ثانیه‌ای ۴۰٫۷ متر برای زاویهٔ ثابت و ۳٫۳ متر برای مقداردهی اولیه از gravity نشان داد. این فقط آزمون boundary مقداردهی اولیه است؛ drift طولانی‌مدت estimator و موفقیت پرواز را اثبات نمی‌کند. پروفایل سرد تشخیصی Full-Fidelity پس از ۱۲۵ ثانیه ابزاربندی‌شده قطع شد و نتیجهٔ پذیرش محسوب نمی‌شود.</p>
<h2>اجرای نهایی و علت توقف</h2>'''
body+=dump([{k:e.get(k) for k in ('origin_verified','cycles_completed','termination_reason','terminal_reached','rtmo_final_phase','final_goal_distance_m')} for e in episodes])
body+='<p>cycleهای دارای اجرای واقعی و بازخورد OK: <strong>'+str(result['executed_feedback_cycles'])+'</strong>. شمارش دو cycle و ده cycle فقط بر اساس همین evidence انجام می‌شود.</p>'
body+='<table><tr><th>تراکنش</th><th>L4 verified</th><th>L5 FINAL</th><th>L6</th><th>L7</th></tr>'+rows+'</table>'+dump(blockers)
body+='''<p><a href="evidence/online_final.log">log نهایی canonical nav-online</a> — <a href="final_native/reports/online/ONLINE_JSBSIM_SUMMARY.json">خلاصهٔ ثابت‌شدهٔ آخرین اجرا</a> — <a href="evidence/FINAL_BLOCKERS.json">موانع نهایی و خطای کامل</a> — <a href="evidence/native_session_archive.json">بایگانی نشست‌های Native شامل log و tlog</a></p>'''
body+='<h2>نتیجهٔ آخرین اجرای سورس نهایی</h2>'+dump(result['final_blockers'])
body+='''<p>در آخرین نشست، L4 برابر ۱۰٫۴۶ ثانیه و L5 پیش از release برابر ۲٫۹۱ ثانیه بود. acquisition موفق cycle دوم ۱٫۰۸ ثانیه طول کشید: initial status برابر ۰٫۶۱۰، health برابر ۰٫۰۳۷، آماده‌سازی history برابر ۰٫۲۸۶ و final status برابر ۰٫۱۴۱ ثانیه. این اندازه‌گیری نشان می‌دهد هزینهٔ health در این acquisition کوتاه است؛ باقی‌ماندن drift از روی زمان کل pipeline و state واقعی گزارش می‌شود و به مدل باد نسبت داده نمی‌شود. در cycle چهارم، forcing_ok=True بود، ولی بازاعتبارسنجی ingress رد شد؛ علت دقیق زیرگیت از خطای موجود قابل تعیین نیست و حدس زده نشده است. حلقهٔ اجرایی به همین دلیل و مانع corridor، هنوز تأیید Native ندارد.</p>'''
body+='''<h2>موانع باقی‌مانده و مرز دامنه</h2><p>در اجرای دو-candidate با پیش‌بینی تازه، L4 در ۱۷٫۷۹ ثانیه پایان یافت. هنگام مصرف artifact با عمر ۲۸٫۰۲ ثانیه، state جاری MESA حدود ۶۹٫۴۲ متر و ۷٫۵۳ متر‌برثانیه از corridor فاصله داشت؛ حدود پذیرش همان ۲۰ متر و ۴ متر‌برثانیه باقی ماند و فرمان صادر نشد. cycle بعدی با ۱۱٫۱۴ ثانیه از deadline لایه۲ عبور کرد. این شواهد نشان می‌دهند آماده‌سازی کند در حالی که plant و IMU جلو می‌روند، برای این نشست مانع است؛ صرف بازشدن پنل‌ها و سالم‌بودن MAVLink آن را رفع نمی‌کند.</p><p>بودجهٔ FF یک‌candidate راهکار سبک بعدی است، با prediction جاری و horizon/scenario دست‌نخورده. نتیجهٔ واقعی آن در آخرین cycleها ثبت شده است. ادعای cadence ده‌هرتز، hover پایدار، ده یا بیست cycle متوالی و touchdown فقط با evidence همان مرحله قابل پذیرش است. تنظیم مجدد estimator، مدل باد، دینامیک Phoenix و قوانین کنترل خارج از این اصلاح رابط بوده و انجام نشده است. لازم است هر مانع باقیمانده با زمان‌بندی pipeline و envelope فعلی حل شود یا به مهندس مسئول مدل/کنترل ارجاع داده شود؛ گشادکردن حدود، re-anchoring به FCU و تازه‌سازی مصنوعی forecast راه‌حل این گزارش نیستند.</p>'''
body+='''<p>جزئیات bootstrap، ردهای safety، clock و state هر cycle در <a href="evidence/FINAL_RESULT.json">FINAL_RESULT.json</a> ثبت شده‌اند. نتیجهٔ مراحل ARM از MPC، یک segment اجرایی، دو cycle متوالی، ۱۰–۲۰ cycle و مأموریت کامل فقط در صورت وجود evidence واقعی پذیرفته می‌شود.</p>
<h2>دانش، forcing و build</h2><p>workspaceهای قبلی بایگانی شدند؛ reset، refresh-external و تغییر forcing انجام نشد. prior هر نسخه از اجرای Offline همان سورس ساخته شد؛ گواهی قدیمی با hash جدید بازبرچسب‌گذاری نشد. هیچ سورس FMU/C/C++/ArduPilot/JSBSim و هیچ binary اصلاح نشده است. بنابراین rebuild جدید لازم نبود. آزمون reset واقعی انجام نشده و برای آن PASS صادر نمی‌شود.</p>'''
body+=dump(result['preservation'])
body+='''<h2>خطای حفظ دانش و بازیابی</h2><p>در یکی از اجراهای همین بررسی، close دوبارهٔ workspace باعث حذف ۲۳ فایل metadata از prior تازهٔ ۳۲۰-cycle شد. این خطا به‌صورت مستقل بازتولید و اصلاح شد. ۵۰۰ رکورد SS و زنجیرهٔ hash آن‌ها کامل بازیابی و با آخرین hash handoff اصلی تطبیق داده شد؛ metadata اصلی SETD به‌صورت byte-exact بازیابی نشده است. این دادهٔ بازیابی‌شده مجوز پرواز یا handoff جایگزین نیست. prior نهایی از اجرای استاندارد تازهٔ همان سورس ساخته شده و snapshot قبل/بعد و نسخهٔ پشتیبان دارد. بایگانی آغاز کار کاربر دست‌نخورده نگه داشته شد. <a href="evidence/SS_RECOVERY_RESULT.json">نتیجهٔ دقیق بازیابی</a></p>
<h2>محدودیت آموزش و محدودهٔ GNSS</h2><p>اجرای طولانی ۳۲۰-cycle همین بررسی تا touchdown موفق نشد؛ در terminal_reposition حدود ۲۲۴٫۱ متر تا مقصد و ۹۴٫۹ متر AGL باقی ماند. prior نهایی کوتاه، سه-cycle و برای آزمودن رابط است، نه گواهی مأموریت کامل. دانش prior راهنماست و جای state جاری را نمی‌گیرد. در مسیر MESA ورودی IMU واقعی مصرف می‌شود و GPS/pose کنترلر جایگزین آن نشده است؛ با این حال استفادهٔ GPS داخلی EKF کنترلر در log مشاهده شده، بنابراین GNSS-denied بودن کل FCU ادعا نمی‌شود.</p>'''
body+='''<h2>آزمون‌ها و سورس</h2><p>۲۶۰ آزمون منتخب TDOC پیش از اصلاح نهایی transport/hash و ۱۶ آزمون مستقل گواهی لایه۴ موفق شدند. پس از اصلاح نهایی prediction/historical evidence، ۹۰ آزمون و پس از تنظیم بودجهٔ FF، ۸۷ آزمون موفق شدند. این انتخاب‌ها همپوشانی دارند و جمع آن‌ها تعداد آزمون یکتا نیست. وابستگی تست‌ها به prior و گزارش عملیاتی با fixture مستقل رفع شد؛ اجرای کوتاه هم‌زمان با تست‌ها در تأیید نهایی استفاده نشد.</p><p><a href="source_changes.patch">patch سورس</a> — <a href="source_changes.json">hash سورس‌ها</a> — <a href="evidence/regression_final2.log">۲۶۰ آزمون TDOC</a> — <a href="evidence/layer4_regression.log">آزمون‌های لایه۴</a> — <a href="evidence/tests_current_prediction.log">۹۰ آزمون prediction</a> — <a href="evidence/tests_light_ff_budget.log">۸۷ آزمون تنظیم نهایی</a></p>
<h2>آزمون‌های اصلاحات نهایی</h2><p>پس از اصلاح transport، ۱۱۲ آزمون موفق شد. پس از اصلاح مرز snapshot، ۵۵ آزمون لایه۷/storage و ۱۱۱ آزمون artifact/model identity موفق شدند. این مجموعه‌ها با آزمون‌های قبلی همپوشانی دارند و تعدادشان جمع زده نمی‌شود. یک آزمون قدیمی، legacy بودن prior زندهٔ کاربر را فرض می‌کرد؛ به fixture مستقل منتقل شد.</p><p><a href="evidence/tests_segment_transport.log">segment transport</a> — <a href="evidence/tests_snapshot_binding.log">snapshot و storage</a> — <a href="evidence/tests_prediction_gate.log">artifact و model identity</a></p>
<p><a href="evidence/TEST_SOURCE_MANIFEST.json">سورس و hash آزمون‌های افزوده/اصلاح‌شده</a> — <a href="../SETUP.md">دستورات و توضیح رفتار استاندارد در SETUP.md</a></p>
<h2>تصاویر واقعی شش پنل</h2><p>این تصاویر به نشست‌های همین بررسی تعلق دارند؛ GUI و Map به telemetry همان ArduPilot/JSBSim متصل‌اند. تصاویر ARMED نشست 8b176f95 مربوط به فرمان MPC واقعی هستند، ولی در آن اجرا segment به لایه۷ نرسید. هر manifest هویت نشست و وضعیت GUI هنگام capture را نگه می‌دارد. تصاویر نشست‌های متفاوت، مدرک هم‌زمانی یک نشست محسوب نمی‌شوند.</p>'''+gallery+'</main></html>'
HERE.joinpath('FINAL_NATIVE_REPORT_FA.html').write_text(body)
print(result['mission_status'],'cycles',len(cycles),'episodes',len(episodes),'screenshots',len(images))
