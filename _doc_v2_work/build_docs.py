"""Build Persian Markdown documentation from verified Drive originals and PASS evidence."""
from pathlib import Path
import ast,hashlib,json,re,os
from urllib.parse import quote

W=Path(__file__).resolve().parent;R=W.parent;OUT=R/'doc_v2';A=R/'integration_realtime_20261104_1855';D=A/'attempt20_full_route'
entries=json.loads((W/'drive_source_inventory.json').read_text());chapters={p.stem:p.read_text() for p in (W/'chapters').glob('*.md')}
def sha(data):return hashlib.sha256(data).hexdigest()
def write(rel,text):
 p=OUT/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text.rstrip()+'\n');return p
def link(src,dst,label):return '['+label+']('+quote(os.path.relpath(OUT/dst,(OUT/src).parent).replace(os.sep,'/'),safe='/')+')'
def table(headers,rows):return '| '+' | '.join(headers)+' |\n| '+' | '.join(['---']*len(headers))+' |\n'+''.join('| '+' | '.join(str(x).replace('|','\\|').replace('\n',' ') for x in row)+' |\n' for row in rows)
def fence(data,kind='json'):return '\n````'+kind+'\n'+data.rstrip()+'\n````\n'
def jsonmd(rel,title,path,intro):
 obj=json.loads(path.read_text());return write(rel,'# '+title+'\n\n'+intro+'\n\nمسیر مرجع در باندل: `'+str(path.relative_to(R))+'`؛ SHA‑256 فایل مرجع: `'+sha(path.read_bytes())+'`.\n'+fence(json.dumps(obj,ensure_ascii=False,indent=2)))
def newpath(e):
 p=Path(e['rel']);n=p.name
 if n.startswith('Diagramme'):return 'TDDT_Dynamic_NTwin_v6/Diagram_L_EPAC/Diagram_L_EPAC_FA_doc_v2.md'
 if re.search(r'_v\d+(?=\.md$)',n):n=re.sub(r'_v(\d+)(?=\.md$)',lambda m:'_v'+str(int(m[1])+1),n)
 else:n=n[:-3]+('_doc_v2.md' if '_FA' in n else '_FA_doc_v2.md')
 return str(p.parent/n)
mapping={e['rel']:newpath(e) for e in entries}
def topics(e):
 n=e['title'];p=e['rel']
 if n=='sensors_v1.md':return ['sensors','mesa']
 if 'Layer5_output' in n:return ['mpc','api','execution']
 if 'Layer7_input' in n:return ['feedback','api','sensors']
 if 'Diagramme' in n or 'L-EPAC' in n:return ['network','feedback','compliance']
 if 'KnowledgeGap' in n:return ['scope','evidence','compliance']
 if 'Layer1_' in n:return ['network','sensors','physics_climate']
 if 'Layer2_' in n:return ['sensors','mesa','physics_climate']
 if 'Layer3_' in n:return ['network','physics_climate','units']
 if 'Layer4_' in n or 'Formalism_layer4' in n:return ['mpc','physics_climate','execution']
 if 'Layer5_' in n or 'Formalism_layer5' in n:return ['mpc','execution','lifecycle']
 if 'Layer7_' in n or 'Formalism_layer7' in n:return ['feedback','sensors','execution']
 if 'TDOC_OFFLINE' in n:return ['knowledge','execution']
 if 'TDOC_TERMINAL' in n:return ['lifecycle','mpc']
 if 'TDOC_ISO' in n:return ['units','sensors','execution']
 if 'TDOC_RTMO' in n or 'TDOC_details' in n:return ['network','lifecycle','execution','knowledge']
 if 'MESA_NAV' in n:return ['mesa','sensors','units']
 if 'WingMPET' in n:return ['physics_climate','mpc','build']
 if 'MicroMet' in n or 'micromet' in n:return ['physics_climate','sensors','knowledge']
 if 'SARG' in n:return ['knowledge','lifecycle','mpc']
 if 'CRP' in n or 'classification_crp' in n:return ['knowledge','physics_climate','units']
 if 'TEP' in n:return ['knowledge','compliance']
 if 'rollout' in n:return ['mpc','execution','feedback']
 if 'Layer5_output' in n:return ['mpc','api','execution']
 if 'Layer7_input' in n:return ['feedback','api','sensors']
 if 'GUIDE_Layer6Client' in n:return ['api','execution','feedback']
 if 'GUIDE_USING' in n:return ['lifecycle','api','execution']
 if 'README_layer6' in n:return ['setup','build','execution','api']
 if 'COMPLIANCE' in n:return ['compliance','execution','lifecycle']
 if 'Key_Drivers' in n:return ['network','knowledge','evidence']
 if 'Layer6' in n or 'Layer6/' in p:return ['execution','api','lifecycle','feedback']
 return ['network','scope','knowledge','evidence']

