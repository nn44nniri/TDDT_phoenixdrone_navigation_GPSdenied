"""Transfer an observed plant clock to Layer3 without calling naive data UTC."""
import hashlib
import math
from pathlib import Path

from .native_binding import digest

SCHEMA='TDOC-native-climate-time-projection-r4.04-v1'


def project_native_time(binding_path,plant_time_s):
    from layer6.binding import forcing_time,consumption_identity
    if isinstance(plant_time_s,bool) or not isinstance(plant_time_s,(float,int)) or not math.isfinite(plant_time_s):
        raise ValueError('observed native plant time required; no scheduler/zero fallback')
    from layer6.binding import context_guard
    path=Path(binding_path).resolve(strict=True);b=context_guard(path).binding
    if hashlib.sha256(Path(b['forcing_file']).read_bytes()).hexdigest()!=b['forcing_file_sha256']:
        raise ValueError('native climate forcing hash mismatch')
    timestamp,elapsed=forcing_time(b,plant_time_s)
    result={'schema':SCHEMA,**consumption_identity(b),'binding_path':str(path),
        'forcing_file':b['forcing_file'],'plant_time_s':float(plant_time_s),'mission_elapsed_s':elapsed,'dataset_timestamp':timestamp,
        'clock_domain':b['time_contract']['clock_domain'],'timezone_status':b['time_contract']['timezone_status'],
        'time_reference':'ACCEPTED_DATASET_NAIVE' if b['time_contract']['timezone_status']=='unspecified' else 'OFFSET_EXPLICIT',
        'source':'TDOC observed FCU plant clock; not scheduler reference','UTC_conversion_performed':False}
    result['projection_hash']=digest(result)
    return result


def validate_native_time(projection):
    if not isinstance(projection,dict) or projection.get('schema')!=SCHEMA:
        raise ValueError('native climate time projection required')
    expected=project_native_time(projection['binding_path'],projection['plant_time_s'])
    if projection!=expected:raise ValueError('native climate time projection/binding mismatch')
    return projection


def stamp_climate_time(packet,context):
    projection=(context or {}).get('native_climate_time_projection')
    if projection is None:return packet
    p=validate_native_time(projection)
    if packet.get('timestamp')!=p['dataset_timestamp']:
        raise ValueError('MicroMet source timestamp differs from bound native query')
    return {**packet,'timestamp_reference':p['time_reference'],'native_climate_time_projection':p}
