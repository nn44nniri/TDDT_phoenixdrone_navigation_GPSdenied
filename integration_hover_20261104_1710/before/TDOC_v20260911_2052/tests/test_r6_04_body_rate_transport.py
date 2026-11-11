"""Actual C++ transport decoder and durable installer chain; no fake flight claim."""
from pathlib import Path
import hashlib
import importlib.util
import json
import math
import shutil
import struct
import subprocess

import pytest

ROOT=Path(__file__).resolve().parents[2]
NATIVE=ROOT/'layer6_v20261007_1032'
RESOURCE=NATIVE/'resources/r6_04_body_rates_v1'


def module(path,name):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


@pytest.fixture(scope='module')
def decoder(tmp_path_factory):
    directory=tmp_path_factory.mktemp('body_rate_cpp');source=directory/'driver.cpp'
    source.write_text('''#include "SIM_JSBSimBodyRates.h"
#include <iostream>
#include <iomanip>
#include <string>
int main(int argc, char **argv) {
  std::string line;
  while (std::getline(std::cin,line)) {
    if (argc > 1) {
      std::cout << (SITL::JSBSimBodyRates::header_matches((line+"\\n").c_str()) ? "HEADER" : "REJECT") << "\\n";
    } else {
      SITL::JSBSimBodyRates sample;
      if (!SITL::JSBSimBodyRates::decode(line.c_str(),sample)) { std::cout << "REJECT\\n"; continue; }
      std::cout << std::setprecision(17) << sample.frame_time_us() << "," << sample.p << "," << sample.q << "," << sample.r << "\\n";
    }
  }
  return 0;
}
''')
    exe=directory/'decoder'
    subprocess.run(['g++','-std=c++17','-Wall','-Wextra','-Werror','-Wno-unused-parameter',
                    '-I'+str(NATIVE/'FlightStack/ardupilot/libraries/SITL'),str(source),'-o',str(exe)],check=True,capture_output=True)
    return exe


def decode(exe,packet,header=False):
    return subprocess.run([str(exe),*(['header'] if header else [])],input=packet+'\n',text=True,capture_output=True,check=True).stdout.strip()


@pytest.mark.parametrize('packet',['','0,0,0','1,0,0,0,9','-1,0,0,0','nan,0,0,0','inf,0,0,0',
                                    '1,nan,0,0','1,0,inf,0','1,0,0,-inf','1,1e300,0,0','1,0,0,0 junk'])
def test_invalid_wire_is_rejected_without_zero_fill(decoder,packet):
    assert decode(decoder,packet)=='REJECT'


def test_version_and_units_in_header_are_mandatory(decoder):
    good='<LABELS> ,Time,TDDT_BODY_RATES_V1_P_RAD_S,TDDT_BODY_RATES_V1_Q_RAD_S,TDDT_BODY_RATES_V1_R_RAD_S'
    assert decode(decoder,good,True)=='HEADER'
    for bad in (good.replace('V1','V2'),good.replace('RAD_S','DEG_S'),good.replace('_P_','_R_'),'Time,P,Q,R'):
        assert decode(decoder,bad,True)=='REJECT'


def test_real_large_rates_are_not_clipped_to_acceptance_threshold(decoder):
    values=list(map(float,decode(decoder,'18.66753,1.98298895359,-2.0,0.4').split(',')))
    assert values[1:]==pytest.approx([1.98298895359,-2.,.4],abs=1e-7)
    assert max(map(abs,values[1:]))>.1


def test_native_rate_transport_avoids_demonstrated_euler_singularity(decoder):
    p,q,r=8.039615631281788e-10,5.3025580177944046e-8,-9.729506848225758e-9
    values=list(map(float,decode(decoder,f'18.66753,{p:.17g},{q:.17g},{r:.17g}').split(',')))
    assert values[1:]==pytest.approx([p,q,r],abs=1e-14)
    # Numeric regression extracted from the native baseline, not a flight model.
    f32=lambda x:struct.unpack('f',struct.pack('f',x))[0]
    legacy_p=f32(-729823488.)-f32(-729823424.)*math.sin(f32(math.pi/2))
    assert abs(legacy_p)>60 and max(map(abs,values[1:]))<1e-6


