"""R4.01: one immutable, content-bound native preparation context.

This module constructs/validates data. It never launches, actuates, refreshes,
repairs knowledge or claims that a simulator has applied the bound environment.
"""
from __future__ import annotations
import csv
from datetime import datetime,timezone
import gzip
import hashlib
import io
import json
import math
from pathlib import Path,PurePosixPath
from urllib.parse import parse_qs,urlsplit
import uuid

from .model_compatibility import capture_model_dependencies,digest,valid_dependencies

SCHEMA='TDOC-native-environment-binding-v2'
REVISION='native-unified-binding-r4.04-v1'
POLICY_SHA256='55987a874022ac4a4fcc0a40cce8e505672abe24b39edf09a517bde613f3edd1'


def binding_policy():
    p=Path(__file__).resolve().parent/'resources/native_binding/r4_04_v1/BINDING_POLICY.json'
    raw=p.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=POLICY_SHA256:raise ValueError('native binding policy hash mismatch')
    return json.loads(raw)


def _sha(raw):return hashlib.sha256(raw).hexdigest()
def _stat(path):
    s=path.stat();return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)


class _Inputs:
    def __init__(self):self.stats={};self.files={}
    def read(self,path,*,logical=False):
        p=Path(path)
        if logical and p.suffix!='.gz':
            compressed=Path(str(p)+'.gz')
            if p.exists() and compressed.exists():raise ValueError('ambiguous plaintext/compressed input: '+str(p))
            if not p.exists():p=compressed
        p=p.resolve(strict=True);before=_stat(p);raw=p.read_bytes()
        if p in self.stats and self.stats[p]!=before:raise ValueError('binding input changed between reads: '+str(p))
        if _stat(p)!=before:raise ValueError('binding input changed during read: '+str(p))
        self.stats[p]=before
        data=gzip.decompress(raw) if logical and p.suffix=='.gz' else raw
        row={'path':str(p),'file_sha256':_sha(raw),'bytes':len(raw),
             'logical_sha256':_sha(data),'logical_bytes':len(data),'gzip':logical and p.suffix=='.gz'}
        self.files[str(p)]=row
        return data,row
    def finish(self):
        if any(_stat(p)!=s for p,s in self.stats.items()):raise ValueError('binding inputs changed during construction')


def _relative(root,name):
    p=PurePosixPath(str(name))
    if p.is_absolute() or not p.parts or '..' in p.parts:raise ValueError('unsafe SETD path')
    path=root.joinpath(*p.parts)
    path.resolve().relative_to(root.resolve())
    return path


def _knowledge(config,manifest,manifest_path,inputs):
    raw,hfile=inputs.read(manifest_path,logical=True)
    # The gate and the physical file must describe the same immutable handoff.
    if json.loads(raw)!=manifest:raise ValueError('handoff changed after integrity validation')
    setd=manifest['setd'];root=config.path_value('TDDT_LAYER5_KSTORE_ROOT',required=True)
    raw,mfile=inputs.read(_relative(root,setd['package_manifest']),logical=True)
    if _sha(raw)!=setd.get('package_manifest_sha256'):raise ValueError('SETD manifest hash mismatch')
    m=json.loads(raw)
    if (m.get('schema')!='SETD-package-manifest-v2' or m.get('state')!='COMMITTED'
            or m.get('loadable') is not True or m.get('package_id')!=setd.get('package_id')):
        raise ValueError('SETD identity/state mismatch')
    if digest({k:v for k,v in m.items() if k!='manifest_content_sha256'})!=m.get('manifest_content_sha256'):
        raise ValueError('SETD manifest content hash mismatch')
    package=_relative(root,setd['package_manifest']).parent
    files=[]
    for name,expected in [(m['header_file'],m['header_sha256'])]+[
            (f['file'],f['sha256']) for c in m['components'] for f in c['files']]:
        data,row=inputs.read(_relative(package,name),logical=True)
        if _sha(data)!=expected:raise ValueError('SETD component hash mismatch: '+str(name))
        files.append(row)
    return {'handoff_hash':manifest['handoff_hash'],'handoff_file':hfile,
        'knowledge_id':manifest['knowledge_id'],'knowledge_version':manifest['knowledge_version'],
        'source_transaction_id':manifest['source_transaction_id'],'setd_package_id':setd['package_id'],
        'setd_manifest':mfile,'setd_manifest_content_sha256':m['manifest_content_sha256'],
        'setd_logical_content_sha256':m['logical_content_sha256'],'verified_package_files':files,
        'role':'offline-prior-only','actuation_authority':False}


