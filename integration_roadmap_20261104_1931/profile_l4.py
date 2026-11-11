"""Read-only profile of archived state, using a separate copied KStore/workspace."""
import cProfile
import gzip
import json
import os
from pathlib import Path
import pstats
import time

from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.adapters.layer2 import Layer2Adapter
from tddt_tdoc.adapters.layer3 import Layer3Adapter
from tddt_tdoc.adapters.layer4 import Layer4Adapter
from tddt_tdoc.adapters.layer5 import Layer5Adapter
from tddt_tdoc.unit_adapter import UnitAdapterBinding

audit=Path(__file__).resolve().parent
root=audit.parent/'TDOC_v20260911_2052'
os.chdir(root)
cfg=TDOCConfig.from_yaml('configs/tdoc.yaml')
cfg.data['runtime']['root']=str(audit/'evidence/profile_runtime')
for name in ('knowledge','cycles'):
    target=cfg.runtime_root/name
    if not target.exists():target.symlink_to(root/'runtime'/name,target_is_directory=True)
os.environ['TDDT_KSTORE_ROOT']=str(audit/'evidence/profile_kstore')
archived=json.load(gzip.open(root/'runtime/cycles/workflow-online_jsbsim-e0001-c0001-pre-release-vertical_takeoff.json.gz','rt'))
offline=json.load(gzip.open(root/'runtime/cycles/mission-offline-0003.json.gz','rt'))
cert=offline['future_layers']['5']['output']['result']['phoenix']['runtime_validation_certificate']['certificate']
units=UnitAdapterBinding(cfg)
l2=Layer2Adapter(cfg,units)
l3=Layer3Adapter(cfg,units,l2)
l5=Layer5Adapter(cfg)
l4=Layer4Adapter(cfg,l3,l5)
print(l5.apply_offline_model_certificate(cert),flush=True)
print(l5.set_verification_model_certificate(cert),flush=True)
request=json.loads((root/'examples/runtime_request.json').read_text())
request.update(learning_mode='ONLINE_JSBSIM',stage='vertical_takeoff',rtmo_pre_release_verify=True,
               transaction_id='READONLY_PROFILE',reference_time_s=archived['reference_time_s'])
request['context']=archived['layer2_packets']['MESA_NAV']['context']
l4.prepare_critical_path()
if os.environ.get('PROFILE_CURRENT_FF')=='1':
    l4.engine.mf.path=audit/'evidence/profile_empty_certificate_bank.jsonl'
    l4.engine.mf._cache=None
    projection=request['context']['native_climate_time_projection']
    request.setdefault('climate',{})['time']={'value':projection['dataset_timestamp'],
        'time_reference':projection['time_reference'],'time_binding':projection,
        'unit':'ISO8601','frame':'time','source':'archived-observation-diagnostic'}
profile=cProfile.Profile()
try:
    start=time.perf_counter()
    result=profile.runcall(l4.invoke,{'snapshot':archived['layer3_result']['snapshot'],'request':request,'transaction_id':'READONLY_PROFILE'})
    print('elapsed_s',time.perf_counter()-start,'verified',len(result['result'].get('verified_candidates',[])),flush=True)
    (audit/'evidence/profile_current_ff_result.json').write_text(json.dumps(result['result']['provenance'].get('full_fidelity_verification'),indent=2))
finally:
    with (audit/('evidence/l4_profile_current_ff.txt' if os.environ.get('PROFILE_CURRENT_FF')=='1' else 'evidence/l4_profile_before.txt')).open('w') as stream:
        pstats.Stats(profile,stream=stream).sort_stats('cumulative').print_stats(50)
    for adapter in (l4,l5,l3,l2):
        close=getattr(adapter,'close',None)
        if close:close()
