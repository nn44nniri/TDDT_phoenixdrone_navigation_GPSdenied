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


def geomagnetic_reference(path,latitude,longitude):
    """Offline coarse IGRF map, not an online simulator/FCU measurement."""
    raw=path.read_bytes();text=raw.decode();values=[]
    i=int(math.floor((latitude+90)/10));j=int(math.floor((longitude+180)/10))
    fy=(latitude+90)/10-i;fx=(longitude+180)/10-j
    for name in ('intensity','declination','inclination'):
        body=re.search(name+r'_table\[LAT_TABLE_SIZE\]\[LON_TABLE_SIZE\]\s*=\s*\{(.*?)\n\};',text,re.S).group(1)
        a=np.array([float(v) for v in re.findall(r'([-+]?\d+\.\d+)f',body)]).reshape(19,37)
        values.append((1-fy)*((1-fx)*a[i,j]+fx*a[i,j+1])+fy*((1-fx)*a[i+1,j]+fx*a[i+1,j+1]))
    strength,decl,incl=values;decl,incl=math.radians(decl),math.radians(incl)
    return np.array([strength*math.cos(incl)*math.cos(decl),strength*math.cos(incl)*math.sin(decl),strength*math.sin(incl)]),hashlib.sha256(raw).hexdigest()


class RealtimeTwins:
    def __init__(self, adapter, origin, calibration, climate):
        adapter._ensure();self.adapter=adapter;self.mesa=adapter.mesa;self.wing=adapter.wing
        self.origin=origin;self.climate=dict(climate);self.last=float(calibration[-1]['plant_time_s'])
        self.last_baro=None;self.last_pitot=None;self.feedback_consumed=None
        fields=('xacc','yacc','zacc','xgyro','ygyro','zgyro','xmag','ymag','zmag','pressure_alt')
        means={k:float(np.mean([s['data'][k] for s in calibration])) for k in fields}
        if len(calibration)<100 or self.last-calibration[0]['plant_time_s']<2.:
            raise ValueError('measured stationary calibration interval too short')
        acc=np.array([means[k] for k in fields[:3]])
        gyro=np.array([means[k] for k in fields[3:6]])
        if np.linalg.norm(gyro)>.05 or abs(np.linalg.norm(acc)-9.80665)>.8:
            raise ValueError('stationary IMU calibration outside support envelope')
        roll=math.atan2(-acc[1],-acc[2]);pitch=math.atan2(acc[0],math.hypot(acc[1],acc[2]))
        r=Rotation.from_euler('xyz',[roll,pitch,math.radians(origin['heading_deg'])])
        q=r.as_quat();q=np.r_[q[3],q[:3]]
        self.baro0=means['pressure_alt']
        self.mag0,self.magnetic_map_hash=geomagnetic_reference(
            adapter.config.path_value('TDDT_LAYER6_ROOT')/'FlightStack/ardupilot/libraries/AP_Declination/tables.cpp',
            origin['latitude_deg'],origin['longitude_deg'])
        self.acc_bias=acc*(1.-9.80665/np.linalg.norm(acc));self.gyro_bias=gyro
        self.mesa.fmu.set(rN=0.,rE=0.,rD=0.,vN=0.,vE=0.,vD=0.,
            qw=q[0],qx=q[1],qy=q[2],qz=q[3],
            bgx=gyro[0],bgy=gyro[1],bgz=gyro[2],
            bfx=self.acc_bias[0],bfy=self.acc_bias[1],bfz=self.acc_bias[2])
        self.wind=np.array(climate['wind_ned_m_s'],float)
        self.P=np.diag([.01]*3+[.005]*3+[4.]*3)
        self.model={'disturbance_ned_m_s2':[0.,0.,0.],'inner_loop_tau_s':.35,
                    'feedback_version':0,'feedback_source':None}
        self.calibration={'gyro_bias_rad_s':gyro.tolist(),'accel_bias_m_s2':self.acc_bias.tolist(),
            'initial_q_wxyz':q.tolist(),'baro_initial_m':self.baro0,
            'source':'stationary approved sensors + surveyed mission heading',
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

    def step(self,measurements,samples,feedback=None):
        if set(measurements)-{'HIGHRES_IMU','AIRSPEED','ULTRASONIC_WIND'}:
            raise ValueError('unapproved navigation measurement')
        if (measurements.get('ULTRASONIC_WIND') or {}).get('present'):
            raise ValueError('ultrasonic samples require their independent registered source')
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
            self.mesa.fmu.set(gx=d['xgyro'],gy=d['ygyro'],gz=d['zgyro'],
                              fx=d['xacc'],fy=d['yacc'],fz=d['zacc'])
            self.mesa.fmu.evaluate_operation(5,inputs={'dt':dt},communication_step=dt)
            F=np.eye(9);F[:3,3:6]=np.eye(3)*dt
            self.P=F@self.P@F.T+np.diag([1e-6]*3+[.0004]*3+[.001]*3)*dt
            # A direction-only compass innovation aids attitude, not position.
            x,q=self.state();r=rotation(q);mag=np.array([d[k] for k in ('xmag','ymag','zmag')])
            if .5*np.linalg.norm(self.mag0)<np.linalg.norm(mag)<1.5*np.linalg.norm(self.mag0):
                predicted=r.inv().apply(self.mag0);error=np.cross(mag/np.linalg.norm(mag),predicted/np.linalg.norm(predicted))
                force=np.array([d[k] for k in ('xacc','yacc','zacc')])-self.acc_bias
                if abs(np.linalg.norm(force)-9.80665)<.4:
                    expected=r.inv().apply([0.,0.,-1.])
                    error+=.2*np.cross(force/np.linalg.norm(force),expected)
                updated=(r*Rotation.from_rotvec(.8*dt*error)).as_quat()
                self.mesa.fmu.set(qw=updated[3],qx=updated[0],qy=updated[1],qz=updated[2])
            self.last=t;count+=1
        h=measurements['HIGHRES_IMU'];d=h['data'];updates={}
        if h['plant_time_s']!=self.last:raise ValueError('sensor snapshot/IMU cursor mismatch')
        if h['wire_sha256']!=self.last_baro and d['fields_updated']&0x800:
            x,_=self.state();z=-(float(d['pressure_alt'])-self.baro0)
            updates['barometer']=self._correct(z-x[2],[0,0,1,0,0,0,0,0,0],.09)
            self.last_baro=h['wire_sha256']
        pitot=measurements.get('AIRSPEED') or {}
        if pitot.get('wire_sha256')!=self.last_pitot and pitot.get('airspeed_m_s') is not None:
            airspeed=float(pitot['airspeed_m_s']);x,_=self.state()
            relative=x[3:]-self.wind;speed=np.linalg.norm(relative)
            if 0<=airspeed<=18 and speed>.5:
                H=np.r_[np.zeros(3),relative/speed,-relative/speed]
                updates['pitot']=self._correct(airspeed-speed,H,.25)
            self.last_pitot=pitot.get('wire_sha256')
        x,q=self.state()
        if not np.all(np.isfinite(np.r_[x,q])):raise ValueError('nonfinite MESA state')
        return {'twin_id':'MESA_NAV','time_s':self.last,'position_ned_m':x[:3].tolist(),
            'velocity_ned_m_s':x[3:].tolist(),'q_wxyz':q.tolist(),'covariance':self.P.tolist(),
            'sensor_updates':updates,'integrated_IMU_samples':count,
            'source_wire_sha256':h['wire_sha256'],'navigation_authority':'MESA',
            'feedback_consumed':self.feedback_consumed,'truth_used':False,'GNSS_used':False,
            'odometry_used':False,'vision_used':False}

    def wing_packet(self,nav,servo):
        x=nav['position_ned_m']+nav['velocity_ned_m_s'];q=nav['q_wxyz']
        motors=[max(0.,min(1.,(float(servo.get('servo%d_raw'%i,1000))-1000.)/1000.)) for i in (3,4)]
        inp=dict(zip(('pn','pe','pd','vn','ve','vd'),x));inp.update(dict(zip(('qw','qx','qy','qz'),q)))
        inp.update(dt=.01,p_rate=0.,q_rate=0.,r_rate=0.,omega_left=800*motors[0],omega_right=800*motors[1],soc=1.,
            left_motor_cmd=motors[0],right_motor_cmd=motors[1],left_elevon_rad=0.,right_elevon_rad=0.,
            wind_n=self.climate['wind_ned_m_s'][0],wind_e=self.climate['wind_ned_m_s'][1],
            wind_d=self.climate['wind_ned_m_s'][2],air_density=self.climate['air_density_kg_m3'])
        out=self.wing.fmu.evaluate_operation(27,inputs=inp,communication_step=.01)
        return {'twin_id':'WingMPET','time_s':nav['time_s'],'FMU_operation':27,'physical_outputs':out,
            'reduced_inner_loop':dict(self.model),'maximum_total_thrust_N':10.06592,'mass_kg':.65,
            'parameter_source':'Phoenix source motor law / integration mass',
            'scope':'SITL native inner-loop predictive control; no ground-contact truth injection'}