def _time(value):
    if not isinstance(value,str) or not value.strip():raise ValueError('binding epoch missing')
    return datetime.fromisoformat(value.replace('Z','+00:00'))


def _forcing(contract,inputs):
    expected=contract.get('forcing_file_sha256')
    if not expected or not contract.get('forcing_file'):raise ValueError('Offline forcing identity required; refresh is forbidden')
    path=Path(contract['forcing_file'])
    raw,row=inputs.read(path)
    if _sha(raw)!=expected:raise ValueError('Offline forcing identity required; hash mismatch')
    series={};source_times=[];aware=None;count=0
    reader=csv.DictReader(io.StringIO(raw.decode('utf-8')))
    if not {'time','point'}.issubset(reader.fieldnames or []):raise ValueError('forcing time/point columns missing')
    for sample in reader:
        point=str(sample.get('point') or '').strip()
        if not point:raise ValueError('forcing point missing')
        t=_time(sample['time']);is_aware=t.utcoffset() is not None
        if aware is not None and aware!=is_aware:raise ValueError('mixed forcing clock domains')
        aware=is_aware
        source_times.append({'point':point,'time':sample['time']})
        values=series.setdefault(point,[])
        if values and t<=values[-1]:raise ValueError('forcing timestamps duplicate or reversed')
        values.append(t);count+=1
    if not series:raise ValueError('forcing has no samples')
    epoch=_time(contract.get('effective_climate_epoch'))
    if (epoch.utcoffset() is not None)!=aware:raise ValueError('handoff/forcing clock domain mismatch')
    start=max(v[0] for v in series.values());end=min(v[-1] for v in series.values())
    if not start<=epoch<=end:raise ValueError('handoff epoch outside common forcing coverage')
    for name,value in [('forcing_start',min(v[0] for v in series.values())),('forcing_end',max(v[-1] for v in series.values()))]:
        declared=_time(contract.get(name))
        if (declared.utcoffset() is not None)!=aware or declared!=value:
            raise ValueError('handoff/forcing window mismatch: '+name)
    metadata=None;meta_path=path.with_suffix(path.suffix+'.meta.json')
    if meta_path.is_file():
        data,mrow=inputs.read(meta_path);m=json.loads(data)
        selectors=sorted({s for p in m.get('points',[]) for s in parse_qs(urlsplit(p.get('url','')).query).get('timezone',[])})
        metadata={'file':mrow,'requested_start':m.get('requested_start'),'requested_end':m.get('requested_end'),
                  'timezone_request_selectors':selectors,'response_timezone_recorded':False,
                  'request_offset_is_not_response_clock_authority':True}
    return {'file':row,'sha256':expected,'source':'accepted-handoff-only','sample_count':count,
        'point_coverage':{k:{'start':v[0].isoformat(),'end':v[-1].isoformat(),'samples':len(v)} for k,v in sorted(series.items())},
        'common_start':start.isoformat(),'common_end':end.isoformat(),'metadata':metadata,
        'time_reference':{'source_times_sha256':digest(source_times),'effective_epoch_exact':contract['effective_climate_epoch'],
            'epoch_source':'accepted-handoff/reuse_contract/climate_time_alignment/effective_climate_epoch',
            'representation_policy':'preserve-dataset-clock-and-explicit-offset; no implicit normalization'},
        'clock_domain':'UTC_OFFSET_AWARE' if aware else 'ACCEPTED_DATASET_NAIVE',
        'timezone_status':'offset_explicit' if aware else 'unspecified',
        'utc_mapping_verified':bool(aware),'refresh_performed':False},epoch


def _identity_parts(binding):
    return {key:digest(binding[key]) for key in ('model_dependencies','knowledge','forcing','origin','time_contract','ground_contact_policy')}


