"""R3.04 measured native ground-contact acceptance; no control authority."""
import csv
import hashlib
import json
import math
from pathlib import Path

POLICY_SHA256='9d6cb1876fd24e266ea348ae6f1f126f1ca89ff5495768aab27d83d36928d21c'
FT_TO_M=.3048
LBF_TO_N=4.4482216152605
PROPERTIES=(
    'position/h-agl-ft','position/terrain-elevation-asl-ft','position/h-sl-ft',
    'attitude/phi-rad','attitude/theta-rad','attitude/psi-rad',
    'velocities/v-north-fps','velocities/v-east-fps','velocities/v-down-fps',
    'velocities/p-rad_sec','velocities/q-rad_sec','velocities/r-rad_sec',
    'inertia/mass-slugs','accelerations/gravity-ft_sec2',
    'forces/fbx-gear-lbs','forces/fby-gear-lbs','forces/fbz-gear-lbs',
    'fcs/left-motor-cmd-norm','fcs/right-motor-cmd-norm',
    'fcs/left-motor-pos-norm','fcs/right-motor-pos-norm')


def policy():
    path=Path(__file__).resolve().parent/'resources/phoenix_fourfoot/r3_04_v1/GROUND_ACCEPTANCE.json'
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=POLICY_SHA256:
        raise ValueError('ground acceptance policy identity mismatch')
    return json.loads(raw)


def read_samples(path,criteria):
    required={'Time',*PROPERTIES}
    for name in criteria['body_frd_feet_m']:
        required.update(name+' '+suffix for suffix in ('WOW','stroke (ft)','compress force (lbs)'))
    with Path(path).open(newline='') as stream:
        reader=csv.reader(stream)
        try:header=next(reader)
        except StopIteration as exc:raise ValueError('empty native ground trace') from exc
        headers=[h.strip().removeprefix('/fdm/jsbsim/') for h in header]
        if len(headers)!=len(set(headers)) or not required<=set(headers):
            raise ValueError('missing or duplicate native ground channels')
        rows=[]
        for values in reader:
            if len(values)!=len(headers):raise ValueError('malformed native ground row')
            row={key:float(value) for key,value in zip(headers,values) if key in required}
            if not all(math.isfinite(v) for v in row.values()):
                raise ValueError('non-finite native ground sample')
            rows.append(row)
    if len(rows)<2:raise ValueError('insufficient native ground samples')
    return rows