titles={'sensors_v1.md':'قرارداد جامع حسگرهای مجاز و نرخ‌های عملیاتی',
 'Layer5_output_v1.md':'قرارداد خروجی لایهٔ ۵ و نگاشت فرمان خلبانی',
 'Layer7_input_v1.md':'قرارداد ورودی لایهٔ ۷ و شاهد واقعی اجرا',
 'GUIDE_USING_v1.md':'راهنمای بهره‌برداری دستی و خودکار فونیکس',
 'GUIDE_Layer6Client_FA_v2.md':'راهنمای جامع رابط Layer6Client و اتصال TDDT',
 'README_layer6_v1.md':'راهنمای جامع نصب و اجرای لایهٔ ۶',
 'Layer6_formalism.md':'فرمالیسم لایهٔ ۶ و تحقق اجرایی Native',
 'LAYER6_FORMALISM_COMPLIANCE_v1.md':'ماتریس انطباق لایهٔ ۶ و حدود قبولی',
 'Key_Drivers_Effective_Leadership_v1.md':'عوامل تعیین‌کنندهٔ هدایت مؤثر و شواهد تحقق آن',
 'Formalism_layer4.md':'فرمالیسم لایهٔ ۴ و محدودهٔ پیش‌بینی اجرایی',
 'Formalism_layer4_v2.md':'تحقق محاسباتی لایهٔ ۴ و تفاوت مسیرهای گواهی و نمونهٔ Native',
 'Formalism_layer5_v5.md':'فرمالیسم لایهٔ ۵ و کنترل پیش‌بین اجرایی فونیکس',
 'Formalism_layer7_v2.md':'فرمالیسم لایهٔ ۷ و تطبیق prediction با اجرای زمان‌مند',
 'rollout_time_issue.md':'زمان rollout و قرارداد زمان در اجرای Native',
 'Diagramme technique du formalisme L-EPAC.md':'تعاریف کامل متغیرهای نمودار L‑EPAC و جایگاه آن در نسخهٔ جدید'}
