import csv,gzip,hashlib,json,sys
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import unquote
sys.path.insert(0,str(Path('TDOC_v20260911_2052/scripts').resolve()))
from online_reports.provenance import snapshot,compare,file_hash,write_json
from tddt_tdoc.config import TDOCConfig
A=Path('integration_online_reports_20261105');base=Path('TDOC_v20260911_2052/reports/online');run=base/'runs/20261105_095929_eac3f73b';out=run/'mission';native=run/'evidence/native';m=json.loads((out/'REPORT_MANIFEST.json').read_text())
assert all(file_hash(out/f['path'])==f['sha256'] for f in m['files'])
assert all(file_hash(v['path'])==v['sha256'] for v in m['source_files'].values())
for name,value in m['report_generator_sources'].items():
 assert file_hash(Path('TDOC_v20260911_2052/scripts/online_reports')/name)==value,name
expected=[json.loads(x) for x in (native/'cycles.jsonl').open()]
with (out/'MISSION_TIMELINE.csv').open() as f:timeline=list(csv.DictReader(f))
with (out/'LAYER4_CANDIDATES.csv').open() as f:candidates=list(csv.DictReader(f))
assert len(timeline)==len(expected)==4458
assert len(candidates)==sum(len(r['L4']['candidates']) for r in expected)
for a,b in zip(timeline,expected):
 assert a['transaction_id']==b['transaction_id']
 for i in range(4):
  assert abs(float(a['l5_pilot_cmd_'+str(i)])-b['L5']['u_cmd'][i])<1e-12
with gzip.open(out/'DECISION_POINT_DETAILS.json.gz','rt') as f:
 assert len(json.load(f))==len(expected)
h=hashlib.sha256()
with gzip.open(out/'RAW_CYCLE_RECORDS.jsonl.gz','rb') as f:
 for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
assert h.hexdigest()==file_hash(native/'cycles.jsonl')
missing=[];external=[]
class Links(HTMLParser):
 def __init__(self,p):super().__init__();self.p=p
 def handle_starttag(self,tag,attrs):
  for k,v in attrs:
   if k in ('src','href') and v and not v.startswith('#'):
    if v.startswith(('http:','https:','//')):external.append((str(self.p),v))
    else:
     q=unquote(v.split('#')[0]);target=self.p.parent/q
     if not target.exists():missing.append((str(self.p),q))
for p in out.glob('*.html'):Links(p).feed(p.read_text())
assert not missing,missing
assert not external,external
for p in (base/'mission').glob('*.html'):Links(p).feed(p.read_text())
assert not missing,missing
cfg=TDOCConfig.from_yaml(Path('TDOC_v20260911_2052/configs/tdoc.yaml').resolve());pres=compare(json.loads((run/'evidence/preservation_before.json').read_text()),snapshot(cfg));assert pres['status']=='PASS'
old=json.loads(Path('integration_realtime_20261104_1855/source_manifest.json').read_text());assert all(file_hash(f['path'])==f['after_sha256'] for f in old['files'])
eq=json.loads((out/'REPORT_EQUIVALENCE.json').read_text());assert len(eq['artifacts'])==28 and eq['all_offline_artifact_names_covered']
q=json.loads((out/'NATIVE_QUALIFICATION.json').read_text());assert q['status']=='PASS'
result={'status':'PASS','native_session_id':q['native_session_id'],'same_model_id':pres['model_id'],'cycles':len(timeline),'candidate_rows':len(candidates),'offline_artifacts_covered':28,'html_reports':len(list(out.glob('*.html'))),'all_report_hashes_verified':True,'control_commands_match_raw_cycles':True,'compressed_raw_archive_byte_exact':True,'all_local_links_work_including_latest_aliases':True,'external_browser_assets':False,'historical_36_changed_files_unchanged':True,'preservation_after_reporting':pres,'actual_goal_error_m':q['actual_goal_error_m_independent']}
write_json(A/'FINAL_VERIFICATION.json',result);print(json.dumps(result,ensure_ascii=False,indent=2))
