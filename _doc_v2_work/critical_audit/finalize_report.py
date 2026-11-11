from pathlib import Path
import hashlib,json,re
R=Path.cwd();W=R/'_doc_v2_work/critical_audit';P=R/'doc_v2/TDDT_PASS_IMPLEMENTATION_GAPS_AND_WEAKNESSES_FA_v1.md'
s=P.read_text()
findings=re.findall(r'^### ([ANPETLVBQ]\d{2}) — (.+)\n\n\*\*نوع/شدت:\*\* ([^\n]+)',s,re.M)
assert len(findings)==len({x[0] for x in findings})==47,len(findings)
index='### ۳.۱. فهرست همهٔ یافته‌ها\n\n| شناسه | موضوع | نوع و شدت |\n| --- | --- | --- |\n'+''.join('| '+i+' | '+title+' | '+kind+' |\n' for i,title,kind in findings)+'\n'
if '### ۳.۱. فهرست همهٔ یافته‌ها' not in s:s=s.replace('## ۴. شکاف‌های ساختاری',index+'## ۴. شکاف‌های ساختاری',1)
metrics=json.loads((W/'derived_metrics.json').read_text());probes=json.loads((W/'isolated_probes.json').read_text())
ref=[
'TDOC_v20260911_2052/src/tddt_tdoc/realtime.py','TDOC_v20260911_2052/src/tddt_tdoc/cli.py','TDOC_v20260911_2052/configs/tdoc.yaml',
'TDOC_v20260911_2052/src/tddt_tdoc/adapters/layer6.py','TDOC_v20260911_2052/src/tddt_tdoc/native_control_binding.py','TDOC_v20260911_2052/src/tddt_tdoc/model_compatibility.py',
'twins_layer2_v20260820_1233/python/twins_fmi/realtime.py','layer3_v20260911_1131/src/layer3/realtime.py','layer4_v20260907_0608/src/layer4/realtime.py','layer5_v20260911_1510/src/layer5/realtime.py','layer7_v20260901_0921/src/tddt_layer7/realtime.py',
'layer6_v20261007_1032/src/layer6/realtime.py','layer6_v20261007_1032/src/layer6/binding.py','layer6_v20261007_1032/src/layer6/timeout_hold.py','layer6_v20261007_1032/configs/native_realtime_vtol.parm','layer6_v20261007_1032/scripts/apply_native_realtime_model.py','layer6_v20261007_1032/FlightStack/ardupilot/libraries/GCS_MAVLink/GCS_Common.cpp',
'integration_realtime_20261104_1855/validate_flight.py','integration_realtime_20261104_1855/validate_realtime_trace.py',
'integration_realtime_20261104_1855/attempt20_full_route/MISSION_RESULT.json','integration_realtime_20261104_1855/attempt20_full_route/FINAL_QUALIFICATION.json','integration_realtime_20261104_1855/attempt20_full_route/independent_validation.json','integration_realtime_20261104_1855/attempt20_full_route/preservation_final.json']
rows=[{'path':p,'sha256':hashlib.sha256((R/p).read_bytes()).hexdigest(),'bytes':(R/p).stat().st_size} for p in ref]
append='\n### ۱۶.۴ آمار کاملِ دوباره‌استخراج‌شده از لاگ\n\nنام فیلدهای JSON برای تطبیق ماشینی حفظ شده‌اند. درصد L7، نسبت cycleهای دارای بازخورد تازه است و «نرخ موفقیت پرواز» نیست. repeated field به معنی acquisition مستقل تازه نیست؛ تکرار مقدار نیز به‌تنهایی خرابی را اثبات نمی‌کند.\n\n```json\n'+json.dumps(metrics,ensure_ascii=False,indent=2)+'\n```\n'
append+='\n### ۱۶.۵ خروجی کامل بررسی‌های محلیِ جدا از پرواز\n\nبررسی‌ها فقط روی کد اصلی در فضای داده/کنترل جریان جدا انجام شدند؛ output مجوز control یا PASS پرواز جدید نیست.\n\n```json\n'+json.dumps(probes,ensure_ascii=False,indent=2)+'\n```\n'
append+='\n### ۱۶.۶ هویت منابع ممیزی\n\nمسیرها از ریشهٔ باندل‌اند. برای نمونه، `tddt_tdoc/realtime.py` در ارجاع‌های کوتاه، همان مسیر کامل TDOC در جدول است و `twins_fmi/realtime.py` به بستهٔ twins_layer2 اشاره دارد.\n\n| مسیر | بایت | SHA‑256 |\n| --- | --- | --- |\n'
append+=''.join('| `'+x['path']+'` | '+str(x['bytes'])+' | `'+x['sha256']+'` |\n' for x in rows)
if '### ۱۶.۴ آمار کاملِ' not in s:s+=append
P.write_text(s)
assert s.count('```')%2==0
assert metrics['production_source_mismatches']==[]
assert probes['late_exception_handler_probe']['result']['status']=='PASS' and probes['late_exception_handler_probe']['result'].get('error')
assert probes['negative_height_Layer4_contact_feasible'] is True
j={'status':'COMPLETE_LOCAL','findings':len(findings),'P0':[x[0] for x in findings if 'P0' in x[2]],'P1':[x[0] for x in findings if 'P1' in x[2]],'P2':[x[0] for x in findings if 'P2' in x[2]],'production_sources_match_PASS':True,'new_native_flight':False,'read_only_analysis_and_isolated_probes_only':True,'report':str(P),'report_sha256':hashlib.sha256(P.read_bytes()).hexdigest(),'report_bytes':P.stat().st_size,'source_files':rows}
(W/'report_validation.json').write_text(json.dumps(j,ensure_ascii=False,indent=2));print({k:v for k,v in j.items() if k!='source_files'})