coverage=[]
for e in entries:
 rel=mapping[e['rel']];old=(W/'drive_sources'/e['rel']).read_text();top=topics(e)
 oldhead=next((l.lstrip('# ').replace('**','') for l in old.splitlines() if l.startswith('#') and re.search('[\u0600-\u06ff]',l)), 'سند تخصصی '+e['title'])
 title=titles.get(e['title'],oldhead)
 intro='# '+title+' — ویرایش doc_v2\n\nتاریخ تدوین: ۲۰۲۶-۱۱-۰۵؛ زبان: فارسی؛ خانوادهٔ معماری: TDDT؛ نسخهٔ اجرایی مرجع: Native PASS، نشست ۲۰.\n\n'
 intro+='این فایل ویرایش جدیدِ '+ '['+e['title']+']('+e['url']+') است. متن مرجع قبلی در انتهای سند کامل حفظ شده؛ بخش اجرایی زیر تغییرات مرتبط با همین موضوع را مشخص می‌کند. نمادها، APIها، کد و نام منابع برای تطبیق فنی به صورت اصلی باقی می‌مانند.\n\n'
 intro+=link(rel,'README_FA.md','راهنمای مجموعه')+' · '+link(rel,'MIGRATION_MATRIX_FA.md','ماتریس تغییر قراردادها')+' · '+link(rel,'Formalisms_specialized_architectures/NATIVE_REALTIME_FORMALISM_FA_v1.md','فرمالیسم اجرایی یکپارچه')+' · '+link(rel,'Release_Evidence/PASS_ACCEPTANCE_FA_v2.md','شواهد مستقل قبولی')+'\n\n'
 if 'scope' not in top:intro+=chapters['scope']+'\n'
 intro+='\n'.join(chapters[k] for k in top)
 # Preserve the previous technical body; normalize obsolete cross-file names.
 for src,dst in sorted(mapping.items(),key=lambda x:-len(Path(x[0]).name)):
  old=old.replace(Path(src).name,Path(dst).name)
 old=re.sub(r':chatgpt-content-reference\{index="(\d+)"\}',lambda m:'[ارجاع تاریخی '+m[1]+'؛ نشانی در متن قبلی موجود نیست]',old)
 old=re.sub(r'(?m)^(#{1,6}) ([^\n]+)$',lambda m:m[0] if re.search('[\u0600-\u06ff]',m[2]) else m[1]+' بخش مرجع فنی: '+m[2],old)
 intro+='\n# بخش مرجع طراحی پیشین — متن کامل با پیوندهای نام‌گذاری‌شدهٔ جدید\n\n> این بخش، سابقهٔ کامل مبانی، روابط، مثال‌ها و API پیشین را نگه می‌دارد. گزاره‌های اجرایی ناسازگار آن برای پروفایل Native با قرارداد ابتدای همین ویرایش جایگزین می‌شوند. منابع علمی و مشخصات سخت‌افزاری قبلی در فرایند PASS مستقلاً بازاعتبارسنجی نشده‌اند. ارجاع‌های تاریخی بدون URL، منبع تازه محسوب نمی‌شوند.\n\n'+old
 write(rel,intro)
 coverage.append({'old':e['rel'],'new':rel,'source_id':e['id'],'source_url':e['url'],'source_sha256':sha((W/'drive_sources'/e['rel']).read_bytes()),'original_body_preserved':True,'topics':top})

deep={
 'Formalisms_specialized_architectures/NATIVE_REALTIME_FORMALISM_FA_v1.md':('فرمالیسم اجرایی یکپارچهٔ نسخهٔ Native منجر به PASS',['scope','network','sensors','mesa','physics_climate','mpc','execution','feedback','lifecycle','units','compliance']),
 'Sensors/SENSOR_CONTRACT_FA_v2.md':('قرارداد اجرایی حسگر، نرخ‌ها، نویز و مسیرهای ممنوع',['scope','sensors','mesa','units']),
 'Layer6/NATIVE_RUNTIME_FA_v2.md':('اجرای Native و رابط کامل میان تصمیم و شاهد فیزیکی',['scope','execution','api','lifecycle','feedback','physics_climate']),
 'Bundle_Preparation_Execution/SETUP_FA_v2.md':('آماده‌سازی و اجرای مرحله‌به‌مرحلهٔ باندل به‌روزشده',['scope','setup','build','knowledge','execution']),
 'Bundle_Preparation_Execution/BUILD_RESET_FA_v2.md':('بازکامپایل، نصب استاندارد و ماندگاری پس از reset',['scope','build','physics_climate']),
 'Bundle_Preparation_Execution/OFFLINE_HANDOFF_FA_v2.md':('آموزش آفلاین، prior و تحویل نسخه‌بندی‌شده',['scope','knowledge','compliance']),
 'Bundle_Preparation_Execution/ONLINE_RUNBOOK_FA_v2.md':('راهنمای اجرای Online، کنترل پنجره‌ها و پایان مأموریت',['scope','setup','execution','lifecycle','feedback','evidence']),
 'Bundle_Preparation_Execution/TROUBLESHOOTING_FA_v2.md':('راهنمای تشخیص ناسازگاری و جلوگیری از قبولی کاذب',['scope','troubleshooting']),
 'Release_Evidence/PASS_ACCEPTANCE_FA_v2.md':('گزارش پذیرش و شواهد مستقل مأموریت Native',['scope','evidence']),
}
for rel,(title,keys) in deep.items():write(rel,'# '+title+'\n\n'+link(rel,'README_FA.md','فهرست مجموعه')+'\n\n'+'\n'.join(chapters[k] for k in keys))

