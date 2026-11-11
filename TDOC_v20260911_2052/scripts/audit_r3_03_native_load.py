#!/usr/bin/env python3
"""Trace a bounded disarmed native startup, without GUI or command submission."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def descendants(pid):
    try:
        children=[int(p) for p in Path(f'/proc/{pid}/task/{pid}/children').read_text().split()]
    except (FileNotFoundError,ProcessLookupError):
        return []
    return [p for child in children for p in [child,*descendants(child)]]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    tdoc=Path(__file__).resolve().parents[1];bundle=tdoc.parent
    fs=bundle/'layer6_v20261007_1032/FlightStack';ap=fs/'ardupilot'
    out=args.output_dir.resolve();out.relative_to(tdoc/'reports/online');out.mkdir(exist_ok=False)
    model=ap/'Tools/autotest/aircraft/phoenix_v3/phoenix_v3.xml'
    reset=model.with_name('reset.xml');binary=ap/'build/sitl/bin/arduplane'
    js=fs/'jsbsim/build/src/JSBSim'
    template=tdoc/'src/tddt_tdoc/resources/phoenix_fourfoot/r3_02_v1/reset.xml'
    expected=template.read_bytes().replace(b'53.000000',b'0.000000')
    (out/'EXPECTED_RUNTIME_reset.xml').write_bytes(expected)
    assert sha(model)=='9118bfac6cb1bbaa05142f1c0f419fe570414be8b1dbdeffb591e7b898f64da9'
    trace=out/'native_open.trace';log=out/'NATIVE_STDOUT.log'
    argv=[str(binary),'--model','jsbsim:phoenix_v3','--home','40.010000,-105.300000,1798.372802734375,0',
          '--speedup','1','--base-port','23000','-I','43',
          '--autotest-dir',str(ap/'Tools/autotest'),'--defaults',str(ap/'Tools/autotest/default_params/phoenix_v3.parm')]
    env=dict(os.environ,PATH=str(js.parent)+os.pathsep+os.environ['PATH'],PYTHONDONTWRITEBYTECODE='1',TMPDIR='/tmp')
    from pymavlink import mavutil
    connection=None;observed=set();executables={};heartbeat=None
    with log.open('w') as stream:
        process=subprocess.Popen(['strace','-f','-yy','-s','512','-e','trace=execve,openat,chdir',
                                  '-o',str(trace),*argv],cwd=out,env=env,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
        try:
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                if connection is None:
                    try:
                        connection=mavutil.mavlink_connection('tcp:127.0.0.1:23000',autoreconnect=False,retries=0)
                    except (OSError,ConnectionError):pass
                observed.update(descendants(process.pid))
                for pid in observed:
                    try:
                        exe=Path(f'/proc/{pid}/exe').resolve(strict=True)
                        if exe in (binary,js):executables[str(pid)]=str(exe)
                    except (FileNotFoundError,ProcessLookupError,PermissionError):pass
                text=trace.read_text(errors='replace') if trace.exists() else ''
                if 'Opened JSBSim control socket' in log.read_text(errors='replace') and any('reset.xml' in line and 'O_RDONLY' in line for line in text.splitlines()):
                    if connection is not None:heartbeat=connection.wait_heartbeat(timeout=5)
                    break
                if process.poll() is not None:raise RuntimeError('native startup exited; inspect NATIVE_STDOUT.log')
                time.sleep(.1)
            assert heartbeat is not None,'native heartbeat unavailable'
            assert not heartbeat.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED,'probe must stay disarmed'
            assert reset.read_bytes()==expected,'runtime Reset differs from the independent mission-specific reference'
            (out/'LOADED_reset.xml').write_bytes(reset.read_bytes())
            (out/'LOADED_phoenix_v3.xml').write_bytes(model.read_bytes())
            lines=trace.read_text(errors='replace').splitlines()
            reads=[line for line in lines if 'O_RDONLY' in line and ('aircraft/phoenix_v3/phoenix_v3.xml' in line or 'aircraft/phoenix_v3/reset.xml' in line)]
            assert any('phoenix_v3.xml' in line for line in reads) and any('reset.xml' in line for line in reads)
            assert str(js) in executables.values() and str(binary) in executables.values(),'live executable identity unavailable'
            result={'step':'R3.03','status':'PASS','argv':argv,'executables_observed_live':executables,
                    'arduplane_sha256':sha(binary),'JSBSim_sha256':sha(js),
                    'loaded_model_path':str(model),'loaded_model_sha256':sha(model),
                    'loaded_reset_path':str(reset),'loaded_reset_sha256':sha(reset),
                    'expected_runtime_reset_sha256':hashlib.sha256(expected).hexdigest(),
                    'runtime_reset_exact_reference_match':True,'successful_native_reads':reads,
                    'heartbeat_base_mode':heartbeat.base_mode,'armed':False,'ARM_sent':False,
                    'command_submission':False,'GUI_executed':False,'ground_load_bearing_tested':False,
                    'knowledge_used':False,'forcing_binding_verified':False,'flight_ready':False}
            (out/'NATIVE_LOAD_RESULT.json').write_text(json.dumps(result,indent=2)+'\n')
        finally:
            if connection:connection.close()
            observed.update(descendants(process.pid))
            for pid in sorted(observed,reverse=True):
                try:os.kill(pid,signal.SIGTERM)
                except ProcessLookupError:pass
            if process.poll() is None:process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
    print(json.dumps({'step':'R3.03','status':'PASS','disarmed_native_load':True}))


if __name__=='__main__':main()
