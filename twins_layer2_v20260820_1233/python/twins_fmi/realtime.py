"""Sensor-restricted realtime MESA aiding and the three Layer2 FMU twins.

Approved measurements: gyro, accelerometer, magnetometer, barometer, scalar
pitot. An absent ultrasonic sensor remains absent. No FCU pose/velocity/attitude,
SIM_STATE, odometry, vision or simulated wind is an estimator measurement.
"""
import math
import re,hashlib
import numpy as np
from scipy.spatial.transform import Rotation


def rotation(q):
    return Rotation.from_quat([q[1],q[2],q[3],q[0]])


def stationary_attitude(acceleration, magnetic, magnetic_reference):
    """Body-to-NED attitude from two approved vectors, including pitch=90deg.

    Euler roll plus surveyed yaw is singular for a tailsitter on its tail.
    Wahba alignment avoids choosing an arbitrary roll from accelerometer noise.
    Gravity has greater weight because the coarse magnetic map is a prior.
    """
    acc=np.array(acceleration,float,copy=True);mag=np.array(magnetic,float,copy=True)
    reference=np.array(magnetic_reference,float,copy=True)
    if min(np.linalg.norm(acc),np.linalg.norm(mag),np.linalg.norm(reference))<1e-6:
        raise ValueError('stationary attitude requires gravity and compass')
    acc/=np.linalg.norm(acc);mag/=np.linalg.norm(mag);reference/=np.linalg.norm(reference)
    if np.linalg.norm(np.cross(acc,mag))<.1:
        raise ValueError('stationary attitude vectors insufficiently independent')
    attitude,residual=Rotation.align_vectors([[0.,0.,-1.],reference],[acc,mag],weights=[100.,1.])
    if residual>.15:raise ValueError('stationary gravity/compass inconsistent with magnetic prior')
    return attitude


class GeomagneticMap:
    """Immutable offline field model; query only at the own estimated location."""
    def __init__(self,path):
        raw=path.read_bytes();text=raw.decode();self.sha256=hashlib.sha256(raw).hexdigest();self.tables=[]
        for name in ('intensity','declination','inclination'):
            body=re.search(name+r'_table\[LAT_TABLE_SIZE\]\[LON_TABLE_SIZE\]\s*=\s*\{(.*?)\n\};',text,re.S).group(1)
            self.tables.append(np.array([float(v) for v in re.findall(r'([-+]?\d+\.\d+)f',body)]).reshape(19,37))

    def at(self,latitude,longitude):
        if not -90<=latitude<=90 or not -180<=longitude<=180:raise ValueError('field query outside geodetic domain')
        i=min(17,int(math.floor((latitude+90)/10)));j=min(35,int(math.floor((longitude+180)/10)))
        fy=(latitude+90)/10-i;fx=(longitude+180)/10-j
        strength,decl,incl=[(1-fy)*((1-fx)*a[i,j]+fx*a[i,j+1])+fy*((1-fx)*a[i+1,j]+fx*a[i+1,j+1]) for a in self.tables]
        decl,incl=math.radians(decl),math.radians(incl)
        return np.array([strength*math.cos(incl)*math.cos(decl),strength*math.cos(incl)*math.sin(decl),strength*math.sin(incl)])


def geomagnetic_reference(path,latitude,longitude):
    field=GeomagneticMap(path)
    return field.at(latitude,longitude),field.sha256


def navigation_earth_model(origin,state):
    """WGS84 local-NED transport and Coriolis; no position observation.

    Native JSBSim PQR is Earth-relative (FGPropagate::GetPQR), not inertial.
    Thus gyro propagation removes transport rate only, not Earth spin twice.
    Position remains a local mission approximation over this short region.
    """
    lat=math.radians(origin['latitude_deg'])+state[0]/6362000.
    e2=.00669437999014;radius=6378137.;s=math.sin(lat)
    rn=radius/math.sqrt(1-e2*s*s);rm=radius*(1-e2)/(1-e2*s*s)**1.5
    h=origin.get('altitude_msl_m',origin.get('terrain_elevation_msl_m',1800.))-state[2]
    lon=math.radians(origin['longitude_deg'])+state[1]/((rn+h)*math.cos(lat))
    transport=np.array([state[4]/(rn+h),-state[3]/(rm+h),-state[4]*math.tan(lat)/(rn+h)])
    earth=7.292115e-5*np.array([math.cos(lat),0.,-math.sin(lat)])
    return math.degrees(lat),math.degrees(lon),transport,earth


