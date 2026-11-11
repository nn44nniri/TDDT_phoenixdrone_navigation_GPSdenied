"""Owned Native FlightStack and live backend observer. No global process-name cleanup."""
from __future__ import annotations
import argparse,contextlib,fcntl,hashlib,json,os,signal,shutil,socket,subprocess,sys,threading,time,uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
RUNTIME=ROOT/'runtime'
SCHEMA='Layer6-native-session-v1'
ROLES=('arduplane','JSBSim','mavproxy','environment','intake','gui')

def write(path,obj):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex)
 tmp.write_text(json.dumps(obj,indent=2,allow_nan=False)+'\n');tmp.replace(path)
def read(path):return json.loads(Path(path).read_text())
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def proc(pid):
 try:
  p=Path('/proc')/str(pid);fields=(p/'stat').read_text().rsplit(')',1)[1].split()
  if fields[0]=='Z':return None
  env=dict(x.split(b'=',1) for x in (p/'environ').read_bytes().split(b'\0') if b'=' in x)
  return {'pid':int(pid),'start_ticks':int(fields[19]),'uid':p.stat().st_uid,
   'session_id':env.get(b'TDDT_NATIVE_SESSION_ID',b'').decode(),'role':env.get(b'TDDT_NATIVE_ROLE',b'').decode(),
   'executable':str((p/'exe').resolve(strict=True)),'cmdline':(p/'cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace')}
 except (OSError,ValueError,ProcessLookupError):return None

def processes(session_id):
 if not isinstance(session_id,str) or len(session_id)!=32:return []
 values=[]
 for path in Path('/proc').iterdir():
  if not path.name.isdigit():continue
  try:
   if path.stat().st_uid!=os.getuid():continue
  except OSError:continue
  value=proc(int(path.name))
  if value and value['session_id']==session_id and value['uid']==os.getuid():
   if Path(value['executable']).name=='JSBSim':value['role']='JSBSim'
   values.append(value)
 return values

def check_session(folder):
 folder=Path(folder).resolve();folder.relative_to((RUNTIME/'sessions').resolve())
 data=read(folder/'session.json')
 if data['schema']!=SCHEMA or data['uid']!=os.getuid() or folder.name!=data['session_id']:raise ValueError('invalid native ownership record')
 return folder,data

def panel_status(folder,session_id,*,now=None,max_age_s=2.):
 """Read the live MAVProxy observer, without turning a process PID into GUI evidence."""
 from .native_panels import validate_panel_record
 try:
  record=read(Path(folder)/'native_panels.json')
  owner=read(Path(folder)/'mavproxy_process.json')
  return validate_panel_record(record,session_id,producer_pid=owner['pid'],now=now,max_age_s=max_age_s)
 except (OSError,ValueError,KeyError,TypeError) as exc:
  return {'ready':False,'reason':'native_panel_evidence_unavailable','error':str(exc),
          'visual_acceptance_verified':False}

def jsbsim_window_status(folder,session_id,items,*,now=None,max_age_s=2.):
 """Verify the separate observer watches the existing owned JSBSim, never a second simulator."""
 try:
  record=read(Path(folder)/'jsbsim_window.json')
  now=time.monotonic() if now is None else now
  viewer=next(p for p in items if p['pid']==record['producer_pid'] and p['role']=='terminal4')
  backend=next(p for p in items if p['pid']==record['jsbsim_pid'] and p['role']=='JSBSim')
  if (record['session_id']!=session_id or record['schema']!='Layer6-JSBSim-window-v1'
      or record['source_log']!=str(Path(folder)/'arduplane.log')
      or record['jsbsim_start_ticks']!=backend['start_ticks']
      or record['producer_start_ticks']!=viewer['start_ticks']
      or record['telemetry_master']!='tcp:127.0.0.1:5760' or record['telemetry_ready'] is not True
      or not isinstance(record['recorded_monotonic_s'],(int,float))
      or not 0<=now-record['recorded_monotonic_s']<=max_age_s):
   raise ValueError('JSBSim observer stale or session/process mismatch')
  return {'ready':True,'reason':'live_owned_jsbsim_observer','evidence':record,'visual_acceptance_verified':False}
 except (OSError,ValueError,KeyError,TypeError,StopIteration) as exc:
  return {'ready':False,'reason':'jsbsim_window_unavailable','error':str(exc),'visual_acceptance_verified':False}

