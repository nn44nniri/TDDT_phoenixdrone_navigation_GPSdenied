#!/usr/bin/env python3
"""Reproducible interpreted JSBSim profile, using existing WingMPET VTOL physics.

This changes the model XML and parameter source, never an opaque binary. The
standard FlightStack installer invokes it after restoring/building its base.
Original contact geometry, mass, inertia and motor law remain source-bound.
"""
import argparse,hashlib,json,math
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
CONFIG=ROOT/'configs'
POUND_FORCE=4.4482216152605
POUND_FOOT=1.3558179483314


def model_bytes():
    root=ET.fromstring((CONFIG/'phoenix_native_base.xml').read_bytes())
    root.set('name','Phoenix native VTOL/WingMPET blend RT-v1')
    aero=root.find('aerodynamics')
    fun=ET.Element('function',name='aero/tddt-wing-blend')
    table=ET.SubElement(fun,'table');ET.SubElement(table,'independentVar',lookup='row').text='velocities/u-aero-fps'
    rows=[]
    for v in [-30.,0.,9.1,9.5,10.,10.5,11.,11.5,12.,30.]:
        u=max(0.,min(1.,(v-9.1)/(12.-9.1)));rows.append(f'{v/.3048:.12g} {u*u*(3-2*u):.12g}')
    ET.SubElement(table,'tableData').text='\n'+'\n'.join(rows)+'\n';aero.insert(0,fun)
    for axis in aero.findall('axis'):
        for function in axis.findall('function'):
            product=function.find('product')
            if product is None:raise ValueError('unrecognized source aerodynamic expression')
            ET.SubElement(product,'property').text='aero/tddt-wing-blend'
    external=root.find('external_reactions')
    def expression(parent,motor,delta,coefficient,squared_delta=False):
        product=ET.SubElement(ET.SubElement(parent,'function'),'product')
        ET.SubElement(product,'value').text=f'{coefficient:.16g}'
        diff=ET.SubElement(product,'difference');ET.SubElement(diff,'value').text='1'
        ET.SubElement(diff,'property').text='aero/tddt-wing-blend'
        for _ in range(2):ET.SubElement(product,'property').text=f'fcs/{motor}-motor-pos-norm'
        ET.SubElement(product,'property').text=f'fcs/{delta}-elevon-pos-rad'
        if squared_delta:ET.SubElement(product,'property').text=f'fcs/{delta}-elevon-pos-rad'
    for side,y in [('left',-.195),('right',.195)]:
        # WingMPET source: L=k_L omega^2 delta, D=k_D omega^2 delta^2,
        # M_pitch=k_M omega^2 delta. JSBSim force/moment functions use lbf/lbft.
        for kind,k,direction,squared in [('lift',3.48e-6,(0,0,-1),False),('drag',1.75e-6,(-1,0,0),True)]:
            force=ET.SubElement(external,'force',name=f'{side}_native_propwash_{kind}',frame='BODY')
            expression(force,side,side,k*800**2/POUND_FORCE,squared)
            loc=ET.SubElement(force,'location',unit='M')
            for name,value in zip(('x','y','z'),(-.036,y,0.)):ET.SubElement(loc,name).text=str(value)
            vec=ET.SubElement(force,'direction')
            for name,value in zip(('x','y','z'),direction):ET.SubElement(vec,name).text=str(value)
        moment=ET.SubElement(external,'moment',name=f'{side}_native_propwash_pitch',frame='BODY')
        expression(moment,side,side,-3.44e-7*800**2/POUND_FOOT)
        vec=ET.SubElement(moment,'direction')
        for name,value in zip(('x','y','z'),(0,1,0)):ET.SubElement(vec,name).text=str(value)
    ET.indent(root,space='  ')
    return b'<?xml version="1.0"?>\n'+ET.tostring(root,encoding='utf-8')+b'\n'


def expected_hash():return hashlib.sha256(model_bytes()).hexdigest()


def apply(fs):
    fs=Path(fs);data=model_bytes();parameters=(CONFIG/'native_realtime_vtol.parm').read_bytes()
    outputs=[fs/'models/phoenix_v3/aircraft/phoenix_v3/phoenix_v3.xml',
             fs/'ardupilot/Tools/autotest/aircraft/phoenix_v3/phoenix_v3.xml']
    param_outputs=[fs/'models/phoenix_v3/default_params/phoenix_v3.parm',
                   fs/'ardupilot/Tools/autotest/default_params/phoenix_v3.parm']
    for p in outputs:p.write_bytes(data)
    for p in param_outputs:p.write_bytes(parameters)
    manifest={'schema':'Phoenix-native-realtime-model-build-v1','model_sha256':expected_hash(),
        'parameter_sha256':hashlib.sha256(parameters).hexdigest(),
        'source_xml_sha256':hashlib.sha256((CONFIG/'phoenix_native_base.xml').read_bytes()).hexdigest(),
        'generator_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'model_type':'interpreted JSBSim XML; no C++/FMU/binary mutation',
        'wing_blend_speed_m_s':[9.1,12.],'propwash_source':'existing Phoenix/WingMPET source coefficients',
        'surface_sign_source':'ArduPlane pitch demand versus source physical Cm_delta',
        'GNSS_receivers':'disabled','new_sensor_types':[],
        'flight_ready':False,'dynamic_qualification':'requires same-session Native test'}
    (fs/'models/phoenix_v3/native_realtime_build.json').write_text(json.dumps(manifest,indent=2))
    return manifest


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--flightstack-dir',type=Path,default=ROOT/'FlightStack');a=p.parse_args()
    print(json.dumps(apply(a.flightstack_dir),indent=2))