def analyze(path,*,duration_s=30.,settled_from_s=20.):
    criteria=policy();rows=read_samples(path,criteria)
    if not 0<=settled_from_s<duration_s:raise ValueError('invalid evaluation window')
    errors=[]
    def require(condition,code):
        if not condition:errors.append(code)
    times=[row['Time'] for row in rows]
    require(abs(times[0])<1e-9 and abs(times[-1]-duration_s)<.01,'INCOMPLETE_HORIZON')
    require(all(0<b-a<=criteria['max_sample_gap_s'] for a,b in zip(times,times[1:])),'TIME_GAP_OR_REVERSAL')
    require(len(rows)>=.99*duration_s*criteria['log_rate_hz'],'INSUFFICIENT_SAMPLES')
    settled=[row for row in rows if row['Time']>=settled_from_s]
    require(len(settled)>=.99*(duration_s-settled_from_s)*criteria['log_rate_hz'],'INCOMPLETE_SETTLED_WINDOW')
    if not settled:return {'status':'FAIL','violations':errors,'flight_ready':False}
    require(all(row['inertia/mass-slugs']>0 and row['accelerations/gravity-ft_sec2']>0 for row in rows),'INVALID_WEIGHT')
    if 'INVALID_WEIGHT' in errors:return {'status':'FAIL','violations':errors,'flight_ready':False}
    heights=[row['position/h-agl-ft']*FT_TO_M for row in rows]
    tail_heights=[row['position/h-agl-ft']*FT_TO_M for row in settled]
    speeds=[math.sqrt(sum(row['velocities/v-'+a+'-fps']**2 for a in ('north','east','down')))*FT_TO_M for row in settled]
    rates=[math.sqrt(sum(row['velocities/'+a+'-rad_sec']**2 for a in ('p','q','r'))) for row in settled]
    tilts=[math.degrees(math.acos(max(-1.,min(1.,math.sin(row['attitude/theta-rad']))))) for row in settled]
    support=[];weight=[];force_errors=[];normal_errors=[];per_foot={}
    worst_residual=0.;worst_unsupported_penetration=0.
    for row in rows:
        roll=row['attitude/phi-rad'];pitch=row['attitude/theta-rad']
        down=(-math.sin(pitch),math.sin(roll)*math.cos(pitch),math.cos(roll)*math.cos(pitch))
        for name,foot in criteria['body_frd_feet_m'].items():
            compression=row[name+' stroke (ft)']*FT_TO_M
            clearance=row['position/h-agl-ft']*FT_TO_M-sum(a*b for a,b in zip(down,foot))
            corrected=clearance+compression
            worst_unsupported_penetration=max(worst_unsupported_penetration,-corrected)
            wow=row[name+' WOW']
            require(wow in (0.,1.),'INVALID_CONTACT_STATE')
            if wow==1.:worst_residual=max(worst_residual,abs(corrected))
        if row['Time']>=settled_from_s:
            force=[row['forces/fb'+a+'-gear-lbs'] for a in ('x','y','z')]
            actual=-sum(a*b for a,b in zip(down,force))*LBF_TO_N
            expected=row['inertia/mass-slugs']*row['accelerations/gravity-ft_sec2']*LBF_TO_N
            normal=-sum(row[name+' compress force (lbs)'] for name in criteria['body_frd_feet_m'])*LBF_TO_N
            support.append(actual);weight.append(expected)
            force_errors.append(abs(actual/expected-1.));normal_errors.append(abs(normal/expected-1.))
    compression_max=max(row[name+' stroke (ft)']*FT_TO_M for row in rows for name in criteria['body_frd_feet_m'])
    settled_compression_max=max(row[name+' stroke (ft)']*FT_TO_M for row in settled for name in criteria['body_frd_feet_m'])
    for name in criteria['body_frd_feet_m']:
        duty=sum(row[name+' WOW'] for row in settled)/len(settled)
        forces=[-row[name+' compress force (lbs)']*LBF_TO_N for row in settled]
        per_foot[name]={'contact_duty':duty,'mean_compression_force_N':sum(forces)/len(forces),'min_compression_force_N':min(forces)}
        require(duty>=criteria['all_four_contact_duty_min'] and min(forces)>0,'UNSUPPORTED_FOOT:'+name)
    north=east=drift=0.
    for a,b in zip(settled,settled[1:]):
        dt=b['Time']-a['Time']
        north+=(a['velocities/v-north-fps']+b['velocities/v-north-fps'])*.5*dt*FT_TO_M
        east+=(a['velocities/v-east-fps']+b['velocities/v-east-fps'])*.5*dt*FT_TO_M
        drift=max(drift,math.hypot(north,east))
    motor_peak=max(abs(row[prop]) for row in rows for prop in PROPERTIES if prop.startswith('fcs/'))
    require(motor_peak<=criteria['max_motor_input_abs'],'MOTOR_NOT_ZERO')
    require(min(heights)>=criteria['min_CG_AGL_m'],'CG_COLLAPSE')
    require(max(speeds)<=criteria['max_settled_speed_m_s'],'NOT_SETTLED_SPEED')
    require(max(rates)<=criteria['max_settled_body_rate_rad_s'],'NOT_SETTLED_RATE')
    require(max(tilts)<=criteria['max_settled_tilt_deg'],'POSE_TILT')
    require(max(tail_heights)-min(tail_heights)<=criteria['max_settled_AGL_range_m'],'AGL_UNSTABLE')
    require(drift<=criteria['max_settled_horizontal_drift_m'],'GROUND_DRIFT')
    require(compression_max<=criteria['max_transient_compression_m'] and settled_compression_max<=criteria['max_settled_compression_m'],'EXCESS_COMPRESSION')
    require(worst_unsupported_penetration<=criteria['max_deformation_geometry_residual_m'] and worst_residual<=criteria['max_deformation_geometry_residual_m'],'UNMODELED_PENETRATION')
    require(max(force_errors)<=criteria['max_relative_support_error'] and max(normal_errors)<=criteria['max_relative_support_error'],'UNSUPPORTED_WEIGHT')
    return {'status':'FAIL' if errors else 'PASS','violations':sorted(set(errors)),
            'policy_sha256':POLICY_SHA256,'samples':len(rows),'settled_samples':len(settled),
            'duration_s':duration_s,'settled_from_s':settled_from_s,
            'metrics':{'min_CG_AGL_m':min(heights),'final_CG_AGL_m':heights[-1],
                       'settled_AGL_range_m':max(tail_heights)-min(tail_heights),
                       'peak_settled_speed_m_s':max(speeds),'peak_settled_body_rate_rad_s':max(rates),
                       'peak_settled_tilt_deg':max(tilts),'peak_settled_horizontal_drift_m':drift,
                       'peak_transient_compression_m':compression_max,'peak_settled_compression_m':settled_compression_max,
                       'max_contact_geometry_residual_m':worst_residual,
                       'max_penetration_beyond_compliance_m':worst_unsupported_penetration,
                       'mean_support_up_N':sum(support)/len(support),'mean_weight_N':sum(weight)/len(weight),
                       'max_relative_support_error':max(force_errors),'max_relative_normal_force_error':max(normal_errors),
                       'motor_peak_abs':motor_peak},'feet':per_foot,
            'actuation_authority':False,'flight_ready':False,'forcing_binding_verified':False}