diagram='''# نمودار فارسی L‑EPAC و تفاوت آن با حلقهٔ اجرا

این سند، تصویر PNG پیشین را به نمودار متنی Markdown/Mermaid و شرح روابط تبدیل می‌کند. تصویر اصلی تغییر نکرده و در پوشهٔ قبلی باقی می‌ماند. این نمودار «طراحی نظری» است؛ فعال‌شدن همهٔ شاخص‌های آن در نسخهٔ PASS ادعا نمی‌شود.

```mermaid
flowchart LR
 DI[لایه ۲: دوقلوی مبدأ و residual محلی] --> DIR[لایه ۴: پیش‌بینی مسیر مستقیم]
 DI --> DJ[دوقلوی میانی اول]
 DJ --> DK[دوقلوی میانی بعدی]
 DK --> HID[لایه ۴: پیش‌بینی مسیر پنهان]
 DIR --> LCI[اختلاف مسیر پنهان و مستقیم]
 HID --> LCI
 LCI --> G[بهرهٔ حلقه، بهرهٔ بالا و نگهبان small-gain]
 G --> RISK[ارزیابی ریسک حلقه]
 RISK --> EDGE[انتخاب رابطهٔ مناسب برای مهار]
 EDGE --> C[پیشنهاد تضعیف coupling یا جداسازی]
 C --> EX[لایه ۵/۶: تصمیم و اجرا]
 EX --> OBS[لایه ۱/۶: مشاهدهٔ فیزیکی]
 OBS --> L7[لایه ۷: اختلاف مشاهده و پیش‌بینی مستقیم]
 L7 --> CAL[کالیبراسیون sensitivity با شاهد معتبر]
 CAL --> DJ
```

مبدأ `D_i` دارای residual محلی `e_i^loc` و excitation محافظه‌کارانه `q_i+` است. مسیر مستقیم `z_dir` با مسیر پنهان `z_hid` پس از تأخیر حلقه `tau_c` مقایسه می‌شود. delayهای رابطه، sensitivity، coupling نرمال‌شده و gain مؤثر، ماتریس انتشار `P_k` را می‌سازند. شاخص LCI اختلاف دو مسیر، Gamma بهرهٔ حلقه، Gamma+ کران محافظه‌کارانه و eta نگهبان سبک‌اند. risk برابر R_c(H)، رابطهٔ منتخب b*، ضریب تضعیف alpha و K_cand پیشنهاد مهار را تعیین می‌کنند. پس از اجرا، LCI_obs و delta_LCI مبنای به‌روزرسانی sensitivity در لایهٔ ۷ هستند.

شماره‌های تصویر پیشین به روابط متن نظری مربوط‌اند: cycle رابطهٔ ۲؛ residual/excitation روابط ۳/۴؛ delay و حساسیت ۵ تا ۸؛ coupling/gain روابط ۹/۱۰؛ انتشار ۱۱/۱۲؛ مسیر مستقیم ۱۳؛ مسیر پنهان ۱۴/۱۵؛ LCI پیش‌بینی ۱۶؛ LCI مشاهده ۱۷؛ gain و amplification روابط ۱۸ تا ۲۲؛ نگهبان ۲۴؛ ریسک ۲۵؛ رابطهٔ منتخب ۲۶؛ مهار ۲۷/۲۸؛ مد ۲۹؛ residual شاخص ۳۰ و adaptation رابطهٔ ۳۱. این شماره‌ها شمارهٔ خطوط سورس نیستند.

در runtime نسخهٔ PASS، شاهد حلقه با hash snapshot، prediction زمانی، receipt واقعی و residual حسگرمحور و model update ثبت می‌شود. این شاهد با پیاده‌سازی کامل ماتریس انتشار/تمام سیاست‌های مهار نظری یکسان نیست. متغیر `z_obs` در مسیر کنترل فقط باید از حسگرهای مجاز بازسازی شود؛ truth شبیه‌ساز به مشاهدهٔ ناوبری تبدیل نمی‌شود.

تصویر پیشین: [نمودار اصلی](https://drive.google.com/file/d/1I4YeLk72UW1roBLkKXp6wNoirjxclzFx/view?usp=drivesdk).
'''
write('TDDT_Dynamic_NTwin_v6/Diagram_L_EPAC/L_EPAC_DIAGRAM_FA_v2.md',diagram)

