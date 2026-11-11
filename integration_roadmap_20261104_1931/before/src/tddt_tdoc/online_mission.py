"""Offline reference -> Online objectives, never Online realized state or authority.

A known surveyed start and its DEM altitude may initialize navigation once. All
subsequent positions must come from MESA. Missing phase evidence is not fabricated.
"""
from __future__ import annotations
import csv
import hashlib
import math
from pathlib import Path
from typing import Any, Mapping


def validate_native_origin(status, native_session, origin, tolerance_m):
    """Validate the physical launch site without supplying a navigation state.

    The FCU position is a commissioning reference only. MESA continues to own
    navigation; estimator drift during cold prediction cannot move the launch
    site or be hidden by copying this reference into the estimator.
    """
    from layer6.status import validate_observation_status
    from .native_control_binding import ground_contact
    result = {"verified": False, "distance_m": None, "position": None,
              "source": "Layer6/GUI/MAVLink/GLOBAL_POSITION_INT",
              "role": "launch_site_validation_only", "navigation_input": False}
    try:
        if not validate_observation_status(status)["ok"]:
            raise ValueError("fresh Native launch observation required")
        sid = (status.get("status_quality") or {}).get("native_session_id")
        if not sid or sid != native_session.get("session_id") or native_session.get("session_ready") is not True:
            raise ValueError("Native launch session mismatch or missing components")
        if status.get("armed") is not False or not ground_contact(status):
            raise ValueError("launch validation requires grounded disarmed FCU")
        lat, lon = float(status["latitude_deg"]), float(status["longitude_deg"])
        goal_lon, goal_lat = map(float, origin[:2])
        tolerance = float(tolerance_m)
        if (not all(math.isfinite(v) for v in (lat, lon, goal_lat, goal_lon, tolerance))
                or abs(lat) > 90 or abs(goal_lat) > 90 or abs(lon) > 180 or abs(goal_lon) > 180 or tolerance <= 0):
            raise ValueError("invalid launch coordinates/tolerance")
        a, b = math.radians(lat), math.radians(goal_lat)
        h = math.sin((b-a)/2)**2 + math.cos(a)*math.cos(b)*math.sin(math.radians(goal_lon-lon)/2)**2
        distance = 2*6371008.8*math.asin(math.sqrt(min(1., max(0., h))))
        result.update(verified=distance <= tolerance, distance_m=distance,
                      position={"lat": lat, "lon": lon}, native_session_id=sid,
                      plant_time_s=status["plant_time_s"],
                      reason="surveyed_origin_confirmed" if distance <= tolerance else "physical_launch_site_mismatch")
    except (ValueError, TypeError, KeyError) as exc:
        result["reason"] = str(exc)
    return result


