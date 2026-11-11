"""Snapshot/recheck the real finalized prior and deployed source/binaries."""
import gzip,hashlib,json,os,sys
from pathlib import Path
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.climate_forcing import resolve_climate_forcing
from tddt_tdoc.knowledge_handoff import OfflineKnowledgeHandoff
from tddt_tdoc.model_compatibility import capture_model_dependencies,certificate_compatibility

audit=Path(__file__).resolve().parent;root=audit.parent;out=audit/'evidence'
cfg=TDOCConfig.from_yaml(root/'TDOC_v20260911_2052/configs/tdoc.yaml')
def read(p):return json.load(gzip.open(p,'rt'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
store=Path(os.environ['TDDT_KSTORE_ROOT'])
manifest=cfg.runtime_root/'knowledge/handoff/offline_latest.json.gz'
handoff=read(manifest)
resolver=OfflineKnowledgeHandoff.__new__(OfflineKnowledgeHandoff);resolver.latest_path=manifest
gate=resolver.validate_online_start()
model=capture_model_dependencies(cfg)
cycle=read(cfg.runtime_root/'cycles'/(handoff['source_transaction_id']+'.json.gz'))
cert=cycle['layer_status']['4']['evidence']['offline_model_control']['certificate']
compat=certificate_compatibility(cert,model)
forcing=resolve_climate_forcing(cfg.path_value('TDDT_CLIMATE_ROOT'),require_exists=True)
snapshot={'model_dependencies':model,'tdoc_yaml_sha256':sha(cfg.path),'handoff_hash':handoff['handoff_hash'],
    'handoff':handoff,'handoff_gate':{k:gate.get(k) for k in ('ok','status','reason')},'model_gate':compat,
    'forcing_file':str(forcing),'forcing_sha256':sha(forcing),'kstore_root':str(store),
    'kstore_files':{str(p.relative_to(store)):sha(p) for p in store.rglob('*') if p.is_file()}}
if sys.argv[1]=='before':
    assert gate['ok'] and compat['ok'],(gate.get('reason'),compat)
    frozen=json.loads((out/'final_source_before_full_training_completed.json').read_text())
    assert frozen['model_dependencies']==model and frozen['tdoc_yaml_sha256']==sha(cfg.path)
    (out/'final_prior_before_online.json').write_text(json.dumps(snapshot,indent=2))
    print(snapshot['handoff_gate'],compat['reason'],'model',model['model_id'])
else:
    before=json.loads((out/'final_prior_before_online.json').read_text())
    initial=json.loads((out/'prior_before_online.json').read_text())
    binaries=('mesa_nav_fmu','wingmpet_fmu','micromet_fmu','arduplane_binary','jsbsim_binary')
    result={'source_unchanged_during_final_online':before['model_dependencies']==model,
        'yaml_unchanged':before['tdoc_yaml_sha256']==sha(cfg.path),
        'handoff_unchanged':before['handoff_hash']==snapshot['handoff_hash'],
        'kstore_unchanged':before['kstore_files']==snapshot['kstore_files'],
        'forcing_unchanged':before['forcing_sha256']==snapshot['forcing_sha256'],
        'compiled_artifacts_unchanged_from_first_run':all(
            initial['model_dependencies']['artifacts'][k]==model['artifacts'][k] for k in binaries),
        'binary_hashes':{k:model['artifacts'][k] for k in binaries},
        'handoff_gate':snapshot['handoff_gate'],'model_gate':compat,
        'reset_performed':False,'compiled_source_changes':False,'rebuilt':False}
    (out/'preservation_final.json').write_text(json.dumps(result,indent=2));print(result)