# Exact evidence objects remain machine-readable inside Markdown.
objects=[('FINAL_QUALIFICATION_FA_v2.md','دامنه و نتیجهٔ نهایی',D/'FINAL_QUALIFICATION.json'),
 ('INDEPENDENT_VALIDATION_FA_v2.md','ارزیابی مستقل فیزیکی',D/'independent_validation.json'),
 ('CROSS_LAYER_TRACE_FA_v2.md','اعتبارسنجی trace تمام چرخه‌ها',D/'cross_layer_trace_validation.json'),
 ('PRESERVATION_FA_v2.md','ثبات source، prior، forcing و binary',D/'preservation_final.json'),
 ('RESET_BUILD_PROOF_FA_v2.md','اثبات reset و بازسازی FMU',A/'evidence/standard_reset_build_proof.json'),
 ('NATIVE_BUILD_PROOF_FA_v2.md','اثبات build استاندارد Native',A/'evidence/native_standard_build_proof.json')]
for n,title,p in objects:jsonmd('Release_Evidence/'+n,title,p,'این داده، خروجی واقعی بایگانی مبناست؛ نام فیلدها برای امکان تطبیق ماشینی حفظ شده‌اند. توضیح فارسی معیارها در گزارش پذیرش آمده است.')

source_manifest=json.loads((A/'source_manifest.json').read_text());pres=json.loads((D/'preservation_final.json').read_text());model=json.loads((D/'realtime_model_before.json').read_text())
for f in source_manifest['files']:assert sha((R/f['path']).read_bytes())==f['after_sha256'],f['path']
hashes='# نسخه‌ها و hashهای کامل build آزموده‌شده\n\n'+chapters['build']+'\n'+table(['artifact','SHA‑256','bytes'],[(k,v['sha256'],v['bytes']) for k,v in pres['binary_hashes'].items()])
hashes+='\n## manifest کامل وابستگی مدل\n\nاین manifest مربوط به پیش از نشست ۲۰ است؛ بسته‌های جدید مستندات جزو build پروازی نیستند.\n'+fence(json.dumps(model,ensure_ascii=False,indent=2))
write('Release_Evidence/BUILD_HASHES_FA_v2.md',hashes)

changes='# تغییرات سورس و علت رفع ناسازگاری‌ها\n\n'+chapters['troubleshooting']+'\n## فهرست کامل فایل‌های تغییرکرده\n\n'
changes+=table(['فایل','نوع','hash پیشین','hash نهایی','خروجی تولیدشده'],[(f['path'],'افزوده' if f['kind']=='added' else 'اصلاح‌شده',f['before_sha256'] or 'نداشت',f['after_sha256'],'بله' if f['generated_output'] else 'خیر') for f in source_manifest['files']])
changes+='\n## محدودیت baseline patch\n\nبیشتر baselineها کپی آغاز کار در پوشهٔ before هستند. baseline installer با حذف insertion شناخته‌شدهٔ اولیه بازسازی شده است؛ baseline فایل تولیدشدهٔ محیط، resource مقید R4 است. بنابراین patch را بدون تطبیق نسخه و hash به نصب دیگر اعمال نکنید. فقط C++ اصلی WingMPET تغییر معنایی کامپایل‌شونده داشته است؛ build واقعی و reset آن جدا ثبت شده‌اند.\n'
write('Release_Evidence/SOURCE_CHANGELOG_FA_v2.md',changes)
write('Release_Evidence/SOURCE_PATCH_FA_v2.md','# patch کامل تغییرات منجر به نسخهٔ PASS\n\nاین متن شامل تغییرات سورس برای بازبینی فنی است؛ binaryها در آن جاسازی نشده‌اند. مبنای patch و حدود آن در سند فهرست تغییرات توضیح داده شده است. این سند دستور اجرای خودکار patch نیست.\n\nSHA‑256 patch اصلی: `'+sha((A/'source_changes.patch').read_bytes())+'`.\n'+fence((A/'source_changes.patch').read_text(),'diff'))

