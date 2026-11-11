"""Mock-only native integration probes. No network, motor or FCU starts."""
from __future__ import annotations
import time
from types import SimpleNamespace
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter

class MockClient:
    def __init__(self):
        self.sent=[];self.cancelled=[];self.snapshot_calls=0;self.closed=False
    def submit(self, packet, **kwargs):
        self.sent.append(packet)
        return SimpleNamespace(ok=True,transaction_id=packet['transaction_id'],raw={})
    def cancel(self, **kwargs):
        self.cancelled.append(kwargs)
        return SimpleNamespace(ok=True,raw={'cancelled':True})
    def gui_status(self, **kwargs):
        self.snapshot_calls+=1
        return healthy_status()
    def build_layer7_input(self,packet,**kwargs):
        return {'ExecutionEvidence':{'source':'SIMULATED_MOCK_ONLY','execution_confirmed':False},
                'PredictionEvidence':{},'NetworkSnapshot':{}}
    def close(self):self.closed=True

def healthy_status(tx=None, sent=None):
    now=time.monotonic()
    return {'mavlink_connected':True,'mode':'QHOVER','armed':False,
        'plant_time_s':1.0,'command_first_sent_monotonic':sent,
        'manual_sent_monotonic':sent,'applied_transaction_id':tx,
        'latitude_deg':40.,'longitude_deg':-105.,'altitude_msl_m':1900.,
        'airspeed_m_s':0.,'heading_deg':0.,'groundspeed_m_s':0.,
        'velocity_n_m_s':0.,'velocity_e_m_s':0.,'velocity_d_m_s':0.,
        'attitude':{'roll':0.,'pitch':0.,'yaw':0.},
        'telemetry_age_s':{k:0. for k in ('HEARTBEAT','ATTITUDE','VFR_HUD','GLOBAL_POSITION_INT','SERVO_OUTPUT_RAW','SYS_STATUS','SCALED_IMU')},
        'telemetry_received_monotonic':{'ATTITUDE':now,'SERVO_OUTPUT_RAW':now},
        'applied_pilot_command':{'aileron':0.,'elevator':0.,'throttle':0.,'rudder':0.},
        'imu':{'frame':'BODY_FRD','source':'MAVLink/SCALED_IMU','plant_time_s':1.,
               'accel_x_m_s2':0.,'accel_y_m_s2':0.,'accel_z_m_s2':9.81,
               'gyro_x_rad_s':0.,'gyro_y_rad_s':0.,'gyro_z_rad_s':0.},
        'battery':{'remaining_percent':90.},'servo_output_raw':{}}

class ModelPrior:
    def __init__(self,ready=True,verified=False):
        self.ready=ready;self.verified=verified;self.calls=[]
    def online_control_readiness(self):
        return {'ok':self.ready,'nominal_control_ready':False,
                'flight_release_blockers':['native_pilot_controller_prediction_unavailable']}
    def verify_native_execution_evidence(self,decision,*,transaction_id):
        self.calls.append(transaction_id)
        return ({'ok':True,'native_execution_model_matched':True}
                if self.verified else
                {'ok':False,'reason':'native_pilot_controller_prediction_unavailable'})
    def ensure_native_control_binding(self):
        return {'ok':self.ready,'status':'TEST_PRIOR_ONLY'}
    def native_actuator_feedback(self,status):
        return {'ok':True,'feedback':{'source':'MOCK_NOT_NATIVE'}}

def native_adapter(*,ready=True,verified=False):
    cfg=SimpleNamespace(get=lambda key,default=None:default)
    model=ModelPrior(ready,verified)
    adapter=Layer6OnlineAdapter(cfg,model)
    client=MockClient()
    adapter._client=client
    adapter._session_open=True
    adapter._status=lambda **k: healthy_status()
    return adapter,client,model

def native_payload(*,final=True,tx='tx-1',package_tx='tx-1',candidate_id='one'):
    now=time.time()
    return {'learning_mode':'ONLINE_JSBSIM','transaction_id':tx,
      'layer5_output':{'result':{'gates':{'FINAL':final},
        'decision':{'candidate_id':'one'},
        'layer6_package':{'transaction_id':package_tx,'candidate_id':candidate_id,
          'u_cmd':[0.,0.,0.,0.], 'tau_apply':now-0.1,'tau_deadline':now+5,
          'C_exec':{'direct_pwm':False},'S_safe':{'psf':True,'rta':True},
          'execution_intent':{'requested_pilot_mode':'QHOVER','discrete':[]}}}}}