def validate_binding(binding):
    """Validate an immutable v2 record; this never asserts live freshness/readback."""
    try:
        if not isinstance(binding,dict) or binding.get('schema')!=SCHEMA or binding.get('revision')!=REVISION:
            raise ValueError('native binding schema/revision mismatch')
        binding_policy()
        if binding.get('policy_sha256')!=POLICY_SHA256:raise ValueError('native binding policy mismatch')
        uid=binding.get('binding_id','')
        if len(uid)!=32 or uuid.UUID(uid).hex!=uid or uuid.UUID(uid).version!=4:raise ValueError('native binding ID invalid')
        if binding.get('binding_hash')!=digest({k:v for k,v in binding.items() if k!='binding_hash'}):
            raise ValueError('native binding content hash mismatch')
        if not valid_dependencies(binding['model_dependencies']):raise ValueError('native binding model identity invalid')
        if binding.get('identity_hashes')!=_identity_parts(binding):raise ValueError('native binding identity hashes mismatch')
        if binding.get('context_sha256')!=digest(binding['identity_hashes']):raise ValueError('native binding context hash mismatch')
        aliases={'handoff_hash':binding['knowledge']['handoff_hash'],
                 'forcing_file':binding['forcing']['file']['path'],'forcing_file_sha256':binding['forcing']['sha256'],
                 'effective_climate_epoch':binding['time_contract']['effective_climate_epoch'],
                 'plant_epoch_s':binding['time_contract']['plant_epoch_s'],'mission_origin':binding['origin']['initialization'],
                 'ground_geometry':binding['origin']['ground_geometry'],
                 'model_id':binding['model_dependencies']['model_id']}
        if any(binding.get(k)!=v for k,v in aliases.items()):raise ValueError('native binding projection mismatch')
        if binding.get('actuation_authority') is not False or binding.get('execution_environment_verified') is not False:
            raise ValueError('preparation binding cannot grant execution authority')
        if binding['knowledge']['actuation_authority'] is not False or binding['knowledge']['role']!='offline-prior-only':
            raise ValueError('knowledge must remain a prior')
        stamp=_time(binding['prepared_at_utc'])
        if stamp.utcoffset() is None or stamp.utcoffset().total_seconds()!=0:raise ValueError('binding audit time must be UTC-aware')
        known=binding['mission_origin'];geometry=binding['ground_geometry']
        values=[known[k] for k in ('latitude_deg','longitude_deg','altitude_msl_m','terrain_elevation_msl_m','cg_height_agl_m','heading_deg')]
        if any(isinstance(x,bool) or not isinstance(x,(float,int)) or not math.isfinite(x) for x in values):
            raise ValueError('invalid binding origin values')
        if (abs(known['latitude_deg'])>90 or abs(known['longitude_deg'])>180 or not 0<=known['heading_deg']<360
                or known['cg_height_agl_m']<=0 or abs(known['cg_height_agl_m']-geometry['cg_height_agl_m'])>1e-9
                or abs(known['altitude_msl_m']-known['terrain_elevation_msl_m']-known['cg_height_agl_m'])>1e-8):
            raise ValueError('binding origin/CG geometry inconsistent')
        if known['initialization_only'] is not True or known['gnss_telemetry_used'] is not False:
            raise ValueError('binding origin cannot become navigation truth')
        time=binding['time_contract'];anchor=time['plant_epoch_s']
        if isinstance(anchor,bool) or not isinstance(anchor,(int,float)) or not math.isfinite(anchor) or anchor<0:
            raise ValueError('invalid native plant epoch')
        epoch=_time(time['effective_climate_epoch']);forcing=binding['forcing']
        if not _time(forcing['common_start'])<=epoch<=_time(forcing['common_end']):raise ValueError('binding epoch outside forcing coverage')
        if time['clock_domain']!=forcing['clock_domain'] or time['utc_mapping_verified']!=forcing['utc_mapping_verified']:
            raise ValueError('binding clock projection mismatch')
        aware=epoch.utcoffset() is not None
        if forcing['clock_domain']!=('UTC_OFFSET_AWARE' if aware else 'ACCEPTED_DATASET_NAIVE') or forcing['utc_mapping_verified'] is not aware:
            raise ValueError('binding clock authority unsupported')
        status='offset_explicit' if aware else 'unspecified'
        if forcing.get('timezone_status')!=status or time.get('timezone_status')!=status:
            raise ValueError('binding timezone status unsupported')
        if time['normalized_effective_epoch']!=epoch.isoformat():
            raise ValueError('binding normalized epoch mismatch')
        if (time.get('schema')!='TDOC-native-binding-declared-clock-v2'
            or time.get('source_clock')!='ArduPilot FCU boot milliseconds; not host UTC or JSBSim absolute time'
            or time.get('plant_epoch_source')!='declared-preparation-input; never first-packet rebase'
            or time.get('mission_elapsed_equation')!='elapsed_s=plant_time_s-plant_epoch_s'
            or time.get('forcing_time_equation')!='forcing_time=effective_climate_epoch+elapsed_s'
            or forcing['time_reference']['effective_epoch_exact']!=time['effective_climate_epoch']
            or time.get('UTC_conversion_performed') is not False or time.get('clock_rate_assumed') is not False):
            raise ValueError('binding time mapping/reference unsupported')
        if (time['runtime_clock_synchronization_verified'] is not False or time['observed_monotonic_s'] is not None
                or time['online_epoch_rebased'] is not False or time['mission_elapsed_epoch_s']!=0.):
            raise ValueError('preparation binding cannot claim runtime clock synchronization')
    except (KeyError,TypeError,AttributeError,OverflowError) as exc:
        raise ValueError('native binding contract incomplete') from exc
    return binding


