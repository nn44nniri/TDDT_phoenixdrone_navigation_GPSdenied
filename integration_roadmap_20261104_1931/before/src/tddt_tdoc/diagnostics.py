"""Opt-in Online diagnostics without changing orchestration or control authority."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback as tracebacks


class _Tee:
    def __init__(self, stream, log):
        self.stream, self.log = stream, log
    def write(self, text):
        count=self.stream.write(text)
        self.log.write(text);self.log.flush()
        return count
    def flush(self):
        self.stream.flush();self.log.flush()
    def __getattr__(self,name):
        return getattr(self.stream,name)


@contextmanager
def diagnostic_session(config,args):
    option=getattr(args,'debug_log',None)
    if not option:
        yield
        return
    path=(config.runtime_root/'logs'/('nav-online_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.log')
          if option=='auto' else Path(option).expanduser().resolve())
    path.parent.mkdir(parents=True,exist_ok=True)
    previous=os.environ.get('TDDT_DIAGNOSTIC_LOG')
    stdout,stderr=sys.stdout,sys.stderr
    with path.open('a',encoding='utf-8',buffering=1) as log:
        sys.stdout,sys.stderr=_Tee(stdout,log),_Tee(stderr,log)
        os.environ['TDDT_DIAGNOSTIC_LOG']=str(path)
        try:
            print(f'[diagnostics] log={path}',flush=True)
            event('online.cli.begin',command=args.cmd,config=str(config.path) if hasattr(config,'path') else str(config.package_root))
            yield
        except BaseException:
            event('online.cli.exception',traceback=True)
            raise
        finally:
            event('online.cli.end')
            sys.stdout,sys.stderr=stdout,stderr
            if previous is None:os.environ.pop('TDDT_DIAGNOSTIC_LOG',None)
            else:os.environ['TDDT_DIAGNOSTIC_LOG']=previous


def event(name,*,traceback=False,**details):
    if not os.environ.get('TDDT_DIAGNOSTIC_LOG'):return
    data={'time_utc':datetime.now(timezone.utc).isoformat(),'event':name,**details}
    if traceback:data['traceback']=tracebacks.format_exc()
    print('[diagnostics] '+json.dumps(data,ensure_ascii=False,default=str),file=sys.stderr,flush=True)


def online_preflight_diagnostics(preflight):
    """Preserve Layer6 failure detail in workflow JSON, console and debug logs."""
    adapters = preflight.get('adapters') or {}
    layer6 = dict(adapters.get('6') or adapters.get(6) or {})
    probe = dict(layer6.get('layer6_client') or {})
    binding = dict(probe.get('boulder_resource_binding') or {})
    return {
        'schema': 'TDOC-online-preflight-diagnostics-v1',
        'layer6_status': layer6.get('status'),
        'layer6_reason': layer6.get('reason'),
        'layer6_client_ok': probe.get('ok'),
        'layer6_missing': probe.get('missing'),
        'layer6_missing_api': list(probe.get('missing_api') or []),
        'layer6_signature_mismatches': list(probe.get('signature_mismatches') or []),
        'layer6_api_inspection_errors': list(probe.get('api_inspection_errors') or []),
        'layer6_expected_version': probe.get('expected_version'),
        'layer6_actual_version': probe.get('actual_version'),
        'layer6_expected_package': probe.get('expected_package'),
        'layer6_actual_package': probe.get('actual_package'),
        'layer6_failure_reasons': list(probe.get('failure_reasons') or []),
        'layer6_failures': list(probe.get('failures') or []),
        'layer6_error': probe.get('error'),
        'layer6_error_stage': probe.get('error_stage'),
        'layer6_client': probe,
        'boulder_resource_binding_ok': binding.get('ok'),
        'boulder_resource_mismatches': [p for p in binding.get('pairs') or [] if not p.get('match')],
        'flight_ready': False,
        'actuation_executed': False,
        'readiness_scope': layer6.get('readiness_scope'),
    }


def run_workflow(command,cwd,*,enabled=False):
    if not enabled:return int(subprocess.run(command,cwd=str(cwd)).returncode)
    env=dict(os.environ,PYTHONUNBUFFERED='1')
    event('online.workflow.start',argv=command,cwd=str(cwd))
    with subprocess.Popen(command,cwd=str(cwd),env=env,stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',bufsize=1) as child:
        for line in child.stdout:
            print(line,end='',flush=True)
        result=int(child.wait())
    event('online.workflow.exit',returncode=result)
    return result
