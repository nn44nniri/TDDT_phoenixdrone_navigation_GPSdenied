#!/usr/bin/env python3
from pathlib import Path
import hashlib
import argparse
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / 'aircraft' / 'phoenix_v3' / 'phoenix_v3.xml'
RESET = ROOT / 'aircraft' / 'phoenix_v3' / 'reset.xml'
PARM = ROOT / 'default_params' / 'phoenix_v3.parm'
PATCH = ROOT / 'patches' / 'SIM_JSBSim_phoenix_v3.patch'
PATCH_V2 = ROOT / 'patches' / 'SIM_JSBSim_phoenix_v3_v2_to_v3.patch'
PATCHED_SRC = ROOT / 'patched_source' / 'SIM_JSBSim.cpp'

REQUIRED_MODEL = [
    'fcs/left-elevon-cmd-norm', 'fcs/right-elevon-cmd-norm',
    'fcs/left-motor-cmd-norm', 'fcs/right-motor-cmd-norm',
    'left_motor_thrust', 'right_motor_thrust',
    'TAIL_FOOT_L1', 'TAIL_FOOT_L2', 'TAIL_FOOT_R1', 'TAIL_FOOT_R2'
]
REQUIRED_PARAMS = [
    'Q_ENABLE 1', 'Q_TAILSIT_ENABLE 1', 'Q_FRAME_CLASS 10', 'Q_TAILSIT_MOTMX 0',
    'SERVO1_FUNCTION 77', 'SERVO2_FUNCTION 78',
    'SERVO3_FUNCTION 73', 'SERVO4_FUNCTION 74',
    'ARSPD_TYPE 100', 'ARSPD_USE 1', 'ARSPD_SKIP_CAL 1', 'ARSPD_AUTOCAL 0'
]
REQUIRED_PATCHED_SOURCE = [
    'strcmp(jsbsim_model, "phoenix_v3") == 0',
    'fcs/left-elevon-cmd-norm', 'fcs/right-elevon-cmd-norm',
    'fcs/left-motor-cmd-norm', 'fcs/right-motor-cmd-norm',
    'Do not specify gamma for the zero-speed tailsitter initial condition',
    '<ubody unit=\\"FT/SEC\\"> 0.0 </ubody>',
    '<theta unit=\\"DEG\\"> 89.0 </theta>',
]

def fail(msg):
    print(f'FAIL: {msg}')
    raise SystemExit(1)

def text_value(root, tag):
    el = root.find(tag)
    return None if el is None else (el.text or '').strip()

def check_xml():
    ET.parse(MODEL)
    root = ET.parse(RESET).getroot()
    alt = float(text_value(root, 'altitudeAGL'))
    theta = float(text_value(root, 'theta'))
    if not abs(alt - 0.141025821708132) < 1e-12: fail(f'reset CG AGL mismatch: {alt}')
    if abs(theta - 89.0) > 1e-6: fail(f'reset theta must be 89 deg, got {theta}')
    if root.find('gamma') is not None:
        fail('Phoenix reset must not contain gamma; gamma can override/recompute theta in JSBSim IC processing')
    for tag in ('ubody','vbody','wbody'):
        v = text_value(root, tag)
        if v is None or abs(float(v)) > 1e-9: fail(f'Phoenix reset requires {tag}=0, got {v}')
    print('PASS: four-foot Reset uses explicit CG AGL, 89 deg and zero body speed.')

def check_contracts():
    if hashlib.sha256(MODEL.read_bytes()).hexdigest() != '9118bfac6cb1bbaa05142f1c0f419fe570414be8b1dbdeffb591e7b898f64da9':
        fail('four-foot aircraft reference hash mismatch')
    text = MODEL.read_text()
    missing = [s for s in REQUIRED_MODEL if s not in text]
    params = PARM.read_text()
    missing += [s for s in REQUIRED_PARAMS if s not in params]
    if missing: fail('missing model/parameter contracts: ' + ', '.join(missing))
    print('PASS: Phoenix model, tailsitter outputs and SITL airspeed parameter contracts are present.')

