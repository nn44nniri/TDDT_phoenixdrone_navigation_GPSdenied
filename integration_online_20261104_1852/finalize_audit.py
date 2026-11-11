"""Build a fresh Persian report from this transaction's code and Native evidence."""
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import difflib
import gzip
import hashlib
import html
import json
import math
import shutil

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent
TDOC=ROOT/'TDOC_v20260911_2052'
E=HERE/'evidence'


def read(p):
    p=Path(p)
    return json.loads(gzip.decompress(p.read_bytes()) if p.suffix=='.gz' else p.read_bytes())


def safe(p,default=None):
    try:return read(p)
    except (OSError,ValueError):return default


def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def esc(s):return html.escape(str(s))


def dump(v):return '<pre dir="ltr">'+esc(json.dumps(v,indent=2,ensure_ascii=False))+'</pre>'


def link(p,label=None):
    p=Path(p)
    try:target=p.relative_to(HERE)
    except ValueError:target=Path('../')/p.relative_to(ROOT)
    return '<a href="'+esc(target)+'">'+esc(label or p.name)+'</a>'


def table(headers,rows):
    return '<table><thead><tr>'+''.join('<th>'+esc(x)+'</th>' for x in headers)+'</tr></thead><tbody>'+''.join(
        '<tr>'+''.join('<td>'+str(x)+'</td>' for x in row)+'</tr>' for row in rows)+'</tbody></table>'


out=E/'final_native';out.mkdir(exist_ok=True)
shutil.copytree(TDOC/'reports/online',out/'reports',dirs_exist_ok=True)
shutil.copytree(TDOC/'runtime/cycles',out/'cycles',dirs_exist_ok=True)
shutil.copytree(TDOC/'runtime/events',out/'events',dirs_exist_ok=True)
prior=read(E/'final_prior_and_binaries.json')
preservation=safe(E/'preservation_check.json',{})
summary=read(out/'reports/ONLINE_JSBSIM_SUMMARY.json')
records=summary.get('records') or [];episodes=summary.get('episodes') or []
events=[]
for p in (out/'events/invocations/chunks').glob('*.gz'):
    events.extend(json.loads(line) for line in gzip.decompress(p.read_bytes()).decode().splitlines() if line.strip())
blockers=[]
for e in events:
    if 'online' not in e.get('transaction_id',''):continue
    if e.get('reason')=='no_valid_l4_artifact':
        d=e.get('details') or {};g=d.get('gate') or {}
        blockers.append({'transaction_id':e['transaction_id'],'gate_reason':d.get('gate_reason'),
            'artifact_age_s':g.get('artifact_age_s'),'artifact_phase':g.get('phase'),
            'nearest_position_delta_m':g.get('nearest_position_delta_m'),
            'nearest_velocity_delta_m_s':g.get('nearest_velocity_delta_m_s'),'refresh':d.get('refresh')})
    if e.get('reason')=='invoke_exception':
        blockers.append({'transaction_id':e['transaction_id'],'layer':e.get('source'),
            'error':(e.get('details') or {}).get('error')})
    if e.get('status')=='DEADLINE_EXCEEDED':
        d=e.get('details') or {}
        blockers.append({'transaction_id':e['transaction_id'],'layer':e.get('source'),
            'reason':'late_output_discarded','elapsed_s':d.get('elapsed_s'),
            'deadline_s':d.get('deadline_s'),'deadline_key':d.get('deadline_key')})
changes=[];patch=[]
for oldroot,newroot in [(HERE/'before/src',TDOC/'src'),(HERE/'before/scripts',TDOC/'scripts')]:
    for old in oldroot.rglob('*.py'):
        new=newroot/old.relative_to(oldroot)
        if new.is_file() and old.read_bytes()!=new.read_bytes():
            changes.append({'file':str(new.relative_to(ROOT)),'before_sha256':sha(old),'after_sha256':sha(new)})
            patch.extend(difflib.unified_diff(old.read_text().splitlines(True),new.read_text().splitlines(True),
                fromfile=str(old.relative_to(ROOT)),tofile=str(new.relative_to(ROOT))))
