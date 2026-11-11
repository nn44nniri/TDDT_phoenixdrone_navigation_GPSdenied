"""Read-only commissioning: real Native windows, no ARM or pilot submission."""
import gzip,json,time,math
from pathlib import Path
from tddt_tdoc import TDOCConfig
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter
from tddt_tdoc.adapters.layer5 import Layer5Adapter
from tddt_tdoc.online_imu import prepare_native_imu_history,native_imu_history_samples
from layer6.native_session import session_health

ROOT=Path(__file__).resolve().parent.parent
OUT=Path(__file__).resolve().parent/'evidence'
cfg=TDOCConfig.from_yaml(ROOT/'TDOC_v20260911_2052/configs/tdoc.yaml')
layer5=Layer5Adapter(cfg);layer5._ensure()
adapter=Layer6OnlineAdapter(cfg,layer5)
results=[];errors=[];started=time.monotonic()
try:
    ready=adapter.prepare_online_transport_session()
    (OUT/'ground_start.json').write_text(json.dumps(ready,indent=2))
    if not ready.get('ok'):raise RuntimeError(str({k:ready.get(k) for k in ('stage','error')}))
    reader=adapter._native_history_reader;after=reader.anchor
    start=adapter._status(include_imu_samples=True)
    (OUT/'ground_initial_status.json').write_text(json.dumps(start,indent=2))
    initial=start['plant_time_s'];deadline=time.monotonic()+85
    with (OUT/'ground_status.jsonl').open('w',buffering=1) as log:
        while time.monotonic()<deadline:
            try:
                mark=time.monotonic();status=adapter._status(include_imu_samples=True)
                if status.get('armed') is not False or status.get('throttle')!=0.:raise RuntimeError('ground test requires disarmed zero throttle')
                row={'wall_s':time.time(),'elapsed_s':time.monotonic()-mark,'status':status}
                log.write(json.dumps(row)+'\n');results.append(row)
                elapsed=status['plant_time_s']-initial
                print('ground',round(elapsed,2),'s status',round(row['elapsed_s'],3),'s',flush=True)
                if elapsed>=60.:break
            except Exception as exc:
                error={'error':repr(exc),'validation':getattr(adapter,'_environment_rejection',None)}
                errors.append(error);log.write(json.dumps(error)+'\n');print(error,flush=True)
            time.sleep(.3)
    last=adapter._status(include_imu_samples=True);through=last['telemetry_metadata']['SCALED_IMU']['history_sequence']
    pages=reader.window(after,through)
    receipt=prepare_native_imu_history(pages,expected_session_id=reader.session,expected_monotonic_domain_id=reader.domain,
        target_system=reader.target[0],target_component=reader.target[1],after_seq=after)
    with gzip.open(OUT/'ground_imu.json.gz','wt') as f:json.dump({'anchor_time_s':reader.anchor_plant_time_s,'pages':pages,'samples':native_imu_history_samples(receipt)},f)
    health=session_health((last.get('status_quality') or {})['native_session_id'])
    (OUT/'ground_final_health.json').write_text(json.dumps(health,indent=2))
finally:
    adapter.close()
    layer5.close()
    summary={'samples':len(results),'errors':errors,'actuation_submitted':False,'arming_requested':False,
        'wall_duration_s':time.monotonic()-started,'plant_duration_s':results[-1]['status']['plant_time_s']-results[0]['status']['plant_time_s'] if results else 0.}
    (OUT/'ground_result.json').write_text(json.dumps(summary,indent=2));print(summary,flush=True)
