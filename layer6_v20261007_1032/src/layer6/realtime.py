"""Same-session telemetry and GUI execution receipts for the SITL realtime profile.

Only IMU, magnetometer, barometer and pitot enter the measurement interface.
SIM_STATE is recorded only for independent physical validation; odometry and
vision are never requested. None is exposed as a navigation measurement.
GPS and FCU state messages are validation-only, never navigation inputs.
This connection sends stream/parameter requests, never flight commands.
"""
from collections import deque
from copy import deepcopy
import hashlib
import json
import socket
import threading
import time
import uuid

from pymavlink import mavutil


class RealtimeIO:
    def __init__(self, session_id, log_path):
        self.session_id=session_id
        self.latest={};self.imu=deque(maxlen=10000)
        self.parameters={};self.text=deque(maxlen=50);self.lock=threading.Lock();self.stop=False
        self.master=mavutil.mavlink_connection('tcp:127.0.0.1:14553',source_system=254,source_component=192)
        hb=self.master.wait_heartbeat(timeout=12)
        if hb is None or hb.get_srcSystem()!=1:raise RuntimeError('same Native FCU heartbeat unavailable')
        self._put(hb)
        for name,hz in [('HIGHRES_IMU',100),('SCALED_IMU',50),('VFR_HUD',20),
                        ('SERVO_OUTPUT_RAW',20),('EXTENDED_SYS_STATE',5),('GPS_RAW_INT',2),('SIM_STATE',10)]:
            self.master.mav.command_long_send(1,1,mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,0,
                getattr(mavutil.mavlink,'MAVLINK_MSG_ID_'+name),int(1e6/hz),0,0,0,0,0)
        self.log=open(log_path,'a',buffering=1)
        self.thread=threading.Thread(target=self._read,daemon=True);self.thread.start()
        self.rpc=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);self.rpc.bind(('127.0.0.1',0))

    def _put(self,msg):
        if msg.get_srcSystem()!=1 or msg.get_srcComponent()!=1:return
        now=time.monotonic();name=msg.get_type();wire=bytes(msg.get_msgbuf())
        row={'type':name,'received_monotonic':now,'data':msg.to_dict(),
             'wire_sha256':hashlib.sha256(wire).hexdigest(),'wire_hex':wire.hex(),
             'native_session_id':self.session_id}
        with self.lock:
            if name=='HIGHRES_IMU':
                row['plant_time_s']=msg.time_usec*1e-6;self.imu.append(row)
            if name=='SCALED_IMU':row['plant_time_s']=msg.time_boot_ms*.001
            if name=='PARAM_VALUE':
                key=msg.param_id.decode().rstrip('\x00') if isinstance(msg.param_id,bytes) else msg.param_id
                self.parameters[key]={'value':float(msg.param_value),'wire_sha256':row['wire_sha256']}
            if name=='STATUSTEXT':self.text.append(str(msg.text))
            self.latest[name]=row
        return row

    def _read(self):
        try:
            while not self.stop:
                msg=self.master.recv_match(blocking=True,timeout=.05)
                if msg is None:continue
                row=self._put(msg)
                if row:self.log.write(json.dumps(row,separators=(',',':'))+'\n')
        except Exception as exc:self.error=repr(exc)

    def request_parameters(self,names,timeout=8.):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            with self.lock:missing=[n for n in names if n not in self.parameters]
            if not missing:return {n:deepcopy(self.parameters[n]) for n in names}
            for n in missing:self.master.mav.param_request_read_send(1,1,n.encode(),-1)
            time.sleep(.15)
        raise RuntimeError('parameter readback missing: '+str(missing))

    def set_surveyed_origin(self,origin,timeout=12.):
        """Set the known launch coordinate frame, never inject a GNSS fix.

        SET_GPS_GLOBAL_ORIGIN is the MAVLink name for configuring EKF's frame;
        no GPS_INPUT/HIL_GPS/position/velocity observation is transmitted.
        """
        lat=int(round(origin['latitude_deg']*1e7));lon=int(round(origin['longitude_deg']*1e7))
        alt=int(round(origin['terrain_elevation_msl_m']*1000.));end=time.monotonic()+timeout
        while time.monotonic()<end:
            self.master.mav.set_gps_global_origin_send(1,lat,lon,alt)
            self.master.mav.command_long_send(1,1,mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE,0,
                mavutil.mavlink.MAVLINK_MSG_ID_GPS_GLOBAL_ORIGIN,0,0,0,0,0,0)
            time.sleep(.3)
            with self.lock:row=deepcopy(self.latest.get('GPS_GLOBAL_ORIGIN'))
            if row:
                d=row['data']
                if abs(d['latitude']-lat)<=1 and abs(d['longitude']-lon)<=1 and abs(d['altitude']-alt)<=100:
                    return {'source':'surveyed launch frame; no GNSS observation','verified_readback':row}
        raise RuntimeError('surveyed EKF origin frame readback unavailable')

    def sample(self,after):
        with self.lock:
            latest=deepcopy(self.latest)
            samples=[deepcopy(r) for r in self.imu if r['plant_time_s']>after]
        for name,age in [('HIGHRES_IMU',.3),('HEARTBEAT',2.),('VFR_HUD',.5)]:
            if name not in latest or not 0<=time.monotonic()-latest[name]['received_monotonic']<=age:
                raise RuntimeError('stale or missing '+name+'; '+str(getattr(self,'error',None)))
        # Strict projection: no FCU position, velocity or attitude crosses this
        # sensor interface, even though the independent validation log has them.
        h=latest['HIGHRES_IMU']
        pitot=latest['VFR_HUD']
        measurements={'HIGHRES_IMU':h,'AIRSPEED':{
            'received_monotonic':pitot['received_monotonic'],
            'wire_sha256':pitot['wire_sha256'],'airspeed_m_s':pitot['data']['airspeed'],
            'source':'AP_Airspeed via VFR_HUD.airspeed only'},
            'ULTRASONIC_WIND':{'present':False,'reason':'no actual sensor sample'}}
        return measurements,samples,latest

    def receipt(self,timeout=.12):
        request=uuid.uuid4().hex;end=time.monotonic()+timeout
        self.rpc.sendto(json.dumps({'type':'realtime_receipt_request','request_id':request,
                                   'native_session_id':self.session_id}).encode(),('127.0.0.1',14660))
        while time.monotonic()<end:
            self.rpc.settimeout(max(.001,end-time.monotonic()))
            try:data,peer=self.rpc.recvfrom(65535)
            except socket.timeout:return None
            if peer!=('127.0.0.1',14660):continue
            reply=json.loads(data)
            if (reply.get('schema')=='Layer6-realtime-receipt-v1' and reply.get('request_id')==request
                and reply.get('native_session_id')==self.session_id):return reply
        return None

    def close(self):
        self.stop=True;self.thread.join(timeout=2);self.log.close();self.rpc.close();self.master.close()