HERE.joinpath('source_changes.patch').write_text(''.join(patch))
HERE.joinpath('source_changes.json').write_text(json.dumps(changes,indent=2))
diag=read(E/'layer6_arm_diagnostic_ready.json')
cycles=[]
for record in records:
    ci=record.get('cycle_in_episode')
    p=out/'cycles'/f'workflow-online_jsbsim-e0001-c{ci:04d}.json.gz'
    d=safe(p,{})
    layers=d.get('downstream_layers') or {};l5=((layers.get('5') or {}).get('output') or {}).get('result') or {}
    l6=(layers.get('6') or {}).get('output') or {}
    m=((d.get('layer2_packets') or {}).get('MESA_NAV') or {})
    state=m.get('state') or {};init=(m.get('context') or {}).get('rtmo_navigation_initial_state') or {}
    cycles.append({'cycle':ci,'phase':record.get('rtmo_phase'),'wall_cycle_s':record.get('wall_cycle_s'),
        'L5_FINAL':(l5.get('gates') or {}).get('FINAL'),'L6_status':l6.get('status') or (record.get('layer6_runtime') or {}).get('status'),
        'actuation_executed':l6.get('actuation_executed'),'L7_status':record.get('layer7_status'),
        'clock':record.get('rtmo_clock'),'error':record.get('error'),
        'mesa_position_n':state.get('position_n'),'mesa_position_e':state.get('position_e'),
        'mesa_velocity_n':state.get('velocity_n'),'mesa_velocity_e':state.get('velocity_e'),
        'plant_time_s':m.get('timestamp')})
accepted=bool(episodes and all(e.get('terminal_reached') for e in episodes)
    and any(x['actuation_executed'] is True for x in cycles)
    and any(x['L7_status']=='OK' for x in cycles))
result={'generated_at':datetime.now(ZoneInfo('America/Toronto')).isoformat(),
    'mission_status':'PASS' if accepted else 'FAIL','interface_ARM_only':diag.get('ok') is True,
    'interface_is_MPC_mission':False,'mission_episodes':[{k:e.get(k) for k in
        ['origin_verified','origin_distance_m','cycles_completed','termination_reason','terminal_reached','rtmo_final_phase','final_goal_distance_m']}
        for e in episodes],'cycles':cycles,'blockers':blockers,'prior':prior,'changed_sources':changes,
    'tests':'194 selected tests passed; regression_final.log','compiled_sources_changed':False,
    'compiled_rebuild_performed':False,'reset_performed':False,'preservation':preservation}
E.joinpath('FINAL_RESULT.json').write_text(json.dumps(result,indent=2,ensure_ascii=False))