class RealtimeTwins:
    def __init__(self, adapter, origin, calibration, climate):
        adapter._ensure();self.adapter=adapter;self.mesa=adapter.mesa;self.wing=adapter.wing
        self.origin=origin;self.climate=dict(climate);self.last=float(calibration[-1]['plant_time_s'])
        self.last_baro=None;self.last_pitot=None;self.feedback_consumed=None
        self.takeoff_requested=False;self.initial_ground_support=True;self.baro_height_lpf=0.
        fields=('xacc','yacc','zacc','xgyro','ygyro','zgyro','xmag','ymag','zmag','pressure_alt')
        means={k:float(np.mean([s['data'][k] for s in calibration])) for k in fields}
        if len(calibration)<100 or self.last-calibration[0]['plant_time_s']<2.:
            raise ValueError('measured stationary calibration interval too short')
        acc=np.array([means[k] for k in fields[:3]])
        gyro=np.array([means[k] for k in fields[3:6]])
        if np.linalg.norm(gyro)>.05 or abs(np.linalg.norm(acc)-9.80665)>.8:
            raise ValueError('stationary IMU calibration outside support envelope')
        self.baro0=means['pressure_alt']
        self.magnetic_map=GeomagneticMap(adapter.config.path_value('TDDT_LAYER6_ROOT')/'FlightStack/ardupilot/libraries/AP_Declination/tables.cpp')
        self.mag0=self.magnetic_map.at(origin['latitude_deg'],origin['longitude_deg']);self.magnetic_map_hash=self.magnetic_map.sha256
        r=stationary_attitude(acc,[means[k] for k in fields[6:9]],self.mag0)
        q=r.as_quat();q=np.r_[q[3],q[:3]]
        self.acc_bias=acc*(1.-9.80665/np.linalg.norm(acc));self.gyro_bias=gyro
        self.mesa.fmu.set(rN=0.,rE=0.,rD=0.,vN=0.,vE=0.,vD=0.,
            qw=q[0],qx=q[1],qy=q[2],qz=q[3],
            bgx=gyro[0],bgy=gyro[1],bgz=gyro[2],
            bfx=self.acc_bias[0],bfy=self.acc_bias[1],bfz=self.acc_bias[2])
        self.wind=np.array(climate['wind_ned_m_s'],float)
        self.P=np.diag([.01]*3+[.005]*3+[4.]*3)
        self.model={'disturbance_ned_m_s2':[0.,0.,0.],'inner_loop_tau_s':1.0,
                    'feedback_version':0,'feedback_source':None}
        motor=self.wing.fmu.operation_signature(28)
        maximum=self.wing.fmu.evaluate_operation(28,inputs=dict(zip(motor['inputs'],[.01,800.,800.,1.,1.])))
        self.maximum_thrust=float(maximum['total_thrust'])
        self.calibration={'gyro_bias_rad_s':gyro.tolist(),'accel_bias_m_s2':self.acc_bias.tolist(),
            'initial_q_wxyz':q.tolist(),'baro_initial_m':self.baro0,
            'source':'stationary accelerometer + magnetometer + offline geomagnetic map (Wahba)',
            'FCU_pose_used':False,'GNSS_used':False}

    def state(self):
        v=self.mesa.fmu.get('rN','rE','rD','vN','vE','vD','qw','qx','qy','qz')
        return np.array([v[k] for k in ('rN','rE','rD','vN','vE','vD')]),np.array([v[k] for k in ('qw','qx','qy','qz')])

    def _correct(self,residual,H,variance):
        state,q=self.state();x=np.r_[state,self.wind];H=np.asarray(H,float);S=float(H@self.P@H+variance)
        nis=float(residual*residual/S)
        if not math.isfinite(nis) or nis>100.:return {'accepted':False,'nis':nis}
        weight=min(1.,math.sqrt(9./max(nis,1e-9)))
        S=float(H@self.P@H+variance/weight);K=self.P@H/S;x+=K*residual
        I=np.eye(9)-np.outer(K,H);self.P=I@self.P@I.T+np.outer(K,K)*variance/weight
        self.mesa.fmu.set(**dict(zip(('rN','rE','rD','vN','vE','vD'),map(float,x[:6]))));self.wind=x[6:]
        return {'accepted':True,'nis':nis,'residual':float(residual),'variance':variance,'weight':weight}

    def _pitot_wind_only(self,airspeed,state):
        # With no independent wind sample, |v-w| has a translation gauge.
        # Do not turn an uncertain weather wind into a ground-velocity sensor.
        relative=state[3:]-self.wind;speed=np.linalg.norm(relative)
        if speed<=.5:return {'accepted':False,'reason':'pitot norm derivative singular'}
        direction=relative/speed;H=-direction;P=self.P[6:,6:]
        variance=.25+float(direction@self.P[3:6,3:6]@direction)
        residual=airspeed-speed;S=float(H@P@H+variance);nis=residual*residual/S
        if nis>100:return {'accepted':False,'nis':float(nis)}
        K=P@H/S;self.wind+=K*residual;I=np.eye(3)-np.outer(K,H)
        self.P[6:,6:]=I@P@I.T+np.outer(K,K)*variance
        return {'accepted':True,'nis':float(nis),'residual':float(residual),
                'navigation_state_corrected':False,'scope':'wind-norm model only; missing independent wind sample'}

    def step(self,measurements,samples,feedback=None):
        if set(measurements)-{'HIGHRES_IMU','AIRSPEED','ULTRASONIC_WIND'}:
            raise ValueError('unapproved navigation measurement')
        if (measurements.get('ULTRASONIC_WIND') or {}).get('present'):
            raise ValueError('ultrasonic samples require their independent registered source')
        # Release the launch zero-velocity constraint before the first powered
        # acceleration. Waiting for barometric height discards the liftoff
        # impulse and biases the entire airborne velocity estimate.
        if self.takeoff_requested:self.initial_ground_support=False
        if feedback is not None:
            if feedback.get('destination')!='Layer2/model-only' or feedback.get('sensor_only') is not True:
                raise ValueError('unapproved Layer7 navigation feedback')
            self.model.update(feedback['model_update']);self.feedback_consumed=feedback['route_hash']
        count=0
        for row in samples:
            t=float(row['plant_time_s']);dt=t-self.last
            if dt<=0:continue
            if dt>.15:raise ValueError('missing HIGHRES_IMU integration interval '+str(dt))
            d=row['data']
            if d.get('mavpackettype')!='HIGHRES_IMU':raise ValueError('unapproved IMU source')
            previous,qprev=self.state();lat,lon,transport,earth=navigation_earth_model(self.origin,previous)
            gyro=np.array([d[k] for k in ('xgyro','ygyro','zgyro')])-rotation(qprev).inv().apply(transport)
            self.mesa.fmu.set(imu_gx=gyro[0],imu_gy=gyro[1],imu_gz=gyro[2],
                              imu_ax=d['xacc'],imu_ay=d['yacc'],imu_az=d['zacc'])
            self.mesa.fmu.evaluate_operation(5,inputs={'dt':dt},communication_step=dt)
            propagated,_=self.state();coriolis=-np.cross(2*earth+transport,previous[3:])
            propagated[:3]+=.5*dt*dt*coriolis;propagated[3:]+=dt*coriolis
            self.mesa.fmu.set(**dict(zip(('rN','rE','rD','vN','vE','vD'),propagated)))
            self.mag0=self.magnetic_map.at(lat,lon)
            F=np.eye(9);F[:3,3:6]=np.eye(3)*dt
            self.P=F@self.P@F.T+np.diag([1e-6]*3+[.0004]*3+[.001]*3)*dt
            # A direction-only compass innovation aids attitude, not position.
            x,q=self.state();r=rotation(q);mag=np.array([d[k] for k in ('xmag','ymag','zmag')])
            if .5*np.linalg.norm(self.mag0)<np.linalg.norm(mag)<1.5*np.linalg.norm(self.mag0):
                predicted=r.inv().apply(self.mag0);error=np.cross(mag/np.linalg.norm(mag),predicted/np.linalg.norm(predicted))
                force=np.array([d[k] for k in ('xacc','yacc','zacc')])-self.acc_bias
                expected_acc=np.array(self.model.get('last_command_acceleration_ne_m_s2',[0.,0.]))+np.array(self.model['disturbance_ned_m_s2'][:2])
                if self.initial_ground_support and abs(np.linalg.norm(force)-9.80665)<.4:
                    expected=r.inv().apply([0.,0.,-1.])
                    error+=.5*np.cross(force/np.linalg.norm(force),expected)
                if self.initial_ground_support:
                    self.gyro_bias=np.clip(self.gyro_bias-.05*dt*error,-.03,.03)
                self.mesa.fmu.set(bgx=self.gyro_bias[0],bgy=self.gyro_bias[1],bgz=self.gyro_bias[2])
                gain=1.5 if self.initial_ground_support else .1
                updated=(r*Rotation.from_rotvec(gain*dt*error)).as_quat()
                self.mesa.fmu.set(qw=updated[3],qx=updated[0],qy=updated[1],qz=updated[2])
            self.last=t;count+=1
        h=measurements['HIGHRES_IMU'];d=h['data'];updates={}
        if h['plant_time_s']!=self.last:raise ValueError('sensor snapshot/IMU cursor mismatch')
        if h['wire_sha256']!=self.last_baro and d['fields_updated']&0x800:
            x,_=self.state();z=-(float(d['pressure_alt'])-self.baro0)
            self.baro_height_lpf=.9*self.baro_height_lpf+.1*(-z)
            updates['barometer']=self._correct(z-x[2],[0,0,1,0,0,0,0,0,0],.09)
            self.last_baro=h['wire_sha256']
        pitot=measurements.get('AIRSPEED') or {}
        if pitot.get('wire_sha256')!=self.last_pitot and pitot.get('airspeed_m_s') is not None:
            airspeed=float(pitot['airspeed_m_s']);x,_=self.state()
            if 0<=airspeed<=18:
                updates['pitot']=self._pitot_wind_only(airspeed,x)
            self.last_pitot=pitot.get('wire_sha256')
        # One launch-support constraint, qualified by the known stationary
        # initialization and measured barometric lift. Never re-anchor airborne
        # navigation, and never use FCU position as a zero-velocity detector.
        if self.takeoff_requested and self.baro_height_lpf>.4:self.initial_ground_support=False
        if self.initial_ground_support:
            self.mesa.fmu.set(rN=0.,rE=0.,vN=0.,vE=0.,vD=0.)
        x,q=self.state()
        if not np.all(np.isfinite(np.r_[x,q])):raise ValueError('nonfinite MESA state')
        return {'twin_id':'MESA_NAV','time_s':self.last,'position_ned_m':x[:3].tolist(),
            'velocity_ned_m_s':x[3:].tolist(),'q_wxyz':q.tolist(),'covariance':self.P.tolist(),
            'body_rates_rad_s':[float(d[k])-self.gyro_bias[i] for i,k in enumerate(('xgyro','ygyro','zgyro'))],
            'initial_ground_support':self.initial_ground_support,
            'airborne_gravity_pseudo_measurement':False,
            'airspeed_m_s':pitot.get('airspeed_m_s'),
            'earth_model':'WGS84 transport + Coriolis; native Earth-relative gyro',
            'attitude_frame':'BODY_FRD_TO_CURRENT_LOCAL_NED','velocity_frame':'CURRENT_LOCAL_NED',
            'position_frame':'LOCAL_MISSION_NED_APPROXIMATION',
            'wind_estimate_ned_m_s':self.wind.tolist(),'magnetic_map_sha256':self.magnetic_map_hash,
            'sensor_updates':updates,'integrated_IMU_samples':count,
            'source_wire_sha256':h['wire_sha256'],'navigation_authority':'MESA',
            'feedback_consumed':self.feedback_consumed,'truth_used':False,'GNSS_used':False,
            'odometry_used':False,'vision_used':False}

    def wing_packet(self,nav,servo):
        x=nav['position_ned_m']+nav['velocity_ned_m_s'];q=nav['q_wxyz']
        motors=[max(0.,min(1.,(float(servo.get('servo%d_raw'%i,1000))-1000.)/1000.)) for i in (3,4)]
        inp=dict(zip(('pn','pe','pd','vn','ve','vd'),x));inp.update(dict(zip(('qw','qx','qy','qz'),q)))
        rates=nav['body_rates_rad_s']
        inp.update(dt=.01,p_rate=rates[0],q_rate=rates[1],r_rate=rates[2],omega_left=800*motors[0],omega_right=800*motors[1],soc=1.,
            left_motor_cmd=motors[0],right_motor_cmd=motors[1],
            left_elevon_rad=np.clip((float(servo.get('servo1_raw',1500))-1500.)/500.,-1.,1.)*math.pi/6,
            right_elevon_rad=np.clip((float(servo.get('servo2_raw',1500))-1500.)/500.,-1.,1.)*math.pi/6,
            wind_n=self.climate['wind_ned_m_s'][0],wind_e=self.climate['wind_ned_m_s'][1],
            wind_d=self.climate['wind_ned_m_s'][2],air_density=self.climate['air_density_kg_m3'])
        out=self.wing.fmu.evaluate_operation(27,inputs=inp,communication_step=.01)
        return {'twin_id':'WingMPET','time_s':nav['time_s'],'FMU_operation':27,'physical_outputs':out,
            'reduced_inner_loop':dict(self.model),'maximum_total_thrust_N':self.maximum_thrust,'mass_kg':.65,
            'initial_acceleration_ne_m_s2':[(out['next_vn']-x[3])/.01,(out['next_ve']-x[4])/.01],
            'climate_input_time_s':self.climate.get('time_s'),'climate_input_source':'current atomic MicroMet model; not wind observation',
            'climate_input_forcing_sha256':self.climate.get('forcing_sha256'),
            'parameter_source':'Phoenix source motor law / integration mass',
            'scope':'SITL native inner-loop predictive control; no ground-contact truth injection'}