def status(folder,*,now=None,max_age_s=2.):
 folder,data=check_session(folder);items=processes(data['session_id']);roles={r:any(p['role']==r for p in items) for r in ROLES}
 panels=panel_status(folder,data['session_id'],now=now,max_age_s=max_age_s);process_ready=all(roles.values())
 backend_window=jsbsim_window_status(folder,data['session_id'],items,now=now,max_age_s=max_age_s)
 return {'schema':SCHEMA,'session_id':data['session_id'],'session_dir':str(folder),'created':False,
  'processes':items,'roles':roles,'process_ready':process_ready,'alive':bool(items),'flight_ready':False,
  'panels':panels,'panels_ready':panels['ready'],'jsbsim_window':backend_window,
  'session_ready':process_ready and panels['ready'] and backend_window['ready'],
  'visual_acceptance_verified':False,
  'stop_requested':(folder/'stop.request').exists(),'binding_file':str(folder/'binding.json'),
  'environment_state_file':str(folder/'environment_state.json')}

def session_health(session_id,*,folder=None,now=None,max_age_s=2.):
 """Fresh, same-session component and panel check suitable for every Online cycle."""
 checked=time.monotonic() if now is None else now
 try:
  if not isinstance(session_id,str) or len(session_id)!=32 or any(c not in '0123456789abcdef' for c in session_id):
   raise ValueError('invalid expected native session ID')
  if folder is None:
   registry=read(RUNTIME/'flightstack_state.json')
   if registry['session_id']!=session_id:raise ValueError('native session registry changed')
   folder=registry['session_dir']
  _,identity=check_session(folder)
  if identity['session_id']!=session_id:raise ValueError('native session identity mismatch')
  # A producer can publish while the check reads /proc. Evaluate freshness after
  # each atomic read rather than making a new publication appear to be future data.
  result=status(folder,now=now,max_age_s=max_age_s)
  ok=result['session_ready'] and not result['stop_requested']
  reason=('live_native_components_and_panels' if ok else 'native_stop_requested' if result['stop_requested']
          else 'native_component_missing' if not result['process_ready']
          else result['panels']['reason'] if not result['panels_ready'] else result['jsbsim_window']['reason'])
  return {**result,'ok':ok,'reason':reason,'checked_monotonic_s':checked}
 except (OSError,ValueError,KeyError,TypeError) as exc:
  return {'ok':False,'session_id':session_id,'session_ready':False,'reason':'native_session_health_unavailable',
          'error':str(exc),'checked_monotonic_s':checked,'flight_ready':False,'visual_acceptance_verified':False}

@contextlib.contextmanager
def lock():
 RUNTIME.mkdir(parents=True,exist_ok=True)
 with (RUNTIME/'lifecycle.lock').open('a') as f:
  fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
  try:yield
  finally:fcntl.flock(f,fcntl.LOCK_UN)

def stop(folder):
 folder,data=check_session(folder);(folder/'stop.request').write_text(str(time.time())+'\n')
 # Only exact same-UID, unique session-tagged processes. Recheck PID birth before every signal.
 for sig,seconds in ((signal.SIGTERM,3.),(signal.SIGKILL,1.)):
  for row in sorted(processes(data['session_id']),key=lambda p:p['role'].startswith('terminal')):
   current=proc(row['pid'])
   if current and all(current[k]==row[k] for k in ('pid','start_ticks','session_id','uid')):
    try:os.kill(row['pid'],sig)
    except ProcessLookupError:pass
  end=time.monotonic()+seconds
  while time.monotonic()<end:
   if not processes(data['session_id']):break
   time.sleep(.05)
 remaining=processes(data['session_id']);result={'schema':SCHEMA,'session_id':data['session_id'],'stopped':not remaining,'remaining':remaining,'scope':'same UID and exact native session tag only','flight_ready':False}
 write(folder/'stop_result.json',result)
 if remaining:raise RuntimeError('owned native processes did not stop')
 return result

def create(binding_file):
 from .binding import load_context
 binding,cfg=load_context(binding_file);session=uuid.uuid4().hex;folder=RUNTIME/'sessions'/session;folder.mkdir(parents=True)
 shutil.copy2(binding_file,folder/'binding.json')
 fs=cfg.path_value('TDDT_LAYER6_ROOT',required=True)/'FlightStack';ap=fs/'ardupilot'
 private=folder/'autotest';private.mkdir();shutil.copytree(ap/'Tools/autotest/aircraft/phoenix_v3',private/'aircraft/phoenix_v3')
 data={'schema':SCHEMA,'session_id':session,'uid':os.getuid(),'created_monotonic':time.monotonic(),'binding_hash':binding['binding_hash'],
  'model_id':binding['model_id'],'flightstack_dir':str(fs),'binding_source':str(Path(binding_file).resolve()),
  'aircraft_sha256':sha(private/'aircraft/phoenix_v3/phoenix_v3.xml'),'source_aircraft_sha256':sha(ap/'Tools/autotest/aircraft/phoenix_v3/phoenix_v3.xml'),
  'reset_template_sha256':sha(private/'aircraft/phoenix_v3/reset.xml')}
 write(folder/'session.json',data);return folder,data

