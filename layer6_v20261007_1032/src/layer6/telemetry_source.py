"""Pinned local sender semantics for R5.03; no remote-peer attestation."""
import hashlib
import json
import os
from pathlib import Path

PINS={'libraries/GCS_MAVLink/GCS_Common.cpp': '6a2240bb47b287abab6c1b2b16356dc5bdd26e8a06e07db7f0761f57472118e8', 'ArduPlane/GCS_MAVLink_Plane.cpp': '6613576d08aacd6751e338f384ab39b875658be86023ed766ab2423d7033a09f', 'libraries/AP_AHRS/AP_AHRS.cpp': '5d7299be22da98843a7927d29ec6a5c4106c1630217c331204272dcf27a58c5c', 'libraries/AP_AHRS/AP_AHRS_View.cpp': 'f3be05925b889d027fbff2dfe3a5c2fbfa71e2a79bce1f615579d585b845ce5a', 'libraries/AP_Math/quaternion.cpp': '86f59857f0a1c7e853b19fe473340297ba271d2831d6da7496126fa73948c276', 'libraries/AP_NavEKF3/AP_NavEKF3_Outputs.cpp': 'f2214b959f986c39cf0ccf12def9601f169b33a6ba75c80802a4ca1787e62ca3', 'libraries/AP_InertialSensor/AP_InertialSensor_Backend.cpp': '9618e915e155b8f17c7ef2ede4d779c308fb49a000cf47465c499e21c0ac1987', 'modules/mavlink/message_definitions/v1.0/common.xml': 'fbac92d0689c9b1bc03b49ebc5cdf989f3fc23026c3c856f7224e4b9af1b5f53', 'modules/mavlink/message_definitions/v1.0/ardupilotmega.xml': '8cff0bf2ae5bec60d16118bf37e1ec6e9f55f54aecc486df0b18f89977479047', 'build/sitl/bin/arduplane': '336b4d0e9a0bcf326eafc1f5b027f9a2c5114f16d9efdb154ea36d17f2d1d056', 'libraries/AP_AHRS/AP_AHRS_SIM.cpp': 'b5530729f799b277f38bdd6dc451edaf7b24ec1208ecd8e9feb4e2e1cf3d5ad4', 'libraries/AP_AHRS/AP_AHRS_Backend.h': 'dae52844052c38d068f4ec6476d5037affaadbd62fc872f43bbb8055df5f2535', 'ArduPlane/tailsitter.cpp': '6a6023cfbcd456f56d19c7d22fd2d5e1d3b056a3804640df96455ab434b57467', 'ArduPlane/quadplane.cpp': '4bf1946ed0c211da57be85a432116d4f1923d4240705bf3922723957ff00d99a', 'Tools/autotest/default_params/phoenix_v3.parm': 'e030ab28268e4fe33451b73ca64b6dcbec44a7732e0854e67eaecf153c90d4cc', 'libraries/SITL/SIM_JSBSim.cpp': 'c266189df071f2c3e7eb857e9173fa1f7167529fa2b8f8c2ac396e20b558d192', 'libraries/SITL/SIM_JSBSim.h': '265ac98833293b696989aea2990c226244d48c928a42d9ec77e9a4c262c47005', 'libraries/SITL/SIM_JSBSimBodyRates.h': '9d0a7a6b05e83fa9fb304f8f9cefd9b0a2619a601ae08ef9154ef41efb43687a'}

def native_source_contract(root=None):
    root=Path(root or os.environ.get('TDDT_LAYER6_ROOT') or Path(__file__).resolve().parents[2])
    ap=root/'FlightStack/ardupilot';actual={};errors=[]
    for name,expected in PINS.items():
        try:value=hashlib.sha256((ap/name).read_bytes()).hexdigest()
        except OSError:value=None
        actual[name]=value
        if value!=expected:errors.append(name)
    identity=hashlib.sha256(json.dumps(PINS,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'schema':'Layer6-local-sender-contract-r5.03-v1','contract_id':identity,
        'deployment_verified':not errors,'mismatched_files':errors,'observed_sha256':actual,
        'default_AHRS_EKF_TYPE':3,'default_backend':'AP_NavEKF3 (configuration; health requires runtime evidence)','GNSS_denied_estimator_verified':False,
        'scope':'pinned_local_ArduPilot_sender_semantics','peer_attested':False,
        'imu_frame':'BODY_FRD','attitude_frame':'BODY_FRD_TO_NED',
        'quaternion_order':'wxyz','display_offset_applied_to_body':False,
        'body_rate_transport':'JSBSim SOCKET native p/q/r v1 matched to FlightGear frame; no Euler inversion',
        'body_rate_frame':'BODY_FRD','body_rate_unit':'rad/s'}
