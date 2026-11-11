"""Commission actual allowed channels; no ARM, no navigation truth substitution."""
import json,time,traceback,statistics,math
from pathlib import Path
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.adapters.layer5 import Layer5Adapter
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter
from layer6.realtime import RealtimeIO
from layer6.native_session import session_health

root=Path(__file__).resolve().parent.parent;out=Path(__file__).parent/'evidence'
cfg=TDOCConfig.from_yaml(root/'TDOC_v20260911_2052/configs/tdoc.yaml')
l5=Layer5Adapter(cfg);l5._ensure();l6=Layer6OnlineAdapter(cfg,l5);io=None;result={}
try:
    ready=l6.prepare_online_transport_session();result['startup']=ready
    if not ready.get('ok'):raise RuntimeError(str(ready))
    status=l6._status();sid=status['status_quality']['native_session_id']
    result['initial_validation_only']=status
    io=RealtimeIO(sid,out/'probe_wire.jsonl')
    names=['SIM_GPS1_ENABLE','SIM_GPS2_ENABLE','SIM_GPS3_ENABLE','SIM_GPS4_ENABLE',
        'AHRS_EKF_TYPE','INS_ACCOFFS_X','INS_ACCOFFS_Y','INS_ACCOFFS_Z',
        'INS_ACCSCAL_X','INS_ACCSCAL_Y','INS_ACCSCAL_Z','Q_A_ANGLE_MAX','Q_PILOT_SPD_UP',
        'Q_PILOT_SPD_DN','Q_TAILSIT_INPUT','RC1_DZ','RC2_DZ','RC3_DZ']
    result['parameters']=io.request_parameters(names,timeout=12.)
    rows=[];last=-1.;start=time.monotonic()
    while time.monotonic()-start<8.:
        try:
            m,s,v=io.sample(last)
            if s:last=s[-1]['plant_time_s'];rows.extend(s)
            if v['HEARTBEAT']['data']['base_mode']&128:raise RuntimeError('probe requires disarmed')
        except RuntimeError as exc:print(str(exc),flush=True)
        time.sleep(.1)
    result['samples']=len(rows)
    if rows:
        result['mean']={k:statistics.mean(r['data'][k] for r in rows)
            for k in ('xacc','yacc','zacc','xgyro','ygyro','zgyro','xmag','ymag','zmag','abs_pressure','pressure_alt')}
        g=result['mean'];result['gravity_norm']=math.sqrt(sum(g[k]**2 for k in ('xacc','yacc','zacc')))
        result['gravity_pitch_deg']=math.degrees(math.atan2(g['xacc'],math.hypot(g['yacc'],g['zacc'])))
        result['plant_duration_s']=rows[-1]['plant_time_s']-rows[0]['plant_time_s']
    result['final_validation_only']=l6._status()
    result['health']=session_health(sid)
    result['ok']=bool(rows) and all(result['parameters'][n]['value']==0 for n in names[:4])
except Exception as exc:
    result['ok']=False;result['error']=repr(exc);traceback.print_exc()
finally:
    if io:io.close()
    l6.close();l5.close()
    (out/'probe_result.json').write_text(json.dumps(result,indent=2,default=str))
    print({k:v for k,v in result.items() if k not in ('startup','initial_validation_only','final_validation_only','health')},flush=True)
