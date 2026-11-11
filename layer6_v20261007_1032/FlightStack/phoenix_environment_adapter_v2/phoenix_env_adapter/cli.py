from __future__ import annotations
import argparse,json
from .prepare import prepare
from .runtime import run_runtime


def main():
    p=argparse.ArgumentParser(prog='phoenix-env');sp=p.add_subparsers(dest='cmd',required=True)
    q=sp.add_parser('prepare',help='Prepare a TDOC binding or an explicitly selected standalone dataset')
    for flag in ('dataset-tar','region','vehicle','time','output','cache','binding'):q.add_argument('--'+flag)
    r=sp.add_parser('runtime',help='Consume TDOC-bound forcing; standalone dataset mode requires explicit inputs')
    for flag in ('dataset-tar','region','time','cache','binding','state-file'):r.add_argument('--'+flag)
    r.add_argument('--mavlink',default='tcp:127.0.0.1:5760');r.add_argument('--update-hz',type=float,default=1.)
    r.add_argument('--dry-run',action='store_true');r.add_argument('--plant-time-s',type=float)
    r.add_argument('--max-updates',type=int)
    r.add_argument('--no-wind-verify',action='store_true');r.add_argument('--param-retries',type=int,default=3)
    r.add_argument('--param-timeout',type=float,default=1.)
    a=p.parse_args()
    if a.binding:
        if any(getattr(a,k,None) is not None for k in ('dataset_tar','region','vehicle','time','cache')):
            p.error('--binding forbids independent dataset, region, vehicle, time or cache overrides')
        from .bound import prepare_bound,run_bound_runtime
        if a.cmd=='prepare':
            if not a.output:p.error('bound prepare requires --output')
            print(json.dumps(prepare_bound(a.binding,a.output),ensure_ascii=False,indent=2))
        else:
            if not a.state_file:p.error('bound runtime requires --state-file')
            if a.no_wind_verify:p.error('bound runtime cannot disable verification')
            run_bound_runtime(a.binding,a.state_file,mavlink=a.mavlink,update_hz=a.update_hz,
                dry_run=a.dry_run,plant_time_s=a.plant_time_s,max_updates=a.max_updates,
                param_retries=a.param_retries,param_timeout=a.param_timeout)
    else:
        required=('dataset_tar','region','vehicle','time','output') if a.cmd=='prepare' else ('dataset_tar','region','time','cache')
        if any(not getattr(a,k,None) for k in required):p.error('binding required, or supply all standalone inputs explicitly')
        if a.cmd=='prepare':print(json.dumps(prepare(a.dataset_tar,a.region,a.vehicle,a.time,a.output,a.cache),indent=2))
        else:run_runtime(a.dataset_tar,a.region,a.time,a.mavlink,a.cache,a.update_hz,a.dry_run,not a.no_wind_verify,a.param_retries,a.param_timeout)


if __name__=='__main__':main()
