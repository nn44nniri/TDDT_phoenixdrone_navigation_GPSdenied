from pathlib import Path
import json,html,datetime
A=Path(__file__).resolve().parent
esc=lambda s:html.escape(str(s))
rows=[];full_pass=False;hover=[];completed=[]
for p in sorted(A.glob('attempt*/MISSION_RESULT.json')):
 r=json.loads(p.read_text());vpath=p.parent/'independent_validation.json';v=json.loads(vpath.read_text()) if vpath.exists() else {}
 qualified=v.get('status')=='PASS'
 if qualified and r.get('route_distance_m',0)>2000:
  trace=p.parent/'cross_layer_trace_validation.json';pres=p.parent/'preservation_final.json'
  tv=json.loads(trace.read_text()) if trace.exists() else {}
  pv=json.loads(pres.read_text()) if pres.exists() else {}
  if tv.get('status')=='PASS' and all(pv.get(k) is True for k in ('source_unchanged_during_final_online','yaml_unchanged','handoff_unchanged','kstore_unchanged','forcing_unchanged','compiled_artifacts_unchanged_during_online')):
   full_pass=True;completed.append((p.parent.name,r,v,tv))
 if qualified and v.get('actual_hover_continuous_seconds',0)>0:hover.append((p.parent.name,r,v))
 status=v.get('status',r['status']);link=p.relative_to(A)
 rows.append(f'<tr><td><a href="{link}">{esc(p.parent.name)}</a></td><td>{esc(status)}'+(' / مستقل' if v else ' / driver فقط')+f'</td><td>{r.get("cycles",0)}</td><td>{r.get("accepted_feedback",0)} / {r.get("consumed_feedback",0)}</td><td>{esc(r.get("inner_FCU_attitude_backend","—"))}</td><td>{esc(r.get("error","")[:450])}</td></tr>')
state='PASS — مأموریت کامل با ارزیابی مستقل' if full_pass else 'مأموریت کامل هنوز تأیید نشده است'
hover_text=''.join(f'<li>{esc(n)}: hover پیوسته {v["actual_hover_continuous_seconds"]:.2f} ثانیه؛ بیشینهٔ فاصله از مبدأ {v["peak_actual_radius_m"]:.2f} متر؛ خطای پایان {v.get("actual_goal_error_m",0):.2f} متر. Backend: {esc(r["inner_FCU_attitude_backend"])}. <a href="{n}/independent_validation.json">شواهد مستقل</a></li>' for n,r,v in hover)
mission_text=''.join(f'<p><strong>نشست {esc(n)}: مسیر {r["route_distance_m"]:.2f} متر تا مقصد اصلی، خطای فیزیکی پایان {v["actual_goal_error_m"]:.2f} متر، ارتفاع بیشینه {v["peak_actual_height_m"]:.2f} متر؛ برخاست، تماس زمین، ON_GROUND و DISARM عادی تأیید شدند.</strong> {r["cycles"]} چرخهٔ تصمیم و {tv["unique_feedbacks_consumed"]} بازخورد یکتای مصرف‌شده ثبت شد. <a href="{n}/MISSION_RESULT.json">نتیجه</a>، <a href="{n}/independent_validation.json">ارزیابی مستقل</a>، <a href="{n}/cross_layer_trace_validation.json">زنجیرهٔ همهٔ چرخه‌ها</a> و <a href="{n}/preservation_final.json">ثبات source / prior / forcing</a>.</p>' for n,r,v,tv in completed)
for n,r,v,tv in completed:
 q=json.loads((A/n/'FINAL_QUALIFICATION.json').read_text())
 mission_text+=f'<p>پرواز فیزیکی پیوسته بالاتر از یک متر: <strong>{q["longest_physical_flight_above_1m_s"]:.2f} ثانیه</strong>؛ زمان حلقهٔ کنترل {q["controlled_cycle_duration_s"]:.2f} ثانیه. آستانهٔ پذیرش مقصد از پیش ۲۵ متر است؛ خطای ۲۱٫۶۱ متر، فرود دقیق روی نقطه نیست. بدنه پس از فرود عمودی و ساکن بود. <a href="{n}/FINAL_QUALIFICATION.json">دامنه و معیارهای قبولی</a>.</p>'