# Full core source makes the newly specialized formalism inspectable offline.
core=[
 'TDOC_v20260911_2052/src/tddt_tdoc/realtime.py',
 'twins_layer2_v20260820_1233/python/twins_fmi/realtime.py',
 'layer3_v20260911_1131/src/layer3/realtime.py',
 'layer4_v20260907_0608/src/layer4/realtime.py',
 'layer5_v20260911_1510/src/layer5/realtime.py',
 'layer6_v20261007_1032/src/layer6/realtime.py',
 'layer7_v20260901_0921/src/tddt_layer7/realtime.py',
 'layer6_v20261007_1032/scripts/apply_native_realtime_model.py',
 'layer6_v20261007_1032/scripts/apply_native_realtime_environment.py']
code='# مرجع کامل سورس هستهٔ پروفایل Native\n\nتوضیح الگوریتم‌ها در فرمالیسم اجرایی فارسی آمده است. کد و identifierها برای جلوگیری از تغییر معنا، دقیقاً به زبان برنامه‌نویسی اصلی حفظ می‌شوند. سورس انگلیسی، متن راهنمای فارسی را جایگزین نمی‌کند. هر بخش از همین نسخهٔ PASS خوانده شده و hash دارد. سایر تغییرات support در patch کامل موجودند.\n'
for name in core:code+='\n## فایل `'+name+'`\n\nSHA‑256: `'+sha((R/name).read_bytes())+'`.\n'+fence((R/name).read_text(),'python')
write('Release_Evidence/RUNTIME_SOURCES_FA_v2.md',code)

config='# پیکربندی کامل پروفایل آزموده‌شده\n\nمقادیر باید با سورس و binary همان نسخه تطبیق داده شوند؛ کپی بخشی از این جدول به firmware نامعلوم، بازتولید آزمون نیست. تغییر این مقادیر model identity را تغییر می‌دهد.\n\n'+chapters['units']
for name,kind in [('layer6_v20261007_1032/configs/native_realtime_vtol.parm','text'),('TDOC_v20260911_2052/configs/tdoc.yaml','yaml')]:
 config+='\n## فایل `'+name+'`\n\nSHA‑256: `'+sha((R/name).read_bytes())+'`.\n'+fence((R/name).read_text(),kind)
write('Layer6/NATIVE_CONFIGURATION_FA_v2.md',config)

# Actual, complete cross-layer packet examples from a correlated transaction.
example=None;next_example=None
for line in (D/'cycles.jsonl').open():
 row=json.loads(line)
 if example is not None and row['L2']['navigation']['feedback_consumed']==example['L7']['route_hash']:next_example=row;break
 if row['L7'].get('ok'):example=row
assert example and next_example
sample='# نمونهٔ واقعی schemaها و بازخورد چرخهٔ بعد\n\nاین مثال از لاگ واقعی نشست PASS استخراج شده است؛ timestamp و hashها تغییر نکرده‌اند. packetها برای مطالعه‌اند و authority بازپخش در Online ندارند. هیچ فیلد observation از `external_validation_only` به ورودی MESA جابه‌جا نشده است.\n'
sample+='\n## چرخهٔ دارای شاهد اجرا و بازخورد معتبر\n'+fence(json.dumps(example,ensure_ascii=False,indent=2))
sample+='\n## مصرف همان route در چرخهٔ بعد\n'+fence(json.dumps({'transaction_id':next_example['transaction_id'],'navigation':next_example['L2']['navigation'],'WingMPET_model':next_example['L2']['wing']['reduced_inner_loop']},ensure_ascii=False,indent=2))
write('Release_Evidence/SCHEMA_EXAMPLES_FA_v2.md',sample)

