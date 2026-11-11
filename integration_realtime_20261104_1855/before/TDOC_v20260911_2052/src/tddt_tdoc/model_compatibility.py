"""R3.06 exact model dependencies, checked at certificate issuance and reuse.

Identity is provenance, not qualification. Never add it to stored legacy evidence.
Dynamic origin/forcing and current state/action/freshness retain separate gates.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile
from collections import OrderedDict
import threading

_YAML_SEMANTICS=OrderedDict()
_YAML_SEMANTICS_LOCK=threading.RLock()

REVISION = 'phoenix-fourfoot-certificates-r3.06-v1'
SCHEMA = 'TDOC-model-dependencies-v1'
POLICY_SHA256 = '0861e64c1020036027e0ebd45994bbc021a2851537a946855e042482242f4e98'


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',', ':'),
        ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def integrated_native(config):
    return config.get('layers.layer6.online.transport') == 'Layer6Client_GUI_MAVLink'


def compatibility_policy():
    p=Path(__file__).resolve().parent/'resources/phoenix_fourfoot/r3_06_v1/COMPATIBILITY_POLICY.json'
    raw=p.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=POLICY_SHA256:
        raise ValueError('model compatibility policy hash mismatch')
    return json.loads(raw)


def _stat(path):
    s=path.stat()
    return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)


def read_safe_yaml(path):
    """Read/hash current bytes every time; reuse only parsed identical semantics.

    This does not cache a file hash, model identity or a successful integrity gate.
    Returning a deep copy prevents a caller from changing the approved parse.
    """
    import yaml
    raw=Path(path).read_bytes();key=(str(Path(path).resolve()),hashlib.sha256(raw).hexdigest())
    with _YAML_SEMANTICS_LOCK:
        if key not in _YAML_SEMANTICS:
            _YAML_SEMANTICS[key]=yaml.load(raw,Loader=getattr(yaml,'CSafeLoader',yaml.SafeLoader))
        _YAML_SEMANTICS.move_to_end(key)
        result=deepcopy(_YAML_SEMANTICS[key])
        while len(_YAML_SEMANTICS)>16:_YAML_SEMANTICS.popitem(last=False)
        return result


def capture_model_dependencies(config):
    """Read current deployed artifacts; no memoization by handoff or file name."""
    from .native_ground_policy import require_native_contacts,POLICY_SHA256 as GROUND_SHA
    from .phoenix_geometry import validate_native_geometry,geometry_profile,aligned_root
    compatibility_policy()
    ground=require_native_contacts(config)
    validate_native_geometry(config)
    geometry_profile(config)
    twins=config.path_value('TDDT_TWINS_ROOT',required=True)
    climate=config.path_value('TDDT_CLIMATE_ROOT',required=True)
    l5=config.path_value('TDDT_LAYER5_ROOT',required=True)
    l6=config.path_value('TDDT_LAYER6_ROOT',required=True)
    fs=l6/'FlightStack'
    paths={
        'native_online_workflow':config.package_root/'scripts/run_training_workflows.py',
        'layer2_profile':twins/'config/phoenix_drone_v3.yaml',
        'layer5_profile':l5/'resources/phoenix_drone_v3.yaml',
        'mesa_nav_fmu':twins/'fmus/MESA_NAV.fmu',
        'wingmpet_fmu':twins/'fmus/WingMPET.fmu',
        'micromet_fmu':climate/'fmu/MicroMetTwin.fmu',
        'flightstack_profile':fs/'data/phoenix_drone_v3.yaml',
        'native_aircraft':fs/'ardupilot/Tools/autotest/aircraft/phoenix_v3/phoenix_v3.xml',
        'packaged_aircraft':fs/'models/phoenix_v3/aircraft/phoenix_v3/phoenix_v3.xml',
        'reset_geometry_template':aligned_root()/'reset.xml',
        'native_bridge':fs/'ardupilot/libraries/SITL/SIM_JSBSim.cpp',
        'native_bridge_header':fs/'ardupilot/libraries/SITL/SIM_JSBSim.h',
        'native_body_rate_decoder':fs/'ardupilot/libraries/SITL/SIM_JSBSimBodyRates.h',
        'arduplane_binary':fs/'ardupilot/build/sitl/bin/arduplane',
        'jsbsim_binary':fs/'jsbsim/build/src/JSBSim',
        'ardupilot_default_parameters':fs/'ardupilot/Tools/autotest/default_params/phoenix_v3.parm',
        'pilot_configuration':fs/'phoenix_controller/config/f310_phoenix.yaml',
    }
    # Configurations and executable Python model/mapping code also affect replay.
    for env_key,label in [('TDDT_LAYER3_ROOT','layer3'),('TDDT_LAYER4_ROOT','layer4'),('TDDT_LAYER5_ROOT','layer5')]:
        root=config.path_value(env_key,required=True)
        for p in sorted((root/'src'/label).rglob('*.py')):
            paths[label+'/'+p.relative_to(root/'src'/label).as_posix()]=p
        for p in sorted((root/'configs').glob('*.yaml')):
            paths[label+'/configs/'+p.name]=p
    module_root=Path(__file__).resolve().parent
    for p in sorted(module_root.rglob('*.py')):
        paths['tdoc/'+p.relative_to(module_root).as_posix()]=p
    for root,label in ((l6/'src/layer6','layer6'),(fs/'phoenix_controller','virtual_pilot')):
        for p in sorted(root.rglob('*.py')):
            if '.venv' not in p.parts and 'venv' not in p.parts:
                paths[label+'/'+p.relative_to(root).as_posix()]=p
    paths['native_launcher']=l6/'src/run_flightstack.py'
    paths['native_gui_configuration']=l6/'gui/config/f310_phoenix.yaml'
    for root,label in ((l6/'src/gui','native_gui'),(fs/'phoenix_environment_adapter_v2/phoenix_env_adapter','environment_adapter')):
        for p in sorted(root.glob('*.py')):
            paths[label+'/'+p.name]=p
    for p in sorted((climate/'python').glob('*.py')):
        paths['micromet_preprocessor/'+p.name]=p
    stats={name:_stat(p) for name,p in paths.items()}
    artifacts={name:{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':stats[name][2]}
               for name,p in paths.items()}
    if artifacts['native_aircraft']!=artifacts['packaged_aircraft']:
        raise ValueError('native/packaged aircraft model mismatch')
    if artifacts['layer2_profile']!=artifacts['flightstack_profile']:
        raise ValueError('FlightStack/Layer2 profile mismatch')
    a=read_safe_yaml(paths['layer2_profile'])
    b=read_safe_yaml(paths['layer5_profile'])
    if a.get('ground_geometry')!=b.get('ground_geometry'):
        raise ValueError('Layer2/Layer5 ground geometry mismatch')
    for name in ('mesa_nav_fmu','wingmpet_fmu'):
        with ZipFile(paths[name]) as z:
            embedded=hashlib.sha256(z.read('resources/phoenix_drone_v3.yaml')).hexdigest()
        if embedded!=artifacts['layer2_profile']['sha256']:
            raise ValueError('stale embedded Phoenix profile: '+name)
    if any(_stat(p)!=stats[name] for name,p in paths.items()):
        raise ValueError('model dependencies changed during capture')
    body={'schema':SCHEMA,'revision':REVISION,'policy_sha256':POLICY_SHA256,
          'ground_policy_revision':ground['revision'],'ground_policy_sha256':GROUND_SHA,
          'artifacts':artifacts}
    return {**body,'model_id':digest(body)}


def valid_dependencies(binding):
    if not isinstance(binding,dict):return False
    try:
        body={k:v for k,v in binding.items() if k!='model_id'}
        return bool(body.get('schema')==SCHEMA and body.get('revision')==REVISION
            and body.get('policy_sha256')==POLICY_SHA256 and body.get('artifacts')
            and binding.get('model_id')==digest(body))
    except (ValueError,TypeError):return False


def certificate_compatibility(certificate,current):
    dependencies=(certificate or {}).get('model_dependencies') if isinstance(certificate,dict) else None
    reason=('current_model_dependencies_invalid' if not valid_dependencies(current) else
            'certificate_model_dependencies_missing' if dependencies is None else
            'certificate_model_dependencies_invalid' if not valid_dependencies(dependencies) else
            'certificate_model_dependencies_mismatch' if dependencies!=current else 'exact_model_dependencies_match')
    return {'ok':reason=='exact_model_dependencies_match','reason':reason,'revision':REVISION,
            'current_model_id':current.get('model_id') if isinstance(current,dict) else None,
            'certificate_model_id':dependencies.get('model_id') if isinstance(dependencies,dict) else None,
            'scope':'MODEL_IDENTITY_ONLY','flight_authority':False,'current_state_replaced':False}


def qualify_new_certificate(certificate,before,after):
    """Only an issuer may call this after NEW qualification; never a migration tool."""
    if not valid_dependencies(before) or before!=after:
        raise ValueError('model dependencies changed during certificate qualification')
    return {**certificate,'model_dependencies':deepcopy(before)}
