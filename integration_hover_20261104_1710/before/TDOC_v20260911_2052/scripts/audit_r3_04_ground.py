#!/usr/bin/env python3
"""Run the installed JSBSim in independent ground states, without FCU or ARM."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import uuid
import xml.etree.ElementTree as ET

from tddt_tdoc.ground_contact_audit import PROPERTIES,analyze,policy
from tddt_tdoc.phoenix_geometry import validate_native_geometry


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def run_case(binary,model,reset,out,*,dt,duration,settled_from,negative=False):
    started=datetime.now(timezone.utc).isoformat();run_id=uuid.uuid4().hex
    out.mkdir()
    aircraft=out/'aircraft/phoenix_v3';aircraft.mkdir(parents=True)
    data=model.read_bytes()
    if negative:
        tree=ET.fromstring(data)
        for c in tree.findall('ground_reactions/contact'):
            c.find('spring_coeff').text='0';c.find('damping_coeff').text='0'
        data=ET.tostring(tree)
    (aircraft/'phoenix_v3.xml').write_bytes(data);(aircraft/'reset.xml').write_bytes(reset.read_bytes())
    before={name:sha(aircraft/name) for name in ('phoenix_v3.xml','reset.xml')}
    logger=ET.Element('output',{'name':str(out/'ground.csv'),'type':'CSV','rate':'200'})
    ET.SubElement(logger,'ground_reactions').text='ON'
    for prop in PROPERTIES:ET.SubElement(logger,'property').text=prop
    ET.ElementTree(logger).write(out/'logging.xml',encoding='unicode')
    script=ET.Element('runscript',{'name':'R3.04 isolated ground; no FCU or ARM'})
    ET.SubElement(script,'use',{'aircraft':'phoenix_v3','initialize':'reset'})
    run=ET.SubElement(script,'run',{'start':'0','end':str(duration),'dt':str(dt)})
    for key in ('fcs/left-motor-cmd-norm','fcs/right-motor-cmd-norm'):
        ET.SubElement(run,'property',{'value':'0'}).text=key
    ET.ElementTree(script).write(out/'run.xml',encoding='unicode')
    argv=[str(binary),'--root='+str(out),'--script='+str(out/'run.xml'),
          '--logdirectivefile='+str(out/'logging.xml'),'--outputpath='+str(out),'--nohighlight']
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',TMPDIR='/tmp')
    process=subprocess.run(argv,cwd=out,env=env,text=True,capture_output=True,timeout=45)
    (out/'JSBSIM.log').write_text(process.stdout+process.stderr)
    if process.returncode:raise RuntimeError(f'JSBSim failed: {out}')
    assert before=={name:sha(aircraft/name) for name in before}
    result=analyze(out/'ground.csv',duration_s=duration,settled_from_s=settled_from)
    result.update(argv=argv,JSBSim_sha256=sha(binary),input_sha256=before,
                  run_id=run_id,started_utc=started,csv_sha256=sha(out/'ground.csv'),
                  analyzer_sha256=sha(Path(__import__('tddt_tdoc.ground_contact_audit',fromlist=['x']).__file__)),
                  audit_script_sha256=sha(Path(__file__)),
                  installed_aircraft_sha256=sha(model),installed_reset_source_sha256=sha(reset),
                  negative_control=negative,dt_s=dt,FCU_executed=False,ARM_sent=False,
                  fixture_used=False,state_clamping=False,trim_used=False)
    (out/'RESULT.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args();tdoc=Path(__file__).resolve().parents[1]
    fs=tdoc.parent/'layer6_v20261007_1032/FlightStack'
    binary=fs/'jsbsim/build/src/JSBSim';model=fs/'ardupilot/Tools/autotest/aircraft/phoenix_v3/phoenix_v3.xml'
    reset=model.with_name('reset.xml');template=tdoc/'src/tddt_tdoc/resources/phoenix_fourfoot/r3_02_v1/reset.xml'
    criteria=policy()
    alignment=validate_native_geometry(SimpleNamespace(path_value=lambda *a,**kw:fs.parent))
    if sha(template)!=alignment['files']['reset.xml']['sha256']:
        raise ValueError('aligned Reset template hash mismatch')
    if sha(binary)!=criteria['JSBSim_sha256'] or sha(model)!=criteria['aircraft_sha256']:
        raise ValueError('unrecognized native simulator/model; revalidation required')
    out=args.output_dir.resolve();out.relative_to(tdoc/'reports/online');out.mkdir(exist_ok=False)
    (out/'CRITERIA_BEFORE_RUN.json').write_text(json.dumps(criteria,indent=2)+'\n')
    cases={}
    for name,dt,source,duration,settled,negative in (
        ('native_reset',1/1200,reset,30,20,False),
        ('refined_dt',1/2400,reset,30,20,False),
        ('template_yaw53',1/1200,template,30,20,False),
        ('negative_no_stiffness',1/1200,reset,2,1.5,True)):
        cases[name]=run_case(binary,model,source,out/name,dt=dt,duration=duration,settled_from=settled,negative=negative)
    height_delta=abs(cases['native_reset']['metrics']['final_CG_AGL_m']-cases['refined_dt']['metrics']['final_CG_AGL_m'])
    accepted=all(cases[n]['status']=='PASS' for n in ('native_reset','refined_dt','template_yaw53'))
    accepted=accepted and height_delta<1e-5
    rejected=cases['negative_no_stiffness']['status']=='FAIL' and 'CG_COLLAPSE' in cases['negative_no_stiffness']['violations'] and 'UNSUPPORTED_WEIGHT' in cases['negative_no_stiffness']['violations']
    result={'step':'R3.04','status':'PASS' if accepted and rejected else 'FAIL',
            'positive_cases':{n:cases[n]['status'] for n in ('native_reset','refined_dt','template_yaw53')},
            'negative_control_rejected':rejected,'negative_violations':cases['negative_no_stiffness']['violations'],
            'dt_refinement_height_difference_m':height_delta,
            'model_changed':False,'FCU_executed':False,'ARM_sent':False,
            'scope':'flat zero-wind terrain, zero motors, pinned native model/simulator',
            'flight_ready':False,'fixture_decision_pending':'R3.05','model_compatibility_pending':'R3.06'}
    (out/'GROUND_SUITE_RESULT.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))
    if result['status']!='PASS':raise SystemExit(1)


if __name__=='__main__':main()
