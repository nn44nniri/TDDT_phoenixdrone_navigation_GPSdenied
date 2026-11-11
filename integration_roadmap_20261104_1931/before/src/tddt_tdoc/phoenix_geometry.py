"""Reference-bound ground/CG/datum alignment, without flight authorization."""
import hashlib
import math
from pathlib import Path
import yaml
from .phoenix_reference import inspect_reference

ALIGNMENT_MANIFEST_SHA256='402c4e0a9366065996ad06dbbcda4e9101213677c6539dc190172d257983a386'


def aligned_root():
    return Path(__file__).resolve().parent/'resources/phoenix_fourfoot/r3_02_v1'


def geometry_profile(config):
    from twins_fmi.ground_geometry import GroundGeometry
    root=config.path_value('TDDT_TWINS_ROOT',required=True)
    from .model_compatibility import read_safe_yaml
    profile=read_safe_yaml(root/'config/phoenix_drone_v3.yaml')
    geometry=GroundGeometry(profile)
    reference=inspect_reference()
    if not reference['ok'] or geometry.data.get('reference_aircraft_sha256')!=reference['files']['aircraft']['sha256']:
        raise ValueError('four-foot reference identity mismatch')
    if geometry.data['cg_structural_m']!=reference['CG']['xyz']:
        raise ValueError('reference CG mismatch')
    if profile['mass']['empty_or_flight_mass_kg']!=reference['mass']['value']:
        raise ValueError('reference model mass mismatch')
    if profile['inertia']['inertial_pose_xyz_m']!=[0.,0.,0.] or 'BODY_FRD_CG' not in profile['inertia']['frame']:
        raise ValueError('canonical inertia/CG frame mismatch')
    for contact,ref in zip(geometry.contacts,reference['contacts']):
        if contact['structural_m']!=ref['location']['xyz']:
            raise ValueError('reference foot geometry mismatch')
    return profile,geometry


def initialization_geometry(config,q):
    profile,geometry=geometry_profile(config)
    height=geometry.height_agl(q)
    override=config.get('tdoc.online_guidance.ground_cg_height_m',None)
    if override is not None and (not math.isfinite(float(override)) or abs(float(override)-height)>1e-9):
        raise ValueError('ground_cg_height_m contradicts four-foot geometry')
    return {'revision':geometry.data['revision'],'reference_aircraft_sha256':geometry.data['reference_aircraft_sha256'],
        'cg_height_agl_m':height,'foot_clearances_m':geometry.clearances(height,q),
        'position_origin':'CG','body_frame':'BODY_FRD_CG','navigation_frame':'NED',
        'vertical_equations':'CG_MSL=terrain_MSL+CG_AGL; pd=-CG_MSL',
        'native_altitude_export':'terrain_home_MSL + JSBSim_AGL',
        'terrain_vertical_datum_verified':False,'actuation_authority':False,'ground_load_bearing_verified':False}


def validate_native_geometry(config):
    """Check installed geometry; dynamic Reset origin binding belongs to R4."""
    import json
    raw=(aligned_root()/'ALIGNMENT_MANIFEST.json').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=ALIGNMENT_MANIFEST_SHA256:
        raise ValueError('aligned geometry manifest hash mismatch')
    manifest=json.loads(raw)
    target=config.path_value('TDDT_LAYER6_ROOT',required=True)/'FlightStack/ardupilot/Tools/autotest/aircraft/phoenix_v3'
    for name in ('phoenix_v3.xml',):
        p=target/name
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=manifest['files'][name]['sha256']:
            raise ValueError('native ground geometry deployment mismatch; R3.03 required: '+name)
    reset=target/'reset.xml'
    if not reset.is_file():
        raise ValueError('native Reset geometry missing')
    # Native SITL regenerates lat/lon/yaw at startup. Compare its fixed geometry
    # fields exactly with the pinned template, while explicitly withholding
    # origin/forcing readiness for these dynamic fields until the R4 binding.
    import xml.etree.ElementTree as ET
    template_path=aligned_root()/'reset.xml'
    if hashlib.sha256(template_path.read_bytes()).hexdigest()!=manifest['files']['reset.xml']['sha256']:
        raise ValueError('aligned Reset template hash mismatch')
    try:
        actual=ET.parse(reset).getroot();template=ET.parse(template_path).getroot()
        if actual.tag!=template.tag or actual.attrib!=template.attrib or len(actual)!=len(template):
            raise ValueError('native Reset schema mismatch')
        for a,b in zip(actual,template):
            if a.tag!=b.tag or a.attrib!=b.attrib or len(a):
                raise ValueError('native Reset field contract mismatch')
            value=float(a.text)
            if not math.isfinite(value):raise ValueError('non-finite native Reset field')
            if a.tag in ('latitude','longitude','psi'):
                limit={'latitude':90.,'longitude':180.,'psi':360.}[a.tag]
                if abs(value)>limit:raise ValueError('native Reset origin range mismatch')
            elif (a.text or '').strip()!=(b.text or '').strip():
                raise ValueError('native Reset geometry value mismatch')
    except (ET.ParseError,TypeError) as exc:
        raise ValueError('native Reset geometry invalid') from exc
    bridge=config.path_value('TDDT_LAYER6_ROOT',required=True)/'FlightStack/ardupilot/libraries/SITL/SIM_JSBSim.cpp'
    # R6.04 changes the rate transport; its exact Reset generator is unchanged.
    supported={manifest['native_bridge_after_sha256'], 'c266189df071f2c3e7eb857e9173fa1f7167529fa2b8f8c2ac396e20b558d192'}
    if not bridge.is_file() or hashlib.sha256(bridge.read_bytes()).hexdigest() not in supported:
        raise ValueError('native generated Reset geometry mismatch; R3.03 rebuild required')
    # Runtime SIM_JSBSim regenerates reset.xml. Its source/binary update is owned
    # by R3.03; matching an on-disk template alone never grants flight authority.
    return {**manifest,'installed_reset_sha256':hashlib.sha256(reset.read_bytes()).hexdigest(),
            'reset_origin_binding_verified':False,'flight_ready':False}
