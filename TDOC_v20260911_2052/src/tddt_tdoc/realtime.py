"""TDOC-owned asynchronous realtime VTOL mission using only approved sensors.

Heavy import/dataset work precedes ARM. Layer6 keeps the current short lease at
30Hz while TDOC replans at 10Hz. Layer7 matches the original prediction tube at
the measured plant time and returns model-only feedback to the next Layer2 tick.
"""
from copy import deepcopy
from datetime import datetime,timedelta
import hashlib,json,math,sys,threading,time,traceback,uuid
from pathlib import Path
from types import SimpleNamespace
import numpy as np
from pyproj import Geod

from .adapters.layer2 import Layer2Adapter
from .adapters.layer5 import Layer5Adapter
from .adapters.layer6 import Layer6OnlineAdapter
from .model_compatibility import capture_model_dependencies
from .knowledge_handoff import OfflineKnowledgeHandoff


class ContactSupport:
    """Latch barometer/IMU support evidence through motor-cut transients.

    Commanded slow descent is not a stall. No pose from the physical simulator
    enters this detector, including its near-ground impact alternative.
    """
    def __init__(self):self.samples=[];self.certificate=None
    def update(self,t,height,down_velocity,force_error):
        if self.certificate is not None:return self.certificate
        self.samples.append((t,height,down_velocity,force_error))
        self.samples=[s for s in self.samples if t-s[0]<=.7]
        recent=[s for s in self.samples if t-s[0]<=.4]
        span=recent[-1][0]-recent[0][0]
        stationary=(span>=.3 and max(s[1] for s in recent)<.2
            and max(s[1] for s in recent)-min(s[1] for s in recent)<.04
            and all(abs(s[2])<.08 and s[3]<.5 for s in recent))
        impact=(height<.15 and force_error>1.2
            and any(s[2]>.1 and s[1]>.08 for s in self.samples[:-1]))
        if stationary or impact:
            self.certificate={'qualified':True,'qualified_time_s':t,
                'condition':'barometric_stall' if stationary else 'near_ground_IMU_impact',
                'height_m':height,'window_s':span,'specific_force_error_m_s2':force_error,
                'source':'MESA/barometer/IMU only'}
        return self.certificate


class ClimatePrediction:
    def __init__(self,cfg,binding,initial_time):
        self.cfg=cfg;self.binding=binding;self.point=(0.,0.,0.,initial_time)
        self.value=None;self.error=None;self.stop=False
        self.calculate();self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
    def calculate(self):
        from estimate_climate import estimate
        n,e,d,t=self.point;o=self.binding['mission_origin'];geod=Geod(ellps='WGS84')
        lon,lat,_=geod.fwd(o['longitude_deg'],o['latitude_deg'],math.degrees(math.atan2(e,n)),math.hypot(n,e))
        epoch=datetime.fromisoformat(self.binding['effective_climate_epoch'])
        args=SimpleNamespace(fmu=str(self.cfg.path_value('TDDT_CLIMATE_ROOT')/'fmu/MicroMetTwin.fmu'),
            input=self.binding['forcing_file'],time=(epoch+timedelta(seconds=t)).isoformat(),
            latitude=lat,longitude=lon,elevation_sea_level=float(o['altitude_msl_m'])-d,
            elevation_ground_level=None,dem=str(self.cfg.path_value('TDDT_MAP_ROOT')/'terrain_obstacles.tif'),
            dem_dir=None,terrain_policy='require-dem',curvature_radius_m=1000.,ground_elevation_m=None,
            slope_deg=0.,aspect_deg=0.,curvature=0.,barnes_filter_km=20.,with_metadata=False,
            wind_3dw_dx_m=100.,wind_3dw_dy_m=100.,wind_3dw_dz_m=10.,wind_profile_power=.143)
        est=estimate(args)
        if est.get('ok') is False:raise ValueError('MicroMet query rejected: '+str(est.get('error')))
        out=est.get('fmu_outputs') or est
        self.value={'twin_id':'MicroMetTwin','time_s':t,'wind_ned_m_s':[
            float(out['wind_v_3dw']),float(out['wind_u_3dw']),-float(out['diagnostic_vertical_wind'])],
            'air_density_kg_m3':float(out['air_density']),'source':'bound cached forcing through MicroMet FMU',
            'forcing_sha256':self.binding['forcing_file_sha256'],'model_only':True,
            'received_monotonic':time.monotonic()}
    def run(self):
        while not self.stop:
            time.sleep(.5)
            if self.stop:break
            try:self.calculate();self.error=None
            except Exception as exc:self.error=repr(exc)
    def close(self):self.stop=True;self.thread.join(timeout=5)