def read_binding(path):return validate_binding(json.loads(Path(path).read_text(encoding='utf-8')))


def verify_bound_input_files(binding):
    """Rehash every accepted input; detect concurrent changes without rewriting it."""
    stats={}
    for row in binding['input_files']:
        p=Path(row['path']);before=_stat(p);raw=p.read_bytes()
        if _stat(p)!=before or _sha(raw)!=row['file_sha256']:
            raise ValueError('binding input hash mismatch: '+str(p))
        stats[p]=before
    if any(_stat(p)!=s for p,s in stats.items()):raise ValueError('binding inputs changed during verification')
    return stats


def build_native_binding(config,*,plant_epoch_s=0.,manifest_path=None):
    """Bind current model, accepted prior/forcing and surveyed origin, read-only."""
    from .knowledge_handoff import OfflineKnowledgeHandoff
    from .native_ground_policy import require_native_contacts
    from .online_mission import load_mission_prior
    from .phoenix_geometry import validate_native_geometry
    binding_policy();ground=require_native_contacts(config)
    if isinstance(plant_epoch_s,bool) or not isinstance(plant_epoch_s,(int,float)) or not math.isfinite(plant_epoch_s) or plant_epoch_s<0:
        raise ValueError('invalid native plant epoch')
    model=capture_model_dependencies(config)
    inputs=_Inputs();hp=Path(manifest_path) if manifest_path else config.runtime_root/'knowledge/handoff/offline_latest.json.gz'
    h=OfflineKnowledgeHandoff.__new__(OfflineKnowledgeHandoff);h.latest_path=hp
    gate=h.validate_online_start({'offline_knowledge_manifest':str(hp)})
    if not gate.get('ok'):raise ValueError('verified Offline handoff required for native environment')
    hm=gate['manifest'];knowledge=_knowledge(config,hm,hp,inputs)
    forcing,epoch=_forcing(hm['reuse_contract']['climate_time_alignment'],inputs)
    region_path=config.package_root/'region/region_v1.geojson';raw,region_file=inputs.read(region_path,logical=True)
    region=json.loads(raw);roles={}
    for f in region['features']:
        role=f.get('properties',{}).get('feature_role')
        if role in ('start','destination'):
            if role in roles:raise ValueError('duplicate mission endpoint')
            if f['geometry']['type']!='Point':raise ValueError('mission endpoint must be a Point')
            coord=f['geometry']['coordinates']
            if (len(coord)<2 or any(isinstance(x,bool) or not isinstance(x,(float,int)) or not math.isfinite(x) for x in coord[:2])
                    or abs(coord[0])>180 or abs(coord[1])>90):raise ValueError('invalid surveyed mission origin/destination')
            roles[role]=coord
    start=roles['start'];destination=roles['destination']
    _,dem_file=inputs.read(config.path_value('TDDT_MAP_ROOT',required=True)/'terrain_obstacles.tif')
    # Existing mission geometry is read without modifying or materializing KStore.
    prior=load_mission_prior(config,config.package_root/'reports',start,destination)
    _,reference_file=inputs.read(prior['reference_file'])
    if reference_file['file_sha256']!=prior['reference_sha256']:raise ValueError('mission reference changed during origin construction')
    alignment=validate_native_geometry(config)
    if prior['ground_geometry']['revision']!=alignment['revision']:raise ValueError('mission/native ground geometry revision mismatch')
    terrain=float(prior['endpoint_ground_msl_m']['origin']);cg=float(prior['ground_geometry']['cg_height_agl_m'])
    heading=math.degrees(prior['mission_heading_rad'])%360.
    if not all(math.isfinite(x) for x in (terrain,cg,heading)) or cg<=0:raise ValueError('invalid ground initialization geometry')
    known={'latitude_deg':float(start[1]),'longitude_deg':float(start[0]),'altitude_msl_m':terrain+cg,
        'terrain_elevation_msl_m':terrain,'cg_height_agl_m':cg,'heading_deg':heading,'map_crs':prior['map_crs'],
        'initialization_only':True,'gnss_telemetry_used':False}
    origin={'initialization':known,'ground_geometry':prior['ground_geometry'],
        'region_file':region_file,'terrain_file':dem_file,'mission_reference_file':reference_file,
        'frame':'WGS84_LON_LAT + PROJECTED_ENU + BODY_FRD_CG','altitude_equation':'CG_MSL=terrain_MSL+CG_AGL',
        'vertical_datum':'existing DEM MSL contract; geoid verification not asserted','navigation_truth_injection':False}
    clock={'schema':'TDOC-native-binding-declared-clock-v2','effective_climate_epoch':hm['reuse_contract']['climate_time_alignment']['effective_climate_epoch'],
        'normalized_effective_epoch':epoch.isoformat(),'clock_domain':forcing['clock_domain'],
        'timezone_status':forcing['timezone_status'],
        'utc_mapping_verified':forcing['utc_mapping_verified'],'naive_time_policy':'preserve-accepted-dataset-clock; no-host-timezone-or-rebase',
        'plant_epoch_s':float(plant_epoch_s),'plant_epoch_source':'declared-preparation-input; never first-packet rebase','mission_elapsed_epoch_s':0.,
        'source_clock':'ArduPilot FCU boot milliseconds; not host UTC or JSBSim absolute time',
        'monotonic_role':'observed receipt/freshness/cadence; host boot identity required at runtime',
        'UTC_conversion_performed':False,'clock_rate_assumed':False,
        'mission_elapsed_equation':'elapsed_s=plant_time_s-plant_epoch_s',
        'forcing_time_equation':'forcing_time=effective_climate_epoch+elapsed_s',
        'out_of_window_policy':'reject; no clamp or repeat','observed_monotonic_s':None,
        'runtime_clock_synchronization_verified':False,'configured_offline_epoch':hm['reuse_contract']['climate_time_alignment'].get('configured_layer3_epoch'),
        'online_epoch_rebased':False}
    result={'schema':SCHEMA,'revision':REVISION,'policy_sha256':POLICY_SHA256,'binding_id':uuid.uuid4().hex,
        'prepared_at_utc':datetime.now(timezone.utc).isoformat(),'readiness_scope':'PREPARATION_BINDING_ONLY',
        'actuation_authority':False,'execution_environment_verified':False,'policy_required_for_binding':False,
        'model_dependencies':model,'model_id':model['model_id'],'knowledge':knowledge,'forcing':forcing,'origin':origin,'time_contract':clock,
        'ground_geometry':prior['ground_geometry'],'ground_contact_policy':{'revision':ground['revision'],'fixture_supported':False,'actuation_authority':False},
        'navigation_authority':'MESA_STD','gps_role':'validation_reference_only',
        'handoff_hash':hm['handoff_hash'],'forcing_file':forcing['file']['path'],'forcing_file_sha256':forcing['sha256'],
        'effective_climate_epoch':clock['effective_climate_epoch'],'plant_epoch_s':clock['plant_epoch_s'],'mission_origin':known}
    result['identity_hashes']=_identity_parts(result);result['context_sha256']=digest(result['identity_hashes'])
    result['input_files']=list(inputs.files.values())
    inputs.finish()
    if capture_model_dependencies(config)!=model:raise ValueError('model changed during binding construction')
    result['binding_hash']=digest(result)
    return validate_binding(result)


def verify_binding_inputs(binding,config):
    """Read-only input/hash check; live application and freshness are later gates."""
    validate_binding(binding)
    if capture_model_dependencies(config)!=binding['model_dependencies']:raise ValueError('binding model no longer current')
    for row in binding['input_files']:
        raw=Path(row['path']).read_bytes()
        if _sha(raw)!=row['file_sha256']:raise ValueError('binding input hash mismatch: '+row['path'])
    rebuilt=build_native_binding(config,plant_epoch_s=binding['plant_epoch_s'],
        manifest_path=binding['knowledge']['handoff_file']['path'])
    if rebuilt['context_sha256']!=binding['context_sha256']:
        raise ValueError('binding semantic context no longer current')
    if rebuilt['input_files']!=binding['input_files']:
        raise ValueError('binding input index incomplete or inconsistent')
    return {'ok':True,'binding_id':binding['binding_id'],'binding_hash':binding['binding_hash'],
            'scope':'CURRENT_INPUT_IDENTITY_ONLY','execution_environment_verified':False,'actuation_authority':False}