# Compare only one Native mission session; the FCU line is a diagnostic reference.
chart=''
try:
    initial=safe(out/'cycles/workflow-online_jsbsim-e0001-c0001-pre-release-vertical_takeoff.json.gz',{})
    init=initial['layer2_packets']['MESA_NAV']['context']['rtmo_navigation_initial_state']
    sid=episodes[0]['ptlib_episode_preparation']['native_session']['session_id']
    native=[]
    for line in E.joinpath('native_observations.jsonl').read_text().splitlines():
        x=json.loads(line);s=x.get('status') or {}
        if (x.get('health') or {}).get('session_id')==sid and s.get('mavlink_connected'):
            lat,lon=s['latitude_deg'],s['longitude_deg'];a=math.radians(lat);b=math.radians(40.01)
            h=math.sin((a-b)/2)**2+math.cos(a)*math.cos(b)*math.sin(math.radians(lon+105.3)/2)**2
            native.append((s['plant_time_s'],2*6371008.8*math.asin(math.sqrt(min(1.,h)))))
    mesa=[(x['plant_time_s'],math.hypot(x['mesa_position_n']-init['pn'],x['mesa_position_e']-init['pe']))
        for x in cycles if x['mesa_position_n'] is not None]
    from plotly.subplots import make_subplots
    import plotly.graph_objects as go
    fig=make_subplots(rows=2,cols=1,subplot_titles=('MESA estimated displacement','FCU validation reference only'))
    if mesa:fig.add_trace(go.Scatter(x=[x[0] for x in mesa],y=[x[1] for x in mesa],mode='lines+markers',name='MESA'),row=1,col=1)
    if native:fig.add_trace(go.Scatter(x=[x[0] for x in native],y=[x[1] for x in native],mode='lines',name='FCU reference'),row=2,col=1)
    fig.update_yaxes(title_text='Distance from origin (m)')
    fig.update_xaxes(title_text='Native plant time (s)',row=2,col=1)
    fig.update_layout(height=650,title='One Native mission session; FCU reference is not a MESA input')
    fig.write_html(E/'navigation_vs_reference.html',include_plotlyjs=True)
    chart='<figure><iframe style="width:100%;height:690px;border:0" src="evidence/navigation_vs_reference.html"></iframe><figcaption>مقایسهٔ تشخیصی موقعیت تخمینی MESA با مرجع FCU در یک نشست؛ مرجع FCU به ناوبری تزریق نشده است.</figcaption></figure>'
except (KeyError,ValueError,OSError,ImportError) as exc:
    result['chart_unavailable']=str(exc)
E.joinpath('FINAL_RESULT.json').write_text(json.dumps(result,indent=2,ensure_ascii=False))

panels=''
for folder in sorted(E.glob('armed_QHOVER_*')):
    manifest=safe(folder/'manifest.json',{})
    panels+='<p class="notice">تصاویر این بخش متعلق به آزمون مستقل رابط با transaction برابر <code>interface-ready-arm</code> هستند؛ نتیجهٔ مأموریت MPC نیستند.</p>'
    panels+='<p>Native session: <code>'+esc(manifest.get('native_session_id'))+'</code> — '+link(folder/'manifest.json','manifest و شواهد اتصال')+'</p><div class="gallery">'
    for role in ['ardupilot','jsbsim','environment','gui','map','console']:
        p=folder/(role+'.png')
        if p.exists():panels+='<figure><a href="'+esc(p.relative_to(HERE))+'"><img src="'+esc(p.relative_to(HERE))+'"></a><figcaption>'+esc(role)+'</figcaption></figure>'
    panels+='</div>'

rows=[[str(x['cycle']),esc(x['phase']),f"{x['wall_cycle_s']:.3f}" if isinstance(x['wall_cycle_s'],(int,float)) else '—',
       esc(x['L5_FINAL']),esc(x['L6_status']),esc(x['L7_status'])] for x in cycles]