tests='# آزمون‌های نسخهٔ نهایی و حدود پوشش\n\n'+chapters['compliance']+'\n## خروجی واقعی آزمون مرتبط\n'+fence((A/'evidence/tests_contact_profile_final.log').read_text(),'text')
tests+='\n## خطاهای collection در suite تاریخی\n\nاین خطاها حذف یا PASS نامیده نشده‌اند.\n'+fence((A/'evidence/tests_native_broad_final.log').read_text(),'text')
tests+='\n## اجرای دوبارهٔ چک‌های مستقل روی بایگانی\n\nاز ریشهٔ باندل، پس از بارگذاری محیط:\n'+fence('source TDOC_v20260911_2052/configs/environment_fixed.sh\n"$PYTHON" integration_realtime_20261104_1855/validate_flight.py integration_realtime_20261104_1855/attempt20_full_route\n"$PYTHON" integration_realtime_20261104_1855/validate_realtime_trace.py integration_realtime_20261104_1855/attempt20_full_route','bash')
tests+='\nاین دو دستور read-only از نظر کنترل پهپادند، ولی خروجی ارزیابی داخل پوشهٔ بایگانی می‌نویسند. replay شبیه‌سازی یا پرواز جدید راه نمی‌اندازند. برای نگه‌داری دقیق بایگانی، ابتدا روی کپی خروجی کار کنید.\n'
write('Release_Evidence/TESTS_FA_v2.md',tests)

windows='# شواهد واقعی شش پنجرهٔ نشست نهایی\n\nتصاویر اصلی در باندل مرجع نگه‌داری می‌شوند؛ این مستند نام، hash و telemetry واقعی هر capture را برای تطبیق می‌آورد. فایل تصویری یا دادهٔ تازه تولید نشده و مجموعهٔ منتشرشده فقط Markdown است.\n'
for p in sorted((A/'evidence').glob('*d3893ea0/manifest.json')):
 data=json.loads(p.read_text());assert data['all_six_captured']
 windows+='\n## capture `'+p.parent.name+'`\n\nمسیر مرجع: `'+str(p.relative_to(R))+'`.\n'+fence(json.dumps(data,ensure_ascii=False,indent=2))
write('Release_Evidence/NATIVE_WINDOWS_FA_v2.md',windows)

migration='# ماتریس تغییر قراردادها در doc_v2\n\n'+chapters['scope']+'\n'+chapters['compliance']+'\n## قواعد خواندن نسخهٔ جدید\n\nرابطهٔ نظریِ حفظ‌شده در بخش مرجع قبلی، به خودی خود الزام اجراشده یا گواهی PASS نیست. عبارت «تمام فازها»، «تمام anchorها»، «full FF verified» یا «state مشاهده‌شده» باید به scope همان مسیر محدود شود. در مسیر فعلی، مشاهدات کنترلی از حسگر مجاز، predicton از مدل جاری و تأیید فیزیکی از مسیر مستقل‌اند. چرخهٔ control با چرخهٔ بررسی حقیقت یکی نمی‌شود.\n'
write('MIGRATION_MATRIX_FA.md',migration)
coverage_text='# فهرست تطبیق تمام مستندات پیشین با ویرایش جدید\n\n۳۹ فایل Markdown مستقیم از Drive خوانده و با نسخهٔ محلی تطبیق داده شدند؛ همگی، با صرف‌نظر از newline پایانی، برابر بودند. هیچ سند Markdown از پوشهٔ مبنا کنار گذاشته نشده است. یک PNG نمودار نیز با سند نمودار متنی فارسی پوشش داده شده است.\n\n'
coverage_text+=table(['سند پیشین','ویرایش جدید','منبع Drive','hash متن دریافتی'],[(c['old'],link('SOURCE_COVERAGE_FA.md',c['new'],'ویرایش کامل'),f'[منبع]({c["source_url"]})',c['source_sha256']) for c in coverage])
coverage_text+='\nتصویر قدیمی L‑EPAC → '+link('SOURCE_COVERAGE_FA.md','TDDT_Dynamic_NTwin_v6/Diagram_L_EPAC/L_EPAC_DIAGRAM_FA_v2.md','نسخهٔ نموداری Markdown فارسی')+'. توضیح متغیرهای همان تصویر نیز در ویرایش کامل سند متنیِ پیشین حفظ شده است.\n'
write('SOURCE_COVERAGE_FA.md',coverage_text)
write('CHANGELOG_FA.md','# تغییرات مجموعهٔ مستندات doc_v2\n\nتاریخ تدوین ۲۰۲۶-۱۱-۰۵، تورنتو. نسخهٔ اجرایی مستندشده همان PASS مورخ ۲۰۲۶-۱۱-۰۴ است؛ تدوین این اسناد نتیجهٔ پرواز تازه یا تغییر باندل نیست.\n\n'+chapters['network']+'\n'+chapters['troubleshooting']+'\n'+chapters['evidence'])

