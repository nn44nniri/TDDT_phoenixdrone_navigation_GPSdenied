#!/usr/bin/env python3
"""Reproducible physical-weather position coupling, never a navigation sensor.

The FCU sensor EKF without XY aiding need not export a global position. Weather
forces must be evaluated at the physical simulator point, not at an invalid EKF
coordinate. SIM_STATE is confined to physics forcing and independent displays.
Its time is conservatively bounded by adjacent ordered native-clock events.
"""
import argparse,hashlib,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BASE_SHA='9565c71ddf31e6b3438a6d1c0a74dc99ebba707454e8a8aae19edb7d11837b25'


def content():
    base=(ROOT/'resources/r4_02_v1/phoenix_env_adapter/bound.py').read_bytes()
    if hashlib.sha256(base).hexdigest()!=BASE_SHA:raise ValueError('unknown base weather adapter')
    text=base.decode()
    text=text.replace("type=['ATTITUDE','GLOBAL_POSITION_INT','SYSTEM_TIME']", "type=['ATTITUDE','GLOBAL_POSITION_INT','SYSTEM_TIME','SIM_STATE']")
    before="""            observation=clock.observe(msg,received=time.monotonic())"""
    after="""            received=time.monotonic()
            if msg.get_type()=='SIM_STATE':
                if (msg.get_srcSystem(),msg.get_srcComponent())!=target:continue
                if newest_observation is None:continue
                lat=getattr(msg,'lat_int',0) or msg.lat
                lon=getattr(msg,'lon_int',0) or msg.lon
                position=dict(latitude=lat*1e-7,longitude=lon*1e-7,altitude_msl_m=msg.alt)
                if not all(math.isfinite(v) for v in position.values()):
                    raise ValueError('invalid physical weather position')
                # Lower clock bound precedes this SIM_STATE on the same stream.
                # Sampling occurs only on the subsequent native clock event.
                position_time=newest_observation['event']['plant_time_s']
                position_received=received
                continue
            observation=clock.observe(msg,received=received)"""
    if text.count(before)!=1:raise ValueError('weather clock seam changed')
    text=text.replace(before,after)
    start=text.index("            if channel=='GLOBAL_POSITION_INT':")
    end=text.index("            if observation['newest']:",start)
    text=text[:start]+text[end:]
    text=text.replace("environment_position_source='MAVLink/GLOBAL_POSITION_INT',", "environment_position_source='MAVLink/SIM_STATE physical forcing only',\n                environment_position_time_bounds_s=[position_time,plant],\n                position_clock_semantics='ordered lower/upper native clock bounds, not a fabricated SIM_STATE timestamp',\n                navigation_observation_generated=False,")
    return base,text.encode()


def apply(root,restore=False):
    target=Path(root)/'phoenix_env_adapter/bound.py';base,overlay=content()
    existing=target.read_bytes() if target.exists() else None
    if restore:
        if existing!=overlay:return {'status':'DELEGATED_TO_PINNED_R4_INSTALLER','modified':False}
        data=base
    else:
        if existing not in (base,overlay):raise ValueError('unknown installed weather adapter; refusing overwrite')
        data=overlay
    if data!=existing:target.write_bytes(data)
    return {'status':'PASS','phase':'base' if restore else 'physical-weather',
            'source_sha256':hashlib.sha256(data).hexdigest(),'new_sensor_types':[],
            'MESA_navigation_input':False,'truth_position_scope':'simulator weather forcing only'}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--environment-root',required=True,type=Path);p.add_argument('--restore-base',action='store_true');a=p.parse_args()
    print(json.dumps(apply(a.environment_root,a.restore_base)))