source_rows=[[link(ROOT/x['file'],x['file']),'<code>'+x['after_sha256']+'</code>'] for x in changes]
binary_rows=[[esc(k),str(v['bytes']),'<code>'+esc(v['sha256'])+'</code>'] for k,v in prior['compiled_artifacts'].items()]
doc=f'''<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8"><title>گزارش تازهٔ اصلاح و اجرای Native — TDDT</title>
<style>body{{font:16px/1.9 Tahoma,Arial,sans-serif;background:#eef2f6;color:#172537;margin:0}}main{{max-width:1160px;margin:32px auto;background:white;padding:32px;border-radius:14px}}h1,h2{{color:#163958}}h2{{border-bottom:1px solid #ccd9e5;padding-top:20px}}table{{border-collapse:collapse;width:100%;margin:20px 0}}th,td{{border:1px solid #cbd5df;padding:9px;text-align:right;vertical-align:top}}th{{background:#e9f1f8}}code,pre{{font-family:monospace;overflow-wrap:anywhere}}pre{{direction:ltr;text-align:left;white-space:pre-wrap;background:#142539;color:#e8f0fa;padding:18px;border-radius:8px;max-height:600px;overflow:auto}}.notice{{background:#fff3dc;padding:16px;border-right:5px solid #cf8a00}}.fail{{background:#fde6e6;padding:18px;border-right:6px solid #bf3030}}.ok{{background:#e4f5eb;padding:16px}}.gallery{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}img{{width:100%;height:auto}}figure{{margin:12px 0}}a{{color:#065ea6}}small{{color:#526579}}@media print{{main{{margin:0;padding:0}}pre{{max-height:none}}}}</style>
<main><h1>گزارش اصلاح هماهنگ رابط و اجرای واقعی Native</h1><p>{esc(result['generated_at'])} — منطقهٔ زمانی Toronto</p>
<p class="fail"><strong>نتیجهٔ هدف مأموریتی: {result['mission_status']}.</strong> بازشدن پنل‌ها و ARM مستقلِ لایه۶ تأیید شد؛ هدایت پهپاد با تصمیم‌های MPC از مبدأ تا مقصد و بسته‌شدن حلقهٔ اجرایی ۲ تا ۷ تأیید نشد.</p>
<p>این گزارش فقط از سورس بررسی‌شده، اجرای تازه، telemetry، آزمون‌ها و تصاویر همین transaction ساخته شده است. گزارش‌های قبلی مبنای نتیجه نیستند. شاخهٔ شواهد: <code>{esc(HERE)}</code>.</p>
<h2>۱. چه چیزهایی واقعاً اصلاح شد</h2><ol>
<li><strong>تأیید مبدأ:</strong> بررسی محل فیزیکی شروع از position ناوبری MESA جدا شد. مشاهدهٔ تازه، grounded و disarmed از همان Native session با مبدأ نقشه مقایسه می‌شود. مختصات FCU فقط مرجع validation است و موقعیت یا سرعت MESA را بازنویسی نمی‌کند. در اجرای اولیه، drift پیش از ARM باعث رد مبدأ با فاصلهٔ ۵۰۳٫۸ متر شده بود؛ اجرای اصلاح‌شده از این بررسی عبور کرد.</li>
<li><strong>readiness بدون startup جانبی:</strong> callback تصمیم لایه۵ اکنون فقط نشست آماده‌شده را می‌خواند. preflight یا Offline نمی‌تواند با پرس‌وجوی readiness، پشتهٔ Native را ناخواسته راه‌اندازی کند.</li>
<li><strong>ترتیب phaseها:</strong> آماده‌سازی phase آینده در phase کنترل‌شده به اجرای تأییدشدهٔ لایه۶ و تکمیل بازخورد لایه۷ مشروط شد. bootstrap آغازین از on_ground به vertical_takeoff همچنان مجاز است. صرف گذشت زمان به forecast مرحلهٔ بعد مجوز نمی‌دهد.</li>
<li><strong>تاریخچهٔ IMU:</strong> timeout موقت transport با همان cursor، حداکثر سه بار پیاپی تکرار می‌شود. هیچ نمونه‌ای ساخته، interpolate یا تازه‌نمایی نمی‌شود. شکاف، تغییر session، دادهٔ معیوب و timeout تکرارشونده همچنان fail-closed هستند. علت خطای source نیز اکنون تا لایه۲ حفظ می‌شود.</li>
<li><strong>اسکریپت کمکی bridge:</strong> حالت <code>--execute</code> به workflow اصلی و تصمیم‌های جاری L2→L5 تفویض می‌شود؛ packet آرشیوی لایه۵ replay نمی‌شود. اجرای لایه۷ و route بازخورد از مسیر مالک TDOC انجام می‌شود. بدون execute فقط input بررسی می‌شود.</li>
<li><strong>تست‌های ناسازگار:</strong> ارجاع تست‌ها به دو فایل حذف‌شده با آزمون مسیر فعلی paging تاریخچهٔ خام و بررسی hashِ ورودی forcing جایگزین شد.</li></ol>
<h2>۲. اعتبار prior، forcing و حفظ اطلاعات قبلی</h2>
<p>runtime و reports قبلی در پوشه‌های <code>before</code>، <code>archived_before_fresh_prior</code> و <code>after_initial_fixes</code> حفظ شدند. KStore موجود طبق Single-Episode، آموزش دوم را با <code>offline episode limit=1 already consumed</code> رد کرد؛ این محدودیت دور زده نشد. workspace جاری پس از بایگانی ساخته شد و prior تازه با دستور استاندارد Offline و سقف سه cycle تولید شد.</p>
<p class="notice">Offline محدود، مأموریت کامل مبدأ تا touchdown نیست. خروجی کلی آن به‌علت تکمیل‌نشدن مأموریت code=2 بود، اما SETD/handoff نهایی به‌عنوان prior توسط validator پذیرفته شد. این پذیرش به‌عنوان مجوز پرواز یا شاهد تکمیل مأموریت استفاده نشد.</p>
<p>هیچ reset، refresh-external، دانلود مجدد، تغییر forcing یا برچسب‌گذاری مجدد handoff قبلی انجام نشد. گواهی تازه با سورس نهایی تطابق دارد. {link(E/'final_prior_and_binaries.json','شناسه و hashهای کامل prior و binaryها')}</p>
<p>hash کامل KStore پیش و پس از Online برابر بود و پنج artifact کامپایل‌شده نیز با hashهای ثبت‌شدهٔ قبل از تغییرات یکسان‌اند. {link(E/'preservation_check.json','بررسی مستقل حفظ KStore و binaryها')}</p>
{dump({'handoff':prior['gate'],'handoff_hash':prior['handoff_hash'],'model':prior['model'],'forcing_sha256':prior['forcing_sha256']})}
<h2>۳. آزمون مأموریت با دستور canonical</h2>
<pre>source configs/environment_fixed.sh
$PYTHON -m tddt_tdoc.cli --config configs/tdoc.yaml nav-online \\
  --reuse-existing-offline-knowledge --online-episodes 1 \\
  --online-max-cycles-per-episode-auto</pre>
<p>{link(E/'online_final.log','log اجرای نهایی')} — {link(E/'FINAL_RESULT.json','نتیجهٔ ساختاریافتهٔ این بررسی')}</p>
{dump(result['mission_episodes'])}
{table(['cycle','phase','زمان دیواری cycle (s)','L5 FINAL','وضعیت L6','وضعیت L7'],rows)}
<h2>۴. موانع باقی‌مانده و محل توقف</h2>
<p>آمادگی package/API و Native transport به معنی آماده‌بودن prediction برای مصرف جاری نیست. در اجرای قبلی همین transaction، نخستین artifact مرحلهٔ takeoff با عمر <strong>۵۳٫۱۱۸ ثانیه</strong> در مقابل حد <strong>۳۰ ثانیه</strong> رد شد؛ refresh بعدی نیز <code>newer_refresh_unverified</code> بود. این‌ها به ارسال command معتبر و جاری به لایه۶ منجر نشدند. جزئیات اجرای نهایی، از رویدادهای همین اجرا:</p>
{dump(blockers)}
<p>ردیف‌های <code>HELD</code> بدون <code>actuation_executed=true</code> و بدون ExecutionEvidence، دلیل بسته‌نشدن مسیر لایه۷ هستند. هیچ ACK یا FINAL مربوط به pre-release به اجرای واقعی ارتقا داده نشد. pre-release یک تراکنش L2→L5 غیر اجرایی است.</p>
<p class="notice">در اجرای نهایی، artifact مرحلهٔ takeoff با عمر <strong>۵۴٫۵۱۷ ثانیه</strong> رد شد. سپس پردازش لایه۲ در cycle سوم <strong>۶٫۵۰۸ ثانیه</strong> طول کشید و از deadline پنج‌ثانیه‌ای عبور کرد؛ خروجی آن طبق قرارداد کنار گذاشته شد. در نتیجه packet قابل مصرف MESA/Layer3 باقی نماند و پایان عمومی <code>rtmo_wait_plant_time</code> ثبت شد. این عنوان، علت اولیه را توضیح نمی‌دهد: telemetry فیزیکی همچنان موجود بود، اما خروجی دیررس لایه۲ برای کنترل پذیرفته نشد. خطای permanent تاریخچهٔ IMUِ اجرای پیشین در این اجرای نهایی مشاهده نشد.</p>
<p>فاصلهٔ تخمینی MESA در زمان disarmed ماندن FCU تغییر قابل‌توجه داشت. در دادهٔ پیش از اصلاح نهایی، initialization زاویهٔ pitch برابر ۸۹ درجه بود، درحالی‌که IMU تازه <code>ax=9.7674234، ay=0، az=-1.04931155 m/s²</code> و gyro حول y برابر <code>-0.002 rad/s</code> گزارش می‌کرد. این مشاهده، ناسازگاری میان شرط اولیه و دادهٔ جاری را برای بررسی بعدی مطرح می‌کند؛ تشخیص قطعی علت مدل یا estimator نیست. مدل باد، مدل هواگرد و فرمالیسم MESA/WingMPET برای کاهش این اختلاف تغییر نکردند.</p>
{chart}
<p>کاهش horizon، تغییر solver، افزایش خودسرانهٔ TTL/envelope، تزریق GPS به MESA یا ساخت FINAL مصنوعی برای گذر از موانع انجام نشد. برای رسیدن به مأموریت واقعی، مسیر L4 باید prediction معتبر را در زمان و محدودهٔ state جاری تحویل دهد و مسیر L2 باید تاریخچهٔ واقعی قابل مصرف را حفظ کند؛ نتیجهٔ همین اجرا برای هرکدام در JSON بالا ثبت شده است.</p>
<h2>۵. آزمون مستقل رابط ARM — موفق، با محدودهٔ روشن</h2>
<p class="ok">پس از انتظار برای FCU prearm health، دستورهای خنثی با throttle صفر از <code>Layer6Client.submit_command()</code> ارسال شدند: QHOVER → ARM → DISARM. وضعیت armed از telemetry و COMMAND_ACK فرمان ۴۰۰ با result=0 تأیید شد. force-arm، direct PWM و پرواز انجام نشد.</p>
<p>این آزمون از MPC لایه۵ صادر نشد و شاهد هدایت مأموریتی TDDT نیست. عنوان عمومی «TDDT Layer5-virtual» در GUI صرفاً نام کانال ورودی است؛ شناسهٔ واقعی این آزمون <code>interface-ready-arm</code> است. نخستین تلاشِ مستقل پیش از سلامت EKF متوقف شد؛ تلاش دوم با انتظار سلامت، موفق شد.</p>
{table(['مرحله','تأیید واقعی','MODE','ARM'],[[esc(s['step']),esc(s['ok']),esc((s.get('status') or {}).get('mode')),esc((s.get('status') or {}).get('armed'))] for s in diag['steps']])}
<p>{link(E/'layer6_arm_diagnostic_ready.json','telemetry و ACK کامل آزمون ARM')} — {link(E/'layer6_arm_diagnostic_ready.log','log آزمون ARM')}</p>
<h2>۶. تصاویر واقعی و اتصال هم‌زمان پنل‌ها</h2>
<p>ناظر فقط‌خواندنی، سلامت processها، session ID، freshness پنل‌ها، اتصال Map/Console و وضعیت GUI را ثبت کرده است. JSBSim نمایشگر backend همان فرزند ArduPilot است و simulator دوم ساخته نشده است. نقشهٔ واقعی، marker هواگرد و MODE/ARM همان session را نمایش می‌دهد. هیچ تصویر ساخته‌شده با AI در این گزارش وجود ندارد.</p>
{panels}
<p>{link(E/'native_observations.jsonl','نمونه‌های telemetry و سلامت نشست‌ها')} — تصاویر نشست مأموریت و آزمون مستقل در زیرپوشه‌های <code>evidence/disarmed_*</code> و <code>evidence/armed_*</code> جدا هستند. وجود تصاویر ARMED آزمون مستقل، به نتیجهٔ مأموریت منتقل نشده است.</p>
<h2>۷. محدودهٔ ادعای GNSS-denied</h2>
<p>در مسیر MESA، provenance ورودی IMU دارای <code>gnss_used=false</code> و <code>truth_position_used=false</code> بود. با این حال، STATUSTEXT مربوط به <code>EKF3 IMU0 is using GPS</code> در FCU مشاهده شد. بنابراین این گزارش، GNSS-denied بودن کل Flight Controller یا تکمیل پرواز GNSS-denied را تأیید نمی‌کند. position نقشه و تأیید مبدأ، مرجع validation هستند.</p>
<h2>۸. تست، سورس، build و Reset</h2>
<p><strong>۱۹۴ آزمون منتخب موفق شدند.</strong> آزمون‌ها شامل MPC واقعی در fixture، قراردادهای Native، correlation و segment، بازخورد لایه۷، سلامت پنل‌ها، origin validation، عدم startup در readiness، ترتیب phase و timeout محدود history هستند. این تست‌ها جای اجرای پرواز Native را نمی‌گیرند. {link(E/'regression_final.log','خروجی کامل آزمون‌ها')}</p>
<p>تغییرات اجرایی در Python و orchestration ثبت شده‌اند. هیچ سورس FMU، C/C++، ArduPilot یا JSBSim اصلاح نشده و هیچ binary وصله نشده است؛ بنابراین rebuild جدیدی برای binaryها انجام نشد. hashهای binary/FMU:</p>
{table(['artifact','اندازهٔ bytes','SHA-256'],binary_rows)}
<p>پایداری پس از reset واقعی آزمایش نشد؛ اجرای reset در این مرحله knowledge جاری را حذف می‌کرد. هیچ PASS برای rebuild/Reset صادر نمی‌شود. اصلاحات در فایل‌های سورس استاندارد قرار دارند. {link(HERE/'source_changes.patch','diff سورس‌ها')} — {link(HERE/'source_changes.json','hash قبل و بعد')}</p>
{table(['سورس تغییرکرده','SHA-256 نهایی'],source_rows)}
<h2>۹. شرایط پذیرش هدف</h2>
<p>پذیرش مأموریت به اجرای هم‌زمان و معتبر شش پنل، تصمیم تازه و مجاز لایه۵، ARM تأییدشده از همان مسیر، اجرای واقعی فرمان، خروجی پذیرفته‌شدهٔ لایه۷، مصرف feedback در چرخهٔ بعد و رسیدن واقعی به مقصد/touchdown وابسته است. در این transaction، کارکرد رابط ARM مستقل اثبات شد؛ زنجیرهٔ مأموریتی این شرایط را کامل نکرد. نتیجهٔ نهایی مأموریت <strong>{result['mission_status']}</strong> باقی می‌ماند.</p>
<p><small>گزارش خودبسنده است؛ فایل‌های JSON، log، diff و PNG پیوندشده برای بازبینی دقیق حفظ شده‌اند. نشست‌های ایجادشده پس از پایان آزمون با lifecycle رسمی بسته شدند.</small></p></main></html>'''
HERE.joinpath('FINAL_NATIVE_REPORT_FA.html').write_text(doc)
print(json.dumps({'report':str(HERE/'FINAL_NATIVE_REPORT_FA.html'),'mission_status':result['mission_status'],
    'interface_arm':result['interface_ARM_only'],'episodes':result['mission_episodes'],'blockers':blockers},indent=2,ensure_ascii=False))