@pytest.mark.parametrize('stamp',[0.,.000833,.001666,8.000132,18.66753,4294.967295,4294.967296,4300.])
def test_clock_tag_is_frame_matched_without_rebase(decoder,stamp):
    tag=int(decode(decoder,f'{stamp:.17g},0,0,0').split(',')[0])
    assert tag==int(math.fmod(stamp*1e6,2**32))


def test_overlay_is_idempotent_and_refuses_unknown_code(tmp_path):
    helper=module(NATIVE/'scripts/apply_r6_04_body_rates.py','r604_overlay')
    ap=tmp_path/'ap';pins=json.loads((RESOURCE/'OVERLAY_MANIFEST.json').read_text())['files']
    for rel,pin in pins.items():
        if pin['before_sha256']:
            p=ap/rel;p.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(RESOURCE/'before'/p.name,p)
    helper.apply(ap);snapshot={p:p.stat().st_mtime_ns for p in ap.rglob('*') if p.is_file()}
    helper.apply(ap);assert all(p.stat().st_mtime_ns==stamp for p,stamp in snapshot.items())
    path=ap/'libraries/SITL/SIM_JSBSim.h';path.write_text('unrecognized user edit')
    before={str(p):p.read_bytes() for p in ap.rglob('*') if p.is_file()}
    with pytest.raises(ValueError,match='refusing overwrite'):helper.apply(ap)
    assert before=={str(p):p.read_bytes() for p in ap.rglob('*') if p.is_file()}


def test_real_R303_model_preinstall_then_R604_overlay_twice(tmp_path):
    body=module(NATIVE/'scripts/apply_r6_04_body_rates.py','r604_chain')
    geometry=module(NATIVE/'scripts/apply_r3_03_model_alignment.py','r303_chain')
    fs=tmp_path/'FlightStack';model=fs/'models/phoenix_v3';shutil.copytree(NATIVE/'FlightStack/models/phoenix_v3',model)
    ap=fs/'ardupilot';(ap/'libraries/SITL').mkdir(parents=True)
    pins=json.loads((RESOURCE/'OVERLAY_MANIFEST.json').read_text())['files']
    for rel in pins:shutil.copy2(RESOURCE/'after'/Path(rel).name,ap/rel)
    (ap/'waf').symlink_to(NATIVE/'FlightStack/ardupilot/waf')
    for _ in range(2):
        body.apply(ap,restore_r303_base=True);geometry.apply(fs)
        subprocess.run(['bash',str(model/'scripts/preinstall.sh'),'--ardupilot-root',str(ap),'--no-clean'],check=True,capture_output=True)
        body.apply(ap)
        for rel,pin in pins.items():assert hashlib.sha256((ap/rel).read_bytes()).hexdigest()==pin['after_sha256']
        assert hashlib.sha256((ap/'Tools/autotest/aircraft/phoenix_v3/phoenix_v3.xml').read_bytes()).hexdigest()=='9118bfac6cb1bbaa05142f1c0f419fe570414be8b1dbdeffb591e7b898f64da9'


def test_default_FlightGear_wire_and_Reset_are_preserved():
    old=(RESOURCE/'before/SIM_JSBSim.cpp').read_text();new=(RESOURCE/'after/SIM_JSBSim.cpp').read_text()
    start='    char *jsbsim_reset;';end='    created_templates = true;'
    assert old[old.index(start):old.index(end)]==new[new.index(start):new.index(end)]
    old_h=(RESOURCE/'before/SIM_JSBSim.h').read_text();new_h=(RESOURCE/'after/SIM_JSBSim.h').read_text()
    assert old_h[old_h.index('class FGNetFDM'):]==new_h[new_h.index('class FGNetFDM'):]
    assert 'Missing current JSBSim body-rate frame' in new and 'Unknown JSBSim native body-rate header' in new
