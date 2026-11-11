from pathlib import Path
import difflib,hashlib,json
A=Path(__file__).resolve().parent;R=A.parent;before=A/'before';baseline={}
exts={'.py','.yaml','.yml','.cpp','.h','.sh','.md','.xml','.parm','.json','.toml'}
for p in before.rglob('*'):
 if p.is_file() and p.suffix in exts:baseline[p.relative_to(before)]=p.read_bytes()
new=['twins_layer2_v20260820_1233/python/twins_fmi/realtime.py','layer3_v20260911_1131/src/layer3/realtime.py','layer4_v20260907_0608/src/layer4/realtime.py','layer5_v20260911_1510/src/layer5/realtime.py','layer7_v20260901_0921/src/tddt_layer7/realtime.py','layer6_v20261007_1032/src/layer6/realtime.py','TDOC_v20260911_2052/src/tddt_tdoc/realtime.py','layer6_v20261007_1032/scripts/apply_native_realtime_model.py','layer6_v20261007_1032/scripts/apply_native_realtime_environment.py','layer6_v20261007_1032/configs/native_realtime_vtol.parm','layer6_v20261007_1032/configs/phoenix_native_base.xml','layer6_v20261007_1032/docs/NATIVE_REALTIME_VTOL_FA.md','TDOC_v20260911_2052/tests/test_realtime_sensor_contract.py','TDOC_v20260911_2052/tests/test_native_forward_flow.py']
for rel in new:baseline.setdefault(Path(rel),b'')
rel=Path('layer6_v20261007_1032/FlightStack/phoenix_environment_adapter_v2/phoenix_env_adapter/bound.py')
baseline.setdefault(rel,(R/'layer6_v20261007_1032/resources/r4_02_v1/phoenix_env_adapter/bound.py').read_bytes())
rows=[];patch=[]
for rel,old in sorted(baseline.items()):
 p=R/rel
 if not p.is_file():continue
 data=p.read_bytes()
 if data==old:continue
 try:lines=list(difflib.unified_diff(old.decode().splitlines(True),data.decode().splitlines(True),fromfile='a/'+rel.as_posix(),tofile='b/'+rel.as_posix()))
 except UnicodeDecodeError:continue
 patch.extend(lines);rows.append({'path':rel.as_posix(),'kind':'modified' if old else 'added','before_sha256':hashlib.sha256(old).hexdigest() if old else None,'after_sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),'generated_output':('FlightStack' in rel.parts)})
(A/'source_changes.patch').write_text(''.join(patch))
(A/'source_manifest.json').write_text(json.dumps({'schema':'TDDT-native-prototype-source-changes-v1','files':rows,'baseline_notes':['Most baselines are task-start source copies under before/.','Installer baseline reconstructs its original content by removing the one known initial overlay insertion.','Environment generated baseline is the hash-pinned R4 source resource, also present in the early captured model manifest.'],'compiled_source_changes':['twins_layer2_v20260820_1233/src/wingmpet/model.cpp'],'compiled_output_evidence':['evidence/standard_reset_build_proof.json','evidence/native_standard_build_proof.json'],'new_sensor_types':[]},indent=2))
print('source files',len(rows),'patch bytes',(A/'source_changes.patch').stat().st_size)