readme='# مجموعهٔ جامع مستندات فارسی doc_v2\n\nاین مجموعه ویرایش کامل مستندات باندل `TDDT_integrated_fix_v4` پس از آزمون Native موفق است. تمام فایل‌ها Markdown UTF‑8 هستند؛ متن اصلی فارسی است و نام‌های فنی، کد، فرمول‌ها، bibliographic identifierها و کلیدهای schema برای تطبیق بدون ترجمهٔ مخرب حفظ شده‌اند.\n\n'
readme+=chapters['scope']+'\n'+chapters['evidence']
readme+='\n## ترتیب مطالعه\n\n'
order=[('Bundle_Preparation_Execution/SETUP_FA_v2.md','آماده‌سازی و اجرای کامل'),('Formalisms_specialized_architectures/NATIVE_REALTIME_FORMALISM_FA_v1.md','فرمالیسم اجرایی دقیق لایه‌های ۲ تا ۷'),('Sensors/SENSOR_CONTRACT_FA_v2.md','حسگرها، نرخ‌ها و ممنوعیت truth aiding'),('Layer6/NATIVE_RUNTIME_FA_v2.md','رابط لایهٔ ۶، GUI، Native و receipt'),('Bundle_Preparation_Execution/OFFLINE_HANDOFF_FA_v2.md','دانش جزئی/کامل و binding'),('Release_Evidence/BUILD_HASHES_FA_v2.md','نسخه و hash و بازکامپایل'),('Release_Evidence/SCHEMA_EXAMPLES_FA_v2.md','packet واقعی و چرخهٔ بعد'),('Release_Evidence/NATIVE_WINDOWS_FA_v2.md','شواهد شش پنجره'),('Release_Evidence/SOURCE_CHANGELOG_FA_v2.md','فهرست همهٔ تغییرات سورس'),('Release_Evidence/RUNTIME_SOURCES_FA_v2.md','سورس کامل هستهٔ runtime'),('SOURCE_COVERAGE_FA.md','تطبیق تک‌تک مستندات قدیمی'),('MIGRATION_MATRIX_FA.md','حدود انطباق و تقدم قرارداد جدید')]
readme+='\n'.join(str(i+1)+'. '+link('README_FA.md',p,title) for i,(p,title) in enumerate(order))+'\n'
readme+='\n## فهرست تمام فایل‌ها\n\n'
readme+='\n'.join('- '+link('README_FA.md',str(p.relative_to(OUT)),str(p.relative_to(OUT))) for p in sorted(OUT.rglob('*.md')))+'\n'
readme+='\n## منبع و پایداری شواهد\n\n[پوشهٔ مستندات پیشین](https://drive.google.com/drive/folders/1iRAPpwnZK7R7VqoFWBX7WhhtPjkKiwmP) بدون تغییر باقی می‌ماند. مرجع کامل گزارش اجرایی در باندل `integration_realtime_20261104_1855/FINAL_NATIVE_REPORT_FA.html` است. raw logها، تصاویر و binaryها در باندل‌اند؛ این مجموعه مستندات جای فایل‌های اجرایی را نمی‌گیرد. لینک‌های نسبی برای ساختار پوشهٔ دانلودشده طراحی شده‌اند؛ برای پیمایش در Drive از فهرست انتشار پیونددار استفاده شود.\n'
write('README_FA.md',readme)
(W/'coverage.json').write_text(json.dumps(coverage,ensure_ascii=False,indent=2))
files=[{'path':str(p.relative_to(OUT)),'sha256':sha(p.read_bytes()),'bytes':p.stat().st_size} for p in sorted(OUT.rglob('*.md'))]
(W/'publication_files.json').write_text(json.dumps(files,ensure_ascii=False,indent=2));print(json.dumps({'markdown_files':len(files),'bytes':sum(f['bytes'] for f in files),'original_markdown_covered':len(coverage),'output':str(OUT)},ensure_ascii=False))