def active_binding(config):
 """Reuse the exact live forcing/knowledge binding, never replace it on reuse."""
 registry=RUNTIME/'flightstack_state.json'
 if not registry.exists():return None
 data=read(registry)
 if not data.get('session_dir'):return None
 current=status(data['session_dir'])
 if not current['alive']:return None
 if not current['session_ready']:raise RuntimeError('existing native session or required panels are degraded')
 from .binding import load_context
 path=Path(current['binding_file']);binding,cfg=load_context(path)
 if cfg.package_root.resolve()!=config.package_root.resolve():raise ValueError('native session belongs to another TDOC root')
 return path,binding

def _stream(pipe,path):
 with Path(path).open('a',buffering=1) as log:
  for line in iter(pipe.readline,''):
   log.write(line)
   try:
    shown=line
    if Path(path).name=='environment.log' and line.startswith('{'):
     row=json.loads(line);shown=json.dumps({k:row.get(k) for k in ('ok','reason','plant_time_s','readback_verified')})+'\n'
    print(shown,end='',flush=True)
   except (BrokenPipeError,OSError):pass

def mavproxy_command(venv):
 """All required panels observe the existing sole FCU master and output fanout."""
 return [Path(venv)/'mavproxy.py','--master=tcp:127.0.0.1:5760',
  '--out=udp:127.0.0.1:14551','--out=tcpin:127.0.0.1:14552','--streamrate=20',
  '--map','--console','--load-module=layer6.native_panels','--non-interactive']

def jsbsim_observer(folder,data):
 """Display the owned backend's shared live log and the same FCU stream in its own window."""
 source=folder/'arduplane.log';offset=0;last_report=0.;self_record=proc(os.getpid())
 print('JSBSim Native backend observer | session '+data['session_id'],flush=True)
 print('Observing existing JSBSim child; shared backend log: '+str(source),flush=True)
 while not (folder/'stop.request').exists():
  try:
   with source.open() as stream:
    stream.seek(offset);chunk=stream.read();offset=stream.tell()
   if chunk:print(chunk,end='',flush=True)
  except FileNotFoundError:pass
  now=time.monotonic()
  if now-last_report>=.25:
   last_report=now
   backends=[p for p in processes(data['session_id']) if p['role']=='JSBSim']
   if len(backends)==1:
    backend=backends[0];panels=panel_status(folder,data['session_id'])
    evidence=panels.get('evidence') or {};heart=evidence.get('heartbeat') or {};position=evidence.get('position') or {}
    record={'schema':'Layer6-JSBSim-window-v1','session_id':data['session_id'],'producer_pid':os.getpid(),
            'producer_start_ticks':self_record['start_ticks'],'recorded_monotonic_s':now,
            'jsbsim_pid':backend['pid'],'jsbsim_start_ticks':backend['start_ticks'],'source_log':str(source),
            'telemetry_master':'tcp:127.0.0.1:5760','plant_time_s':position.get('time_boot_ms',0)*.001,
            'mode':heart.get('mode'),'armed':heart.get('armed'),'telemetry_ready':panels['ready'],
            'simulator_spawned':False,'visual_acceptance_verified':False}
    write(folder/'jsbsim_window.json',record)
    print('JSBSim pid=%s | FCU %.3f s | %s | %s | same-session telemetry=%s'%(
          backend['pid'],record['plant_time_s'],record['mode'],'ARMED' if record['armed'] else 'DISARMED',panels['ready']),flush=True)
  time.sleep(.05)