def load_mission_prior(config, report_root: Path, origin, destination) -> dict[str, Any]:
    import rasterio
    from pyproj import Transformer
    from tddt_tdoc.terrain import sample_bilinear
    path=Path(report_root)/'offline/mission/MISSION_TIMELINE.csv'
    if not path.is_file():
        raise FileNotFoundError(f'Online mission reference missing: {path}; restore the supplied Offline reports')
    phases={}
    with path.open(newline='',encoding='utf-8') as stream:
        for row in csv.DictReader(stream):
            phase=str(row.get('phase') or '')
            xyz=[float(row[k]) for k in ('reference_x_m','reference_y_m','reference_z_msl_m')]
            if not all(math.isfinite(v) for v in xyz):
                raise ValueError('Non-finite Offline mission reference')
            phases.setdefault(phase,[]).append(xyz)
    if not phases.get('on_ground') or not phases.get('vertical_takeoff'):
        raise ValueError('Offline prior lacks on_ground/vertical_takeoff reference; cannot initialize mission')
    raster=config.path_value('TDDT_MAP_ROOT',required=True)/'terrain_obstacles.tif'
    with rasterio.open(raster) as dem:
        tx=Transformer.from_crs('EPSG:4326',dem.crs,always_xy=True)
        start_e,start_n=tx.transform(float(origin[0]),float(origin[1]))
        goal_e,goal_n=tx.transform(float(destination[0]),float(destination[1]))
        try:
            start_h=sample_bilinear(dem,start_e,start_n)
            goal_h=sample_bilinear(dem,goal_e,goal_n)
        except ValueError as exc:
            raise ValueError('Mission endpoint outside valid terrain map') from exc
        crs=str(dem.crs)
    # Mission bearing is a configured initial compass attitude, not GNSS telemetry.
    lat1,lat2=map(math.radians,[float(origin[1]),float(destination[1])])
    dl=math.radians(float(destination[0])-float(origin[0]))
    yaw=math.atan2(math.sin(dl)*math.cos(lat2),math.cos(lat1)*math.sin(lat2)-math.sin(lat1)*math.cos(lat2)*math.cos(dl))
    # Native SIM_JSBSim initializes Phoenix at 89 degrees, not the ideal
    # vertical 90-degree Offline pose. This is a known launch condition only;
    # no FCU/GNSS attitude is injected into the navigation estimator.
    native=config.get('layers.layer6.online.transport')=='Layer6Client_GUI_MAVLink'
    pitch=math.radians(89. if native else 90.)
    cp=math.cos(pitch/2);sp=math.sin(pitch/2);cy=math.cos(yaw/2);sy=math.sin(yaw/2)
    from .phoenix_geometry import initialization_geometry
    ground_geometry=initialization_geometry(config,(cp*cy,-sp*sy,sp*cy,cp*sy))
    cg_height=ground_geometry['cg_height_agl_m']
    init={'pn':start_n,'pe':start_e,'pd':-(start_h+cg_height),'vn':0.,'ve':0.,'vd':0.,
          'qw':cp*cy,'qx':-sp*sy,'qy':sp*cy,'qz':cp*sy,
          'initial_pitch_deg':math.degrees(pitch),
          'source':'mission_known_origin+DEM+tail_contact_geometry+configured_Phoenix_tailsitter_attitude',
          'ground_cg_height_m':cg_height,'ground_geometry_revision':ground_geometry['revision'],
          'map_crs':crs,'initialization_only':True,'gnss_telemetry_used':False}
    return {'schema':'TDOC-online-mission-prior-v3','phases':phases,'initial_state':init,
            'mission_heading_rad':float(yaw),'ground_geometry':ground_geometry,
            'goal_projected_enu_m':[goal_e,goal_n,goal_h],'source_reference_origin':phases['on_ground'][0],
            'endpoint_ground_msl_m':{'origin':start_h,'destination':goal_h},
            'reference_file':str(path),'reference_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'map_crs':crs,'terrain_interpolation':'BILINEAR_PIXEL_CENTRE',
            'source_is_reference_only':True,'mission_certified':False,
            'truth_injection_to_mesa':False}