def check_patch_file(path):
    ptxt = path.read_text()
    if '*** End Patch' in ptxt or '\n@@\n' in ptxt: fail(f'{path.name} contains non-unified-diff markers')
    if not ptxt.startswith('--- a/libraries/SITL/SIM_JSBSim.cpp\n+++ b/libraries/SITL/SIM_JSBSim.cpp\n'):
        fail(f'{path.name} header is not a valid -p1 unified diff')

def check_patched_source(path):
    if hashlib.sha256(path.read_bytes()).hexdigest() != 'ba81eaa1cb0bc8b4a3b4bdf82fb26ca8c03e4e2d24a1615a20b56c19dc28d788':
        fail('R3.03 generated Reset source hash mismatch')
    text = path.read_text()
    missing = [s for s in REQUIRED_PATCHED_SOURCE if s not in text]
    if missing: fail(f'{path} missing Phoenix v3 integration markers: ' + ', '.join(missing))
    print(f'PASS: Phoenix v3 runscript/reset/servo mapping present in {path}.')

def dry_run_or_installed(ap_root):
    target = ap_root / 'libraries' / 'SITL' / 'SIM_JSBSim.cpp'
    if not target.is_file(): fail(f'ArduPilot source not found: {target}')
    try:
        check_patched_source(target)
        print('PASS: ArduPilot checkout already contains Phoenix v3 integration.')
        return
    except SystemExit:
        pass
    for patch in (PATCH_V2, PATCH):
        cp = subprocess.run(['patch','--dry-run','-p1','-i',str(patch)], cwd=ap_root,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if cp.returncode == 0:
            print(f'PASS: {patch.name} dry-run applies cleanly to this checkout.')
            return
    fail('neither stock->v3 nor v2->v3 patch applies cleanly to this ArduPilot checkout')

def check_runtime(ap_root):
    start = ap_root/'Tools'/'autotest'/'jsbsim_start_0.xml'
    reset = ap_root/'Tools'/'autotest'/'aircraft'/'phoenix_v3'/'reset.xml'
    if not start.is_file(): fail(f'runtime runscript not found: {start}; launch phoenix_v3 once first')
    st = start.read_text()
    if 'propulsion/engine[0]/set-running' in st: fail('runtime runscript contains stock engine-start event')
    if 'simulation/do_simple_trim' in st: fail('runtime runscript contains stock simple-trim event')
    rr = ET.parse(reset).getroot()
    if rr.find('gamma') is not None: fail('runtime Phoenix reset still contains gamma')
    theta=float(text_value(rr,'theta')); alt=float(text_value(rr,'altitudeAGL'))
    if abs(theta-89.0)>0.01 or not abs(alt-0.141025821708132)<1e-12: fail(f'runtime reset wrong: altitude={alt}, theta={theta}')
    for tag in ('ubody','vbody','wbody'):
        v=text_value(rr,tag)
        if v is None or abs(float(v))>1e-9: fail(f'runtime reset requires {tag}=0, got {v}')
    print('PASS: generated Phoenix runscript/reset are v3-correct (no stock events, no gamma, theta=89).')

def check_log(path):
    text=path.read_text(errors='replace')
    if 'Opened JSBSim control socket' not in text: fail('runtime log lacks "Opened JSBSim control socket"')
    if 'No property, "propulsion/engine[0]/set-running" is defined' in text: fail('runtime log contains legacy engine-start failure')
    if 'PANIC: Failed to open JSBSim control socket' in text: fail('runtime log contains JSBSim control socket panic')
    print('PASS: runtime log confirms JSBSim control socket startup and no legacy blocker.')

p=argparse.ArgumentParser()
p.add_argument('--ardupilot-root', type=Path)
p.add_argument('--check-runtime', action='store_true')
p.add_argument('--runtime-log', type=Path)
a=p.parse_args()
for f in (MODEL,RESET,PARM,PATCH,PATCH_V2,PATCHED_SRC):
    if not f.is_file(): fail(f'missing packaged file: {f}')
check_xml(); check_contracts(); check_patch_file(PATCH); check_patch_file(PATCH_V2); check_patched_source(PATCHED_SRC)
if a.ardupilot_root:
    ap=a.ardupilot_root.resolve(); dry_run_or_installed(ap)
    if a.check_runtime: check_runtime(ap)
if a.runtime_log: check_log(a.runtime_log.resolve())
print('PASS: Phoenix v3 package validation complete.')
