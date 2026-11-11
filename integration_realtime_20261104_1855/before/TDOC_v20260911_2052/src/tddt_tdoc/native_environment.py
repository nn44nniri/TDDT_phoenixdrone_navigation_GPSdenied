"""Create execution-only native environment binding without altering Offline artifacts."""
import hashlib
import json
import math
import os
import uuid
from pathlib import Path

from .knowledge_handoff import OfflineKnowledgeHandoff
from .online_mission import load_mission_prior
from .storage import read_json_auto


def prepare_binding(config, *, plant_epoch_s=0., manifest_path=None, output_path=None):
    """Publish the canonical native v2 record; legacy model-only callers stay v1."""
    from .model_compatibility import integrated_native
    if not integrated_native(config):
        return _prepare_legacy_binding(config,plant_epoch_s=plant_epoch_s,
            manifest_path=manifest_path,output_path=output_path)
    from .native_binding import build_native_binding,read_binding,validate_binding
    import tempfile
    binding=build_native_binding(config,plant_epoch_s=plant_epoch_s,manifest_path=manifest_path)
    target=Path(output_path) if output_path is not None else config.path_value('TDDT_LAYER6_ROOT',required=True)/'runtime/tdoc_environment_binding.json'
    if target.resolve() in {Path(r['path']) for r in binding['input_files']}:
        raise ValueError('binding output cannot overwrite an accepted input')
    allowed=(config.path_value('TDDT_LAYER6_ROOT',required=True)/'runtime',
             config.package_root/'reports',Path(tempfile.gettempdir()))
    if not any(target.resolve().is_relative_to(root.resolve()) for root in allowed):
        raise ValueError('binding output must remain in native runtime, reports or temporary output')
    target.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.'+target.name+'.',suffix='.tmp',dir=target.parent)
    tmp=Path(name)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(binding,stream,ensure_ascii=False,sort_keys=True,allow_nan=False)
            stream.write('\n');stream.flush();os.fsync(stream.fileno())
        validate_binding(json.loads(tmp.read_text(encoding='utf-8')))
        os.replace(tmp,target)
        if read_binding(target)!=binding:raise ValueError('native binding publication readback mismatch')
    finally:tmp.unlink(missing_ok=True)
    return binding


def _prepare_legacy_binding(config, *, plant_epoch_s=0., manifest_path=None, output_path=None):
    from .native_ground_policy import require_native_contacts
    ground_policy=require_native_contacts(config)
    if not math.isfinite(float(plant_epoch_s)) or plant_epoch_s<0:
        raise ValueError("invalid native plant epoch")
    path=Path(manifest_path) if manifest_path else config.runtime_root/"knowledge/handoff/offline_latest.json.gz"
    handoff=OfflineKnowledgeHandoff.__new__(OfflineKnowledgeHandoff)
    handoff.latest_path=path
    gate=handoff.validate_online_start({"offline_knowledge_manifest":str(path)})
    if not gate.get("ok"): raise ValueError("verified Offline handoff required for native environment")
    contract=(gate["manifest"].get("reuse_contract") or {}).get("climate_time_alignment") or {}
    forcing=Path(str(contract.get("forcing_file") or ""))
    expected=contract.get("forcing_file_sha256")
    if not expected or not forcing.is_file() or hashlib.sha256(forcing.read_bytes()).hexdigest()!=expected:
        raise ValueError("Offline forcing identity required; refresh is forbidden")
    # Environment identity comes from verified handoff/forcing and surveyed
    # mission initialization, never a learned controller policy. Optional-policy
    # integrity is checked separately at OnlineStart before policy consumption.
    region=read_json_auto(config.package_root/"region/region_v1.geojson")
    roles={f.get("properties",{}).get("feature_role"):f for f in region["features"]}
    origin=roles["start"]["geometry"]["coordinates"]
    destination=roles["destination"]["geometry"]["coordinates"]
    prior=load_mission_prior(config,config.package_root/"reports",origin,destination)
    native=config.get('layers.layer6.online.transport')=='Layer6Client_GUI_MAVLink'
    if native:
        from .phoenix_geometry import validate_native_geometry
        alignment=validate_native_geometry(config)
        if not prior.get('ground_geometry') or prior['ground_geometry']['revision']!=alignment['revision']:
            raise ValueError('mission/native ground geometry revision mismatch')
    terrain_msl=float(prior["endpoint_ground_msl_m"]["origin"])
    cg_height=float((prior.get('ground_geometry') or {}).get('cg_height_agl_m',0.))
    known={"latitude_deg":float(origin[1]),"longitude_deg":float(origin[0]),
           "altitude_msl_m":terrain_msl+cg_height,
           "terrain_elevation_msl_m":terrain_msl,"cg_height_agl_m":cg_height,
           "heading_deg":math.degrees(prior["mission_heading_rad"])%360.,
           "map_crs":prior["map_crs"],"initialization_only":True,"gnss_telemetry_used":False}
    binding={"schema":"TDOC-native-environment-binding-v1","binding_id":uuid.uuid4().hex,
             "readiness_scope":"ENVIRONMENT_BINDING_ONLY; not flight/control readiness",
             "policy_required_for_binding":False,"actuation_authority":False,
             "handoff_hash":gate["manifest"]["handoff_hash"],"forcing_file":str(forcing.resolve()),
             "forcing_file_sha256":expected,"effective_climate_epoch":contract["effective_climate_epoch"],
             "plant_epoch_s":float(plant_epoch_s),"mission_origin":known,
             "navigation_authority":"MESA_STD","gps_role":"validation_reference_only",
             "ground_geometry":prior.get('ground_geometry'),
             "ground_contact_policy":{"revision":ground_policy['revision'],
                                      "fixture_supported":False,"actuation_authority":False}}
    if native:
        from .model_compatibility import capture_model_dependencies
        binding['model_dependencies']=capture_model_dependencies(config)
    target=Path(output_path) if output_path is not None else config.path_value("TDDT_LAYER6_ROOT",required=True)/"runtime/tdoc_environment_binding.json"
    target.parent.mkdir(parents=True,exist_ok=True)
    tmp=target.with_suffix(".tmp");tmp.write_text(json.dumps(binding,allow_nan=False)+"\n");os.replace(tmp,target)
    return binding