def apply_mission_prior(request: dict, prior: Mapping, navigation: Mapping) -> None:
    """Select nearby phase targets from reference geometry, without touching sensors/state."""
    init=prior['initial_state'];base_h=float(prior['source_reference_origin'][2])
    pos=[float(navigation[k]) for k in ('pe','pn')]+[-float(navigation['pd'])]
    phase_targets={};phase_target_agl={}
    for phase,rows in prior['phases'].items():
        refs=[list(map(float,row)) for row in rows]
        if phase in ('on_ground','vertical_takeoff','hover'):
            refs=[[float(init['pe']),float(init['pn']),-float(init['pd'])+(r[2]-base_h)] for r in refs]
        if phase in ('vertical_landing','touchdown'):
            refs=[[float(prior['goal_projected_enu_m'][0]),float(prior['goal_projected_enu_m'][1]),r[2]] for r in refs]
        nearest=min(range(len(refs)),key=lambda i:sum((refs[i][j]-pos[j])**2 for j in range(3)))
        phase_targets[phase]=refs[min(nearest+1,len(refs)-1)]
        endpoint=('origin' if phase in ('on_ground','vertical_takeoff','hover') else
                  'destination' if phase in ('vertical_landing','touchdown') else None)
        if endpoint is not None:
            ground=float(prior['endpoint_ground_msl_m'][endpoint])
            phase_target_agl[phase]=float(phase_targets[phase][2])-ground
    ctx=request.setdefault('context',{})
    ctx['online_phase_reference_targets']=phase_targets
    ctx['online_phase_reference_target_agl_m']=phase_target_agl
    ctx['online_mission_reference']={k:prior[k] for k in ('schema','reference_file','reference_sha256','map_crs','source_is_reference_only','truth_injection_to_mesa')}
    # A tailsitter's vertical attitude is singular in Euler pitch.  Carry the
    # configured mission-bearing launch quaternion as a reference contract so
    # Layer5 can form a continuous quaternion error through +/-90 degrees.  This
    # is an initialization/reference value only: it is not a realized-state
    # injection and it has no actuation authority.
    ctx['vertical_attitude_reference']={
        'schema':'TDOC-Phoenix-vertical-attitude-reference-v1',
        'quaternion_wxyz':[float(init[k]) for k in ('qw','qx','qy','qz')],
        'mission_heading_rad':float(prior['mission_heading_rad']),
        'frame':'NED','source':'configured_mission_bearing+Phoenix_tailsitter_geometry',
        'initialization_reference_only':True,'actuation_authority':False,
        'truth_injection_to_mesa':False,
    }
    ctx['mission_destination_projected_enu_m']=list(prior['goal_projected_enu_m'])
    ctx['sarg_reference_only']=True;ctx['teacher_forced_reference_replay']=False
    bind_phase_reference(request,str(request.get('stage') or 'on_ground'))


def bind_phase_reference(request: dict, phase: str) -> None:
    ctx=request.setdefault('context',{});targets=ctx.get('online_phase_reference_targets') or {}
    reference_phase='vertical_takeoff' if phase=='on_ground' else phase
    target=targets.get(reference_phase)
    # A request may be rebound as the flight phase changes.  Never let the AGL
    # objective from the preceding phase survive into a phase with no contract.
    ctx.pop('phase_target_agl_m',None)
    if target is None:
        return  # Missing phase coverage remains visible to the existing release gates.
    ctx['target_projected_enu_m']={'value':list(target),'unit':'m','frame':'PROJECTED_ENU',
        'semantic':'target_position_absolute','uncertainty':None,'source':'Offline_reference/OnlineMissionPrior',
        'provenance':{'reference_sha256':(ctx.get('online_mission_reference') or {}).get('reference_sha256'),
                      'reference_only':True,'not_realized_next_state':True,'phase':phase}}
    agl_targets=ctx.get('online_phase_reference_target_agl_m') or {}
    # ``phase_target_agl_m`` is an altitude-hold contract.  Publishing a
    # takeoff waypoint through this key would make Layer4's short prediction
    # horizon treat waypoint completion as a per-horizon feasibility floor.
    # Takeoff remains governed by its ground-referenced non-penetration
    # corridor; the explicit AGL target is materialized only for hover.
    if reference_phase == 'hover' and reference_phase in agl_targets:
        ctx['phase_target_agl_m']=float(agl_targets[reference_phase])
    # Never retain a local-ENU example goal alongside an absolute map objective.
    ctx.pop('target_enu_m',None)


def bind_pending_navigation_initialization(context, layer2_adapter, episode_id, initial_state):
    """Keep mission-known initialization available until MESA actually consumes it.

    A failed native telemetry ingress must not consume the one-time episode seed.
    No execution/GPS state is used and an initialized estimator is never re-anchored.
    """
    if initial_state and getattr(layer2_adapter, '_online_mesa_initialized_episode', None) != episode_id:
        context['rtmo_navigation_initial_state'] = dict(initial_state)
        context['rtmo_allow_initial_navigation_alignment'] = True
        return True
    context.pop('rtmo_navigation_initial_state', None)
    context.pop('rtmo_allow_initial_navigation_alignment', None)
    return False