def worker(folder,terminal):
 folder,data=check_session(folder)
 if terminal=='terminal4':
  write(folder/(terminal+'.json'),{'pid':os.getpid(),'terminal':terminal,'session_id':data['session_id']})
  return jsbsim_observer(folder,data)
 binding=read(folder/'binding.json');fs=Path(data['flightstack_dir']);ap=fs/'ardupilot';venv=ap/'venv-ardupilot-debian13/bin'
 python=ROOT.parent/'.venv/bin/python';native_python=venv/'python';tdoc=Path(binding['tdoc_config']).parent.parent if 'tdoc_config' in binding else ROOT.parent/'TDOC_v20260911_2052'
 # Binding validation supplies authoritative TDOC package root; no independent dataset.
 from .binding import load_context
 _,cfg=load_context(folder/'binding.json');tdoc=cfg.package_root
 env=dict(os.environ,TDDT_NATIVE_SESSION_ID=data['session_id'],TDDT_NATIVE_BINDING_FILE=str(folder/'binding.json'),TDDT_NATIVE_ENVIRONMENT_STATE=str(folder/'environment_state.json'),
  TDDT_NATIVE_PANELS_STATE=str(folder/'native_panels.json'),
  PYTHONPATH=os.pathsep.join([str(ROOT/'src'),str(tdoc/'src'),str(fs/'phoenix_environment_adapter_v2'),os.environ.get('PYTHONPATH','')]),
  PATH=str(fs/'jsbsim/build/src')+os.pathsep+os.environ['PATH'],PYTHONDONTWRITEBYTECODE='1',TMPDIR='/tmp')
 os.environ['TDDT_NATIVE_SESSION_ID']=data['session_id'] # exec worker already inherits this tag; children get explicit role.
 write(folder/(terminal+'.json'),{'pid':os.getpid(),'terminal':terminal,'session_id':data['session_id']})
 children=[];stop_requested=[False]
 signal.signal(signal.SIGTERM,lambda *_:stop_requested.__setitem__(0,True));signal.signal(signal.SIGINT,lambda *_:stop_requested.__setitem__(0,True))
 def launch(role,argv,cwd=folder):
  e=dict(env,TDDT_NATIVE_ROLE=role,PYTHONNOUSERSITE='1')
  if role=='gui':
   # Same CPython ABI: Qt/pygame live in the native venv; TDOC GIS/science
   # dependencies live in the integrated venv installed by SETUP. Keep both
   # explicit and bundle-owned instead of failing binding verification in Qt.
   science=ROOT.parent/'.venv/lib'/('python%d.%d'%sys.version_info[:2])/'site-packages'
   if not science.is_dir():raise RuntimeError('integrated scientific Python environment missing')
   e['PYTHONPATH']=e['PYTHONPATH']+os.pathsep+str(science)
  p=subprocess.Popen([str(a) for a in argv],cwd=cwd,env=e,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1,start_new_session=True)
  children.append((role,p));write(folder/(role+'_process.json'),{'pid':p.pid,'role':role,'argv':list(map(str,argv)),'started_monotonic':time.monotonic()})
  threading.Thread(target=_stream,args=(p.stdout,folder/(role+'.log')),daemon=True).start();print(role+' started pid='+str(p.pid),flush=True);return p
 try:
  if terminal=='terminal1':
   origin=binding['mission_origin'];home=','.join(str(origin[k]) for k in ('latitude_deg','longitude_deg','terrain_elevation_msl_m','heading_deg'))
   launch('arduplane',[ap/'build/sitl/bin/arduplane','--model','jsbsim:phoenix_v3','--home',home,'--speedup','1','--base-port','5760','-I','0','--autotest-dir',folder/'autotest','--defaults',ap/'Tools/autotest/default_params/phoenix_v3.parm'])
   # Real MAVProxy fans out the sole native stream; no mock/router replaces FCU or GUI.
   launch('mavproxy',mavproxy_command(venv))
  elif terminal=='terminal2':
   p=launch('environment_prepare',[python,'-B','-m','phoenix_env_adapter.cli','prepare','--binding',folder/'binding.json','--output',folder/'environment'])
   if p.wait(timeout=25)!=0:raise RuntimeError('bound environment prepare failed')
   children.remove(('environment_prepare',p))
   launch('environment',[python,'-B','-m','phoenix_env_adapter.cli','runtime','--binding',folder/'binding.json','--state-file',folder/'environment_state.json','--mavlink','udpin:127.0.0.1:14551','--update-hz','1'])
  elif terminal=='terminal3':
   launch('intake',[native_python,'-B','-m','layer6.service','--listen','127.0.0.1:14661','--gui','127.0.0.1:14660','--rate-hz','30'])
   launch('gui',[native_python,'-B',ROOT/'src/gui/phoenix_f310_gui.py','--connect','tcp:127.0.0.1:14552','--no-gamepad'])
  else:raise ValueError('unknown native terminal role')
  while not stop_requested[0] and not (folder/'stop.request').exists():
   failed=[(role,p.returncode) for role,p in children if p.poll() is not None]
   if failed:raise RuntimeError('native component exited: '+str(failed))
   time.sleep(.1)
 except Exception as exc:
  write(folder/(terminal+'_error.json'),{'error':repr(exc),'monotonic':time.monotonic()});raise
 finally:
  for role,p in children:
   if p.poll() is None:
    try:os.killpg(p.pid,signal.SIGTERM)
    except ProcessLookupError:pass
  for role,p in children:
   try:p.wait(timeout=3)
   except subprocess.TimeoutExpired:
    try:os.killpg(p.pid,signal.SIGKILL)
    except ProcessLookupError:pass
    p.wait(timeout=2)

def main():
 p=argparse.ArgumentParser();p.add_argument('--session',required=True);p.add_argument('--terminal',choices=['terminal1','terminal2','terminal3','terminal4'],required=True);a=p.parse_args();worker(a.session,a.terminal)
if __name__=='__main__':main()
