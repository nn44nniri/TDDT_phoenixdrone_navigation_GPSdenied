"""Atomic three-twin network for the explicitly scoped realtime VTOL profile."""
from copy import deepcopy
import hashlib,json,math


def digest(obj):
    return hashlib.sha256(json.dumps(obj,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def atomic_snapshot(nav,wing,climate,*,phase,reference,transaction_id,origin,feedback_hash=None):
    t=float(nav['time_s'])
    if abs(float(wing['time_s'])-t)>1e-9:raise ValueError('non-atomic twin source time')
    if 'climate_input_time_s' in wing:
        if (wing['climate_input_time_s']!=climate.get('time_s')
            or wing.get('climate_input_forcing_sha256')!=climate.get('forcing_sha256')):
            raise ValueError('WingMPET climate inputs differ from the atomic MicroMet snapshot')
    if nav.get('truth_used') or nav.get('GNSS_used') or nav.get('odometry_used') or nav.get('vision_used'):
        raise ValueError('navigation source outside allowed sensor set')
    data={'schema':'TDDT-realtime-atomic-snapshot-v1','transaction_id':transaction_id,
        'time_s':t,'frame':'LOCAL_NED_MISSION_ORIGIN','origin':origin,
        'phase':phase,'reference':reference,'twins':{'MESA_NAV':nav,'WingMPET':wing,'MicroMetTwin':climate},
        'couplings':[{'source':'MicroMetTwin.wind_ned_m_s','target':'MESA_NAV.initial_wind_prior; not an observation'},
                     {'source':'MicroMetTwin.current_wind/density','target':'WingMPET.current_physical_model_inputs'},
                     {'source':'MESA_NAV.position/velocity/attitude','target':'WingMPET.current_state'},
                     {'source':'WingMPET.reduced_inner_loop','target':'Layer4.reachable_predictions'},
                     {'source':'Layer7.model_residual','target':'Layer2.WingMPET.model_parameters'}],
        'feedback_route_hash':feedback_hash,'navigation_authority':'MESA',
        'constraints':{'max_airspeed_m_s':18.,'max_lean_deg':8.,'max_climb_m_s':1.2,
                       'max_navigation_age_s':.3,'command_lease_s':.5},
        'snapshot_atomic':True,'prior_role':'model/candidate warm start only'}
    # Round-trip rejects NaN and breaks mutable aliases before publication.
    data=json.loads(json.dumps(data,allow_nan=False));data['snapshot_hash']=digest(data)
    return data