manifest=json.loads((A/'evidence/realtime_model_before.json').read_text());model=manifest['model_dependencies']['model_id']
images=[]
for p in sorted((A/'evidence').glob('*/manifest.json')):
 d=json.loads(p.read_text())
 if d.get('all_six_captured'):
  images.append(f'<li><a href="{p.relative_to(A)}">{esc(p.parent.name)}</a> — شش تصویر واقعی؛ نشست {esc(d["native_session_id"])}</li>')
body=f'''<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8"><title>گزارش اجرای Native حسگرمحور TDDT</title>
<style>body{{font:16px/1.9 Tahoma,sans-serif;max-width:1300px;margin:auto;padding:32px;background:#101923;color:#e3edf7}}a{{color:#78c7ff}}table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{border:1px solid #425165;padding:8px;vertical-align:top}}code,pre{{direction:ltr;unicode-bidi:embed}}.state{{padding:18px;border:2px solid #f0b85a}}img{{max-width:100%}}li{{margin:8px 0}}</style>
<h1>گزارش آزمون‌های واقعی و اصلاحات Native</h1><p class="state">{state}</p>
<p>تولید: {esc(datetime.datetime.now().astimezone().isoformat())}. این گزارش بر پایهٔ سورس، لاگ MAVLink، چرخه‌ها و تصاویر این transaction است؛ گزارش‌های قبلی مبنای قبولی نیستند.</p>
<h2>نتیجهٔ قابل اثبات</h2>{mission_text}<ul>{hover_text or '<li>هنوز hover دارای ارزیابی مستقل ثبت نشده است.</li>'}</ul>
<p>قبولی یک نسخهٔ قبلی یا backend شبیه‌سازی AHRS، تأیید نسخهٔ فعلی حسگرمحور یا مسیر کامل نیست. هر نشست model ID و نتیجهٔ مستقل خود را دارد. نسخهٔ فعلیِ frozen: <code>{esc(model)}</code>.</p>
<h2>اصلاحات اجراشده</h2><ol>
<li>حلقهٔ TDOC با module واقعی هر لایه: propagator FMU اصلی MESA و WingMPET و MicroMet، snapshot لایهٔ ۳، rolloutهای لایهٔ ۴، انتخاب MPC و RTA لایهٔ ۵، اجرای GUI/MAVLink و residual زمان‌مند لایهٔ ۷.</li>
<li>رفع تکینگی Euler در مقداردهی اولیهٔ دم‌نشین با alignment دو بردار مجاز؛ آزادسازی قید سکون پیش از liftoff؛ حذف gravity pseudo-observation در پرواز؛ جلوگیری از anchor شدن سرعت زمینی با pitot اسکالر بدون باد مستقل.</li>
<li>ترجمهٔ صریح فرمان شتاب/lean/climb به pilot-domain با پارامترهای واقعی FCU؛ lease نیم‌ثانیه، تصمیم ۱۰ Hz و اجرای ۳۰ Hz؛ تطبیق raw MAVLink با instance/transaction و clock bounds. prediction برای پوشاندن تأخیر باززمان‌بندی نمی‌شود.</li>
<li>بازسازی propwash مدل Native از ضرایب WingMPET، اصلاح علامت سروو و بهره‌های inner-loop؛ تغییر گذار فیزیک operationهای ۲۷/۳۵ و Native به جریان مثبت در امتداد chord. هندسهٔ تماس و جرم و اینرسی مبنا حفظ شده‌اند.</li>
<li>پروفایل FCU فعلی: EKF3، بدون GPS/odometry/vision، startup در QHOVER بدون ARM؛ حذف مالکیت مد از کلید RC بلااستفاده؛ انتخاب EKF سالم در حالت VTOL با AHRS_OPTIONS=2. readiness مخصوص VTOL، attitude/velocity/vertical position و pre-arm واقعی را لازم دارد؛ موقعیت افقی مطلق شرط QHOVER نیست.</li>
<li>مبدأ از پیش معلوم EKF تنظیم و readback می‌شود. موقعیت SIM_STATE فقط برای نمایش، ارزیابی مستقل و نیروهای محیطی شبیه‌ساز است؛ اندازه‌گیری MESA یا residual کنترلی نیست. زمان موقعیت محیط با clockهای مجاور کران‌گذاری می‌شود.</li>
<li>پایان نزول با بارومتر/IMU qualify می‌شود؛ نزول آهسته تماس نیست. سکون بارومتری نزدیک زمین یا ضربهٔ IMU پس از نزول در ارتفاع بسیار پایین لازم است. شاهد تماس برای مدت محدود latch می‌شود تا گذرای قطع موتور آن را باطل نکند. فقط candidate گاز صفر می‌تواند از جهش نرخ/lean تماس عبور کند؛ فرمان پرواز چنین استثنایی ندارد. ON_GROUND و DISARM و تماس فیزیکی مستقل لازم‌اند. فرض ارتفاع این نمونه، زمین تقریباً هم‌تراز مبدأ است. abort، فرود موفق نیست.</li></ol>
<h2>بازکامپایل و ماندگاری</h2><p>سورس C++ اصلاح‌شدهٔ WingMPET با build استاندارد کامپایل و آزموده شد. reset واقعی با refresh و cache و بازسازی سه FMU اجرا و preparation موفق شد. سورس باقی ماند و shared libraryهای تکرار build یکسان بودند. rebuild استاندارد FlightStack نیز اجرا شد؛ خطای بازاعمال نسخهٔ مبنا در refresh پس از build اصلاح شد. hash سورس‌های داخل FMU با سورس جاری کنترل می‌شود؛ تغییر سورس بدون بازسازی رد می‌شود.</p>
<ul><li><a href="evidence/standard_reset_build_proof.json">اثبات reset و shared libraryها</a></li><li><a href="evidence/native_standard_build_proof.json">نسخه و hash build Native</a></li><li><a href="evidence/native_standard_rebuild_final_repeat.log">log تکرار نهایی build استاندارد</a></li><li><a href="evidence/preservation_final.json">کنترل source / forcing / handoff / KStore اجرای اخیر</a></li><li><a href="evidence/realtime_model_before.json">manifest سورس و binaryهای frozen</a></li></ul>
<h2>دانش و محدودیت دامنه</h2><p>prior عیب‌یابی از سه چرخهٔ آفلاین تولید شده و integrity و model identity آن بررسی می‌شود. این آموزش کامل مأموریت یا گواهی کنترل همهٔ فازها نیست؛ state فعلی و تصمیم تازه جایگزین نمی‌شوند. covariance مکمل پروفایل MESA نه‌بعدی است؛ این پیاده‌سازی ادعای همهٔ anchorها و covariance بیست‌ونه‌بعدی formalism اصلی را ندارد. نویز تصادفی پایهٔ IMU بومی حفظ شده است، اما پروفایل صریحاً <code>SIM_DRIFT_SPEED=0</code> دارد و drift مصنوعیِ متغیر با زمان را خاموش می‌کند؛ این آزمون اثبات ناوبری پایدار با drift حسگر واقعی یا aiding همهٔ شرایط نیست. مدل و کنترل فقط نمونهٔ SITL هستند و گواهی آیرودینامیک کامل یا پرواز واقعی صادر نمی‌کنند.</p>
<h2>ناوبری مسیر بلند و پایان فرود</h2><p>اجرای کامل ۱۷ تا نزدیکی مقصد پیش رفت اما با خطای مکان حدود ۶۳ متر و جهش نرخ روی زمین رد شد. مرجع مغناطیسی ثابتِ مبدأ و نبود جبران Earth-frame در propagation اصلاح شدند. بازپخش مستقلِ همان sensor log با سورس واقعی جدید، خطای پیش از فرود را از ۵۴٫۴۵ به ۱۳٫۸۰ متر کاهش داد؛ این بازپخش گواهی پرواز آنلاین نیست. حسگر یا noise floor جدیدی وارد نشده است. نزدیک زمین، مرحلهٔ flare فرمان‌های افقی را خنثی و نزول را آهسته می‌کند. <a href="evidence/geophysical_replay.json">مقایسهٔ فرضیه‌ها</a> و <a href="evidence/production_geophysical_replay.json">بازپخش سورس اصلاح‌شده</a>.</p>
<p>اجرای ۱۸ مسیر را تا خطای فیزیکی ۱٫۸۹ متر از مقصد طی کرد، ولی تشخیص تماس زودهنگام و ناپایدار موجب FAIL پایان فرود شد. این نتیجه با رسیدن موفق مأموریت یکسان نیست. <a href="evidence/attempt18_landing_diagnosis.json">شاهد و علت ریشه‌ای</a>. اصلاح latch و شرط واقعی توقف نزول در اجرای ۱۹ با تماس زمین و DISARM عادی و ارزیابی مستقل تأیید شد.</p>
<h2>اعتبارسنجی نرم‌افزار</h2><p><a href="evidence/tests_contact_profile_final.log">۱۴۵ آزمون مرتبط با پروفایل فعلی</a> گذشتند. اجرای گسترده‌ترِ تست‌های تاریخی در collection با سه import مربوط به moduleهای حذف‌شدهٔ قدیمی متوقف شد؛ این suite سبز اعلام نمی‌شود. <a href="evidence/tests_native_broad_final.log">جزئیات خطای collection</a>.</p><h2>نشست‌ها</h2><table><thead><tr><th>شواهد نشست</th><th>نتیجه</th><th>چرخهٔ تلاش‌شده</th><th>L7 پذیرفته / مصرف</th><th>backend</th><th>مانع یا خطای ثبت‌شده</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<h2>تصاویر واقعی پنجره‌ها</h2><p>هر لینک manifest به شش تصویر و window ID و session ID و hash و telemetry هنگام capture متصل است. تصویرِ پنجره به‌تنهایی گواهی هدایت کامل نیست. در تصویر پس از DISARM، انقضای lease و inhibited شدن خروجی Digital Pilot طبیعی است؛ این پیام به‌عنوان HOLD تأییدشده معرفی نمی‌شود.</p><ul>{''.join(images)}</ul>
<h2>اجرا و سورس قابل بررسی</h2><p>راهنمای قرارداد: <a href="../layer6_v20261007_1032/docs/NATIVE_REALTIME_VTOL_FA.md">Native realtime VTOL</a>؛ نصب و اجرا: <a href="../SETUP.md">SETUP.md</a>.</p>
<pre>source configs/environment_fixed.sh
$PYTHON -m tddt_tdoc.cli --config configs/tdoc.yaml nav-online \\
  --reuse-existing-offline-knowledge --online-episodes 1 \\
  --online-max-cycles-per-episode-auto</pre>
<p>دستور در شاخهٔ TDOC اجرا می‌شود. پیش از online باید prior سازگار همان build وجود داشته باشد. پس از پذیرش آن reset یا تغییر forcing مجاز نیست. <a href="source_changes.patch">patch سورس</a> و <a href="source_manifest.json">manifest تغییرات</a> برای بازبینی نگه‌داری می‌شوند.</p></html>'''
(A/'FINAL_NATIVE_REPORT_FA.html').write_text(body)
print(state,'attempts',len(rows),'images',len(images))
