"""Receding-horizon candidate MPC and source-parameter QHOVER pilot mapping."""
import math
import numpy as np
from scipy.spatial.transform import Rotation


def wrap(angle):return (angle+math.pi)%(2*math.pi)-math.pi


def decide(snapshot,forecast,parameters,*,heading_reference):
    if forecast['snapshot_hash']!=snapshot['snapshot_hash']:raise ValueError('MPC snapshot mismatch')
    feasible=[c for c in forecast['candidates'] if c['feasible']]
    if not feasible:raise ValueError('no safe model candidate')
    c=min(feasible,key=lambda row:row['cost']);nav=snapshot['twins']['MESA_NAV']
    if nav.get('airspeed_m_s') is not None and not 0<=nav['airspeed_m_s']<=snapshot['constraints']['max_airspeed_m_s']:
        raise ValueError('RTA measured airspeed envelope exceeded')
    contact_cut=snapshot['phase']=='contact_settle' and c['candidate_id']=='CONTACT-SUPPORT'
    if not contact_cut and any(abs(v)>limit for v,limit in zip(nav['body_rates_rad_s'],np.radians([300.,200.,150.]))):
        raise ValueError('RTA body-rate envelope exceeded')
    q=nav['q_wxyz'];r=Rotation.from_quat([*q[1:],q[0]])*Rotation.from_euler('y',-90,degrees=True)
    observed_angles=r.as_euler('xyz')
    if not contact_cut and max(abs(observed_angles[0]),abs(observed_angles[1]))>math.radians(25.):
        raise ValueError('RTA observed VTOL lean envelope exceeded')
    heading=observed_angles[2];an,ae=c['acceleration_ne_m_s2'];g=9.80665
    forward=math.cos(heading)*an+math.sin(heading)*ae
    right=-math.sin(heading)*an+math.cos(heading)*ae
    roll=math.atan2(right,g);pitch=-math.atan2(forward,g)
    angle_max=math.radians(parameters['Q_A_ANGLE_MAX'])
    def axis(angle,dz):
        fraction=min(1.,abs(angle)/angle_max)
        return math.copysign(dz+(1.-dz)*fraction,angle) if fraction>1e-4 else 0.
    # MANUAL_CONTROL.x is reversed by GCS_MAVLINK_Plane::manual_override;
    # GUI elevator therefore has the opposite sign of AP's desired lean pitch.
    a=axis(roll,parameters['RC1_DZ']/500.)
    e=-axis(pitch,parameters['RC2_DZ']/500.)
    climb=c['climb_rate_up_m_s'];dz=parameters['RC3_DZ']/1000.
    max_up=parameters['Q_PILOT_SPD_UP'];max_down=parameters['Q_PILOT_SPD_DN'] or max_up
    throttle=.5 if abs(climb)<.03 else .5+math.copysign(dz+(.5-dz)*min(1.,abs(climb)/(max_up if climb>0 else max_down)),climb)
    yaw_error=wrap(heading_reference-heading)
    yaw=float(np.clip(.5*yaw_error,-.15,.15))
    u=[a,e,throttle,yaw]
    mode='QHOVER'
    if snapshot['phase']=='landing_flare':u=[0.,0.,throttle,0.]
    if snapshot['phase']=='contact_settle':
        # QHOVER retains a vertical controller and cannot certify motors stopped.
        # Only a qualified low-height, latched contact candidate permits
        # zero pilot throttle in manual-throttle QSTABILIZE before normal DISARM.
        if c['candidate_id']!='CONTACT-SUPPORT':raise ValueError('contact candidate missing')
        u=[0.,0.,0.,0.];mode='QSTABILIZE'
    if not all(math.isfinite(v) for v in u) or not 0<=throttle<=1:raise ValueError('pilot domain violation')
    if parameters['Q_TAILSIT_INPUT']!=0:raise ValueError('uncommissioned tailsitter input convention')
    return {'schema':'Layer5-realtime-MPC-v1','transaction_id':snapshot['transaction_id'],
        'candidate_id':c['candidate_id'],'snapshot_hash':snapshot['snapshot_hash'],
        'source_time_s':snapshot['time_s'],'u_cmd':u,'mode':mode,
        'gates':{'MPC':True,'PSF':True,'RTA':True,'FINAL':True},
        'prediction':c,'control_semantics':'AP QHOVER lean/climb/yaw; not motor-domain u',
        'mapping':{'roll_rad':roll,'pitch_rad':pitch,'heading_rad':heading,
            'climb_rate_up_m_s':climb,'source':'same-session AP parameter readbacks'},
        'command_lease_s':snapshot['constraints']['command_lease_s'],'direct_pwm':False}
