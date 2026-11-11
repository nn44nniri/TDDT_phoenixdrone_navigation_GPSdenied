"""Bounded candidate rollouts of the native closed-inner-loop WingMPET twin.

The high-fidelity FMU supplies physical state/capability alongside an identified
low-order AP inner-loop response. It is not the old raw-motor affine mapping.
Prediction times stay in the actual source clock; no delayed output is retimed.
"""
import math
import numpy as np


def predict(snapshot):
    nav=snapshot['twins']['MESA_NAV'];wing=snapshot['twins']['WingMPET']
    x=np.array(nav['position_ned_m']+nav['velocity_ned_m_s'],float)
    if snapshot['phase']=='contact_settle':
        evidence=snapshot['reference'].get('contact_evidence',{})
        qualified=(evidence.get('qualified') is True and -x[2]<.8
                   and 0<=snapshot['time_s']-evidence.get('qualified_time_s',-999)<=12.
                   and evidence.get('source')=='MESA/barometer/IMU only')
        rows=[{'time_s':snapshot['time_s']+k*.1,
               'state':np.r_[x[:3],x[3:]*math.exp(-k*.1/.2)].tolist()} for k in range(21)]
        return {'schema':'Layer4-realtime-candidates-v1','snapshot_hash':snapshot['snapshot_hash'],
            'source_time_s':snapshot['time_s'],'actuation_authority':False,
            'candidates':[{'candidate_id':'CONTACT-SUPPORT','acceleration_ne_m_s2':[0.,0.],
                'climb_rate_up_m_s':0.,'prediction_tube':rows,'cost':0.,'feasible':bool(qualified),
                'model':'qualified ground contact support; no airborne zero-velocity observation',
                'source_snapshot_hash':snapshot['snapshot_hash'],'source_time_s':snapshot['time_s'],
                'physical_FMU_operation':wing['FMU_operation']}]}
    target=np.array(snapshot['reference']['position_ned_m'],float)
    vmax=float(snapshot['reference'].get('speed_m_s',3.))
    vref=.45*(target-x[:3]);speed=np.linalg.norm(vref[:2])
    if speed>vmax:vref[:2]*=vmax/speed
    flare=snapshot['phase']=='landing_flare'
    if flare:vref[:2]=x[3:5]
    vref[2]=np.clip(vref[2],-1.2,.25 if flare else .8)
    model=wing['reduced_inner_loop'];bias=np.array(model['disturbance_ned_m_s2'],float)
    # Identified Native attitude response is around one second. A fast outer
    # velocity loop fighting that lag produces alternating lean saturation.
    desired=.25*(vref[:2]-x[3:5])-bias[:2]
    previous=np.array(model.get('last_command_acceleration_ne_m_s2',[0.,0.]),float)
    if flare:desired[:]=0.
    limit=9.80665*math.tan(math.radians(snapshot['constraints']['max_lean_deg']))
    candidates=[];dt=.1;horizon=20
    neighbourhood=(0.,) if flare else (-.1,0.,.1)
    for i,dn in enumerate(neighbourhood):
        for j,de in enumerate(neighbourhood):
            a=desired+np.array([dn,de]);norm=np.linalg.norm(a)
            if norm>limit:a*=limit/norm
            z=x.copy();rows=[{'time_s':snapshot['time_s'],'state':z.tolist()}];cost=0.
            response=np.array(wing.get('initial_acceleration_ne_m_s2',[0.,0.]),float)
            tau=float(model['inner_loop_tau_s'])
            for k in range(horizon):
                response+=(1.-math.exp(-dt/tau))*(a-response)
                acc=np.r_[response+bias[:2],1.5*(vref[2]-z[5])]
                z[:3]+=z[3:]*dt+.5*acc*dt*dt;z[3:]+=acc*dt
                desired_position=x[:3]+vref*(k+1)*dt
                cost+=float(np.sum((z[3:]-vref)**2)+.15*np.sum((z[:3]-desired_position)**2)
                            +.025*np.sum(a*a)+.5*np.sum((a-previous)**2))
                rows.append({'time_s':snapshot['time_s']+(k+1)*dt,'state':z.tolist()})
            safe=bool(np.all(np.isfinite(z)) and np.linalg.norm(a)<=limit+1e-8
                      and wing['maximum_total_thrust_N']/wing['mass_kg']>9.80665/math.cos(math.radians(18)))
            candidates.append({'candidate_id':f'RT-{i}-{j}','acceleration_ne_m_s2':a.tolist(),
                'climb_rate_up_m_s':float(-vref[2]),'prediction_tube':rows,'cost':cost,
                'feasible':safe,'model':'WingMPET/AP-inner-loop-reduced-v1',
                'source_snapshot_hash':snapshot['snapshot_hash'],'source_time_s':snapshot['time_s'],
                'physical_FMU_operation':wing['FMU_operation'],'scenarios':'bounded control neighbourhood',
                'prediction_error_state':'Layer7 sensor-domain feedback; not Native truth'})
    return {'schema':'Layer4-realtime-candidates-v1','snapshot_hash':snapshot['snapshot_hash'],
        'source_time_s':snapshot['time_s'],'candidates':candidates,'actuation_authority':False}