def run_realtime_mission(cfg,*,hover_seconds=None,height_m=3.,max_cycles=20000,goal_override=None,output=None):
    l2=Layer2Adapter(cfg);legacy_l5=Layer5Adapter(cfg);legacy_l5._ensure()
    l6=Layer6OnlineAdapter(cfg,legacy_l5);io=None;climate=None
    l6.readiness_requires_horizontal_position=False
    l6.origin_validation_physical=True
    run_id='rt-'+uuid.uuid4().hex;out=Path(output or cfg.runtime_root/'realtime'/run_id);out.mkdir(parents=True,exist_ok=True)
    result={'schema':'TDOC-realtime-mission-result-v1','status':'FAIL','run_id':run_id,
        'navigation_sensors':['GYROSCOPE','ACCELEROMETER','MAGNETOMETER','BAROMETER','AIRSPEED'],
        'ULTRASONIC_WIND':'MISSING','GNSS_used':False,'odometry_used':False,'vision_used':False,
        'inner_FCU_attitude_backend':'AP_NavEKF3 with IMU/mag/baro; never MESA observation',
        'route_profile':'VTOL translation; no wing-borne phase certification'}
    result['requested_hover_seconds']=hover_seconds
    pending={};next_feedback=None;accepted=0;consumed=0;cycles=0;last_tx=None
    try:
        ready=l6.prepare_online_transport_session()
        if not ready.get('ok'):raise RuntimeError('Native startup: '+str({k:ready.get(k) for k in ('stage','error')}))
        status=l6._status();sid=status['status_quality']['native_session_id'];result['native_session_id']=sid
        from layer6.realtime import RealtimeIO
        from layer6.native_session import session_health
        from layer6.binding import context_guard
        session=ready['native_session'];binding_path=session['binding_file'];guard=context_guard(binding_path);binding=guard.check()
        result['binding_hash']=binding['binding_hash'];result['model_id']=binding['model_id']
        io=RealtimeIO(sid,out/'telemetry_wire.jsonl')
        names=['SIM_GPS1_ENABLE','SIM_GPS2_ENABLE','SIM_GPS3_ENABLE','SIM_GPS4_ENABLE',
            'GPS1_TYPE','GPS2_TYPE','AHRS_EKF_TYPE','Q_A_ANGLE_MAX','Q_PILOT_SPD_UP','Q_PILOT_SPD_DN',
            'Q_TAILSIT_INPUT','RC1_DZ','RC2_DZ','RC3_DZ','EK3_SRC1_POSXY','EK3_SRC1_VELXY',
            'EK3_SRC1_POSZ','EK3_SRC1_VELZ','EK3_SRC1_YAW']
        readbacks=io.request_parameters(names);parameters={k:v['value'] for k,v in readbacks.items()}
        if any(parameters[n]!=0 for n in names[:6]):raise ValueError('GNSS receiver/simulation readback not disabled')
        if parameters['AHRS_EKF_TYPE']!=3 or [parameters[n] for n in names[-5:]]!=[0.,0.,1.,0.,1.]:
            raise ValueError('FCU sensor-only EKF3 configuration not verified')
        result['parameter_readbacks']=readbacks
        result['surveyed_origin_frame']=io.set_surveyed_origin(binding['mission_origin'])
        l2._ensure()
        from twins_fmi.realtime import RealtimeTwins
        from layer3.realtime import atomic_snapshot
        from layer4.realtime import predict
        from layer5.realtime import decide
        from tddt_layer7.realtime import feedback
        origin=binding['mission_origin'];initial_time=0.
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            try:measurements,samples,validation=io.sample(-1.);initial_time=samples[-1]['plant_time_s'];break
            except RuntimeError:time.sleep(.05)
        else:raise RuntimeError('approved sensor channels unavailable')
        climate=ClimatePrediction(cfg,binding,initial_time)
        after=initial_time;calibration=[];cal_start=initial_time
        calibration_duration=float(cfg.get('tdoc.realtime.stationary_calibration_s',60.))
        if not 3.<=calibration_duration<=300.:raise ValueError('stationary calibration duration outside 3..300s')
        while after-cal_start<calibration_duration:
            measurements,samples,validation=io.sample(after)
            if samples:
                after=samples[-1]['plant_time_s'];calibration.extend(samples)
                climate.point=(0.,0.,0.,after)
            if validation['HEARTBEAT']['data']['base_mode']&128:raise ValueError('stationary initialization requires DISARMED')
            time.sleep(.05)
        twins=RealtimeTwins(l2,origin,calibration,climate.value)
        result['calibration']=twins.calibration
        geod=Geod(ellps='WGS84')
        region=json.loads((cfg.package_root/'region/region_v1.geojson').read_text())
        goal=next(f['geometry']['coordinates'] for f in region['features'] if f['properties'].get('feature_role')=='destination')
        if goal_override is not None:goal=list(goal_override)
        if hover_seconds is not None:goal=[origin['longitude_deg'],origin['latitude_deg']]
        bearing,_,distance=geod.inv(origin['longitude_deg'],origin['latitude_deg'],goal[0],goal[1])
        target=np.array([distance*math.cos(math.radians(bearing)),distance*math.sin(math.radians(bearing)),0.])
        altitude=float(height_m if hover_seconds is not None else cfg.get('tdoc.realtime.route_height_m',10.))
        route_speed=float(cfg.get('tdoc.realtime.route_speed_m_s',4.))
        result['goal_lonlat']=goal;result['route_distance_m']=distance;result['cruise_height_above_launch_m']=altitude
        arm_health=session_health(sid)
        if not arm_health['ok'] or not arm_health['panels']['evidence'].get('physical_map',{}).get('ready'):
            raise RuntimeError('six live panels including physical simulator map required before ARM')
        # Separate lifecycle command before any climb/lean command. No force-arm.
        client=l6._get_client();arm_start=time.monotonic();last_arm=0.
        while time.monotonic()-arm_start<12.:
            if time.monotonic()-last_arm>.4:
                client.submit_command([0.,0.,0.,0.],mode='QHOVER',discrete=('ARM',),ttl_s=.8,
                    transaction_id=run_id+'-arm',provenance={'execution_scope':'SITL_RT_MPC_LIFECYCLE'})
                last_arm=time.monotonic()
            measurements,samples,validation=io.sample(twins.last)
            twins.step(measurements,samples)
            if validation['HEARTBEAT']['data']['base_mode']&128:break
            time.sleep(.08)
        else:raise RuntimeError('normal FCU ARM not confirmed: '+str(list(io.text)))
        twins.takeoff_requested=True
        phase='vertical_takeoff';hover_start=None;ground_since=None;support=ContactSupport();phase_start=twins.last
        next_tick=time.monotonic();health_at=0.;health=None;last_print=0.;max_latency=0.
        (out/'startup.json').write_text(json.dumps(result,indent=2))
        with (out/'cycles.jsonl').open('w',buffering=1) as log:
            while cycles<max_cycles:
                cycle_start=time.monotonic();cycles+=1;tx=run_id+f'-c{cycles:06d}'
                measurements,samples,validation=io.sample(twins.last)
                if time.monotonic()-measurements['HIGHRES_IMU']['received_monotonic']>.3:
                    raise ValueError('source observation stale before control')
                if next_feedback is not None:consumed+=1
                nav=twins.step(measurements,samples,next_feedback);next_feedback=None
                p=np.array(nav['position_ned_m']);v=np.array(nav['velocity_ned_m_s']);height=-p[2]
                climate.point=(*p,nav['time_s'])
                current_climate=deepcopy(climate.value)
                if (time.monotonic()-current_climate['received_monotonic']>3.
                    or not 0<=nav['time_s']-current_climate['time_s']<=3.):
                    raise RuntimeError('MicroMet prediction stale: '+str(climate.error))
                if time.monotonic()-health_at>1.:
                    health=session_health(sid);health_at=time.monotonic()
                    if not health['ok']:raise RuntimeError('Native six-panel health: '+str(health['reason']))
                    if not health['panels']['evidence'].get('physical_map',{}).get('ready'):
                        raise RuntimeError('physical simulator map telemetry unavailable')
                    if any(io.parameters[n]['value']!=0 for n in names[:6]):raise ValueError('GNSS configuration changed')
                if phase=='vertical_takeoff' and height>altitude-.5 and abs(v[2])<.5:
                    phase='hover' if hover_seconds is not None else 'cruise_vtol';phase_start=nav['time_s'];hover_start=None
                if phase=='hover':
                    qualified=bool(np.linalg.norm(p[:2])<=5 and abs(height-altitude)<=1 and np.linalg.norm(v)<1.)
                    if not qualified:hover_start=None
                    elif hover_start is None:hover_start=nav['time_s']
                    if hover_start is not None and nav['time_s']-hover_start>=hover_seconds+3.:
                        phase='vertical_landing';phase_start=nav['time_s']
                if phase=='cruise_vtol' and np.linalg.norm(p[:2]-target[:2])<3 and np.linalg.norm(v[:2])<.8:
                    phase='vertical_landing';phase_start=nav['time_s']
                imu_data=measurements['HIGHRES_IMU']['data']
                specific_force_error=abs(np.linalg.norm([imu_data[k] for k in ('xacc','yacc','zacc')])-9.80665)
                if phase=='vertical_landing' and height<1.5:
                    phase='landing_flare';phase_start=nav['time_s']
                if phase in ('vertical_landing','landing_flare'):
                    if support.update(nav['time_s'],height,v[2],specific_force_error):
                        phase='contact_settle';phase_start=nav['time_s']
                ref=target.copy() if phase in ('cruise_vtol','vertical_landing','landing_flare','contact_settle') else np.zeros(3)
                ref[2]=.8 if phase in ('vertical_landing','landing_flare','contact_settle') else -altitude
                reference={'position_ned_m':ref.tolist(),'speed_m_s':route_speed if phase=='cruise_vtol' else 2.}
                if phase=='contact_settle':
                    reference['contact_evidence']=deepcopy(support.certificate)
                twins.climate=deepcopy(current_climate)
                wing=twins.wing_packet(nav,(validation.get('SERVO_OUTPUT_RAW') or {}).get('data') or {})
                snapshot=atomic_snapshot(nav,wing,current_climate,phase=phase,
                    reference=reference,
                    transaction_id=tx,origin=origin,feedback_hash=nav['feedback_consumed'])
                forecast=predict(snapshot);decision=decide(snapshot,forecast,parameters,
                    heading_reference=math.radians(origin['heading_deg']))
                elapsed=time.monotonic()-measurements['HIGHRES_IMU']['received_monotonic']
                if elapsed>.3:raise ValueError('source observation expired before MPC submission')
                # New declared bounded-lease protocol: the 2s prediction tube
                # covers the 0.5s command lease. The old 0.1s segment contract is
                # not relabelled or silently extended.
                ack=client.submit_command(decision['u_cmd'],mode=decision['mode'],transaction_id=tx,ttl_s=.5,
                    provenance={'execution_scope':'SITL_RT_MPC_V1','snapshot_hash':snapshot['snapshot_hash'],
                        'candidate_id':decision['candidate_id'],'model_id':binding['model_id']},
                    safety={'psf':True,'rta':True},execution_rate_hz=30.)
                last_tx=tx;decision['intake_instance_id']=ack.raw['command_instance_id'];pending[tx]=decision
                twins.model['last_command_acceleration_ne_m_s2']=decision['prediction']['acceleration_ne_m_s2']
                receipt=io.receipt();f=feedback(nav,receipt,pending,twins.model)
                if f['ok']:accepted+=1;next_feedback=f
                if cycles>100 and accepted<2:raise RuntimeError('two qualified Layer7 feedback cycles were not produced')
                for key in list(pending):
                    if nav['time_s']-pending[key]['source_time_s']>2.:pending.pop(key)
                landed=(validation.get('EXTENDED_SYS_STATE') or {}).get('data',{}).get('landed_state')==1
                if phase=='contact_settle' and landed and height<.8 and abs(v[2])<.4:
                    if ground_since is None:ground_since=nav['time_s']
                else:ground_since=None
                complete=ground_since is not None and nav['time_s']-ground_since>=2.
                record={'transaction_id':tx,'phase':phase,'L2':{'navigation':nav,'wing':wing,'climate':current_climate},
                    'L3':snapshot,'L4':forecast,'L5':decision,'L6':{'intake':ack.raw,'execution_receipt':receipt},
                    'L7':f,'external_validation_only':{k:validation.get(k) for k in
                        ('GLOBAL_POSITION_INT','ATTITUDE','ATTITUDE_QUATERNION','SIM_STATE','GPS_RAW_INT','EXTENDED_SYS_STATE')},
                    'health':{'ok':health['ok'],'session_id':sid,'panels_ready':health['panels_ready'],
                        'physical_map':health['panels']['evidence']['physical_map']},
                    'cycle_wall_s':time.monotonic()-cycle_start,'accepted_feedback':accepted,'consumed_feedback':consumed}
                log.write(json.dumps(record,separators=(',',':'),allow_nan=False)+'\n')
                max_latency=max(max_latency,record['cycle_wall_s'])
                if time.monotonic()-last_print>1.:
                    print(f"[RT {cycles:05}] {phase} t={nav['time_s']:.2f} h={height:.2f} NE={p[0]:.2f}/{p[1]:.2f} v={np.linalg.norm(v):.2f} L7={accepted}/{consumed} u={np.round(decision['u_cmd'],3).tolist()} cycle={record['cycle_wall_s']:.3f}s",flush=True)
                    last_print=time.monotonic()
                if complete:
                    for _ in range(10):
                        client.submit_command([0.,0.,0.,0.],mode='QSTABILIZE',discrete=('DISARM',),ttl_s=.5,
                            transaction_id=run_id+'-disarm',provenance={'execution_scope':'SITL_RT_MPC_LIFECYCLE'})
                        time.sleep(.2)
                        _,_,validation=io.sample(twins.last)
                        if not validation['HEARTBEAT']['data']['base_mode']&128:break
                    else:raise RuntimeError('normal DISARM not confirmed')
                    final=(validation.get('SIM_STATE') or {}).get('data') or {}
                    if not final:raise RuntimeError('independent physical validation unavailable')
                    if not final.get('lon_int') or not final.get('lat_int'):
                        raise RuntimeError('high-precision MAVLink2 physical validation unavailable')
                    lon,lat=final['lon_int']*1e-7,final['lat_int']*1e-7
                    _,_,actual_error=geod.inv(lon,lat,goal[0],goal[1])
                    result.update(actual_goal_error_m=actual_error,touchdown_confirmed=True,
                        disarm_confirmed=True,estimated_goal_error_m=float(np.linalg.norm(p[:2]-target[:2])),
                        status='PASS' if actual_error<=25 and accepted>=2 and consumed>=2 else 'FAIL')
                    # Retain the same live stream/windows for independent
                    # DISARM observation and capture before owned-session close.
                    time.sleep(2.)
                    break
                if nav['time_s']-phase_start>900:raise RuntimeError('phase timeout')
                next_tick+=.1;delay=next_tick-time.monotonic()
                if delay>0:time.sleep(delay)
                elif delay<-.2:next_tick=time.monotonic()
            else:raise RuntimeError('mission cycle cap reached')
        result.update(cycles=cycles,accepted_feedback=accepted,consumed_feedback=consumed,max_cycle_wall_s=max_latency)
        guard.check()
    except (Exception,KeyboardInterrupt) as exc:
        result['error']=repr(exc);result['traceback']=traceback.format_exc();traceback.print_exc()
    finally:
        if io:io.close()
        if climate:climate.close()
        l6.close();legacy_l5.close();l2.close()
        result.update(cycles=cycles,accepted_feedback=accepted,consumed_feedback=consumed)
        (out/'MISSION_RESULT.json').write_text(json.dumps(result,indent=2,default=str))
        print('REALTIME_RESULT',out/'MISSION_RESULT.json',result['status'],result.get('error',''),flush=True)
    return result
