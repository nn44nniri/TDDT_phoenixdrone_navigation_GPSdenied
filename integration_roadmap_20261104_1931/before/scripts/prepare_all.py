#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, os, selectors, shutil, subprocess, sys, time, zipfile
from pathlib import Path

HERE=Path(__file__).resolve().parents[1]
REPORT=HERE/'reports'/'preparation'
sys.path.insert(0,str(HERE/'src'))
from tddt_tdoc.climate_forcing import forcing_filename, write_central_active_manifest, write_active_manifest

REQUIRED_ROOTS={
    'TDDT_TWINS_ROOT':['scripts/build_all.sh','python/build_tddt_layer2_tep.py','python/twins_fmi','config/phoenix_drone_v3.yaml'],
    'TDDT_CLIMATE_ROOT':['scripts/build_fmu.sh','scripts/download_openmeteo.py','python/estimate_climate.py'],
    'TDDT_UNIT_ADAPTER_ROOT':['HOW_USAGE.md','src/tdoc_unit_converter/__init__.py'],
    'TDDT_BOULDER_ROOT':['build_regional_map.py','build_crp_dataset.py','map_mosaic_query.py','HOW_USAGE.md'],
    'TDDT_SARG_PACKAGE_ROOT':['sarg_builder/cli.py','sarg_builder/report_cli.py','HOW_USAGE.md'],
    'TDDT_LAYER3_ROOT':['src/layer3','configs/configuration_boulder.yaml'],
    'TDDT_LAYER4_ROOT':['src/layer4','configs/configuration_boulder.yaml'],
    'TDDT_LAYER5_ROOT':['src/layer5','configs/configuration_phoenix.yaml'],
    'TDDT_LAYER6_ROOT':['src/layer6/client.py','src/layer6/validation.py','src/layer6/feedback.py','src/run_flightstack.py','scripts/run_layer6_stack.sh'],
    'TDDT_LAYER7_ROOT':['src/tddt_layer7','configs/default.json'],
}
TEP_FILES=('tep.csv','manifest.json','heldout_validation.json','quality_report.json','VERSION')


def sha(p:Path):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()


def pathenv(k):
    v=os.environ.get(k,'').strip(); return Path(v).expanduser().resolve() if v else None


def emit(report,stage,ok,**kw):
    idx=len(report['stages'])+1; total=report['_total']
    rec={'index':idx,'stage':stage,'ok':bool(ok),**kw}; report['stages'].append(rec)
    suffix='PASS' if ok else 'FAIL'
    if kw.get('skipped'): suffix='SKIP' if ok else 'FAIL'
    print(f"[{idx:02d}/{total:02d}] {stage} ... {suffix}",flush=True)
    if not ok: report['ok']=False
    return rec


def _run_live(cmd,cwd,*,env=None,heartbeat_s:float=15.0):
    """Run a child command while streaming output and periodic heartbeats.

    Returns (returncode, output_tail, elapsed_s). This intentionally avoids a silent
    subprocess.run(capture_output=True) wait during long preparation stages.
    """
    t=time.perf_counter(); lines=[]
    p=subprocess.Popen(cmd,cwd=str(cwd),env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,bufsize=1)
    sel=selectors.DefaultSelector()
    if p.stdout is not None: sel.register(p.stdout,selectors.EVENT_READ)
    last_heartbeat=time.perf_counter()
    while True:
        events=sel.select(timeout=0.5)
        if events:
            for key,_ in events:
                line=key.fileobj.readline()
                if line:
                    line=line.rstrip('\n'); lines.append(line)
                    if len(lines)>1200: lines=lines[-1200:]
                    print(f"      | {line}",flush=True)
        now=time.perf_counter()
        if heartbeat_s>0 and now-last_heartbeat>=heartbeat_s and p.poll() is None:
            print(f"      ... still running ({now-t:.1f} s elapsed)",flush=True)
            last_heartbeat=now
        if p.poll() is not None:
            if p.stdout is not None:
                rest=p.stdout.read()
                if rest:
                    for line in rest.splitlines():
                        lines.append(line); print(f"      | {line}",flush=True)
            break
    try: sel.close()
    except Exception: pass
    return int(p.returncode or 0),'\n'.join(lines)[-16000:],round(time.perf_counter()-t,3)


def run(report,stage,cmd,cwd,*,env=None,required=True):
    idx=len(report['stages'])+1; total=report['_total']; print(f"[{idx:02d}/{total:02d}] {stage} ...",flush=True)
    if cwd is None or not Path(cwd).is_dir():
        rec={'index':idx,'stage':stage,'ok':False,'returncode':127,'elapsed_s':0.0,'cwd':str(cwd),'command':[str(x) for x in cmd],'output_tail':'working directory unavailable'}
        report['stages'].append(rec); print('      FAIL (working directory unavailable)',flush=True)
        if required: report['ok']=False
        return False,rec
    try:
        rc,out,elapsed=_run_live(cmd,cwd,env=env)
        ok=rc==0
    except Exception as e:
        ok=False; out=repr(e); rc=127; elapsed=0.0
    rec={'index':idx,'stage':stage,'ok':ok,'returncode':rc,'elapsed_s':elapsed,'cwd':str(cwd),'command':[str(x) for x in cmd],'output_tail':out}
    report['stages'].append(rec); print(f"      {'PASS' if ok else 'FAIL'} ({rec['elapsed_s']} s)",flush=True)
    if required and not ok: report['ok']=False
    return ok,rec


def build_climate(root:Path|None,report:dict):
    idx=len(report['stages'])+1; total=report['_total']; print(f"[{idx:02d}/{total:02d}] Build Climate Twin FMU ...",flush=True); t=time.perf_counter()
    if root is None or not root.is_dir():
        rec={'index':idx,'stage':'Build Climate Twin FMU','ok':False,'returncode':127,'elapsed_s':0.0,'cwd':str(root),'command':['bash','scripts/build_fmu.sh'],'output_tail':'TDDT_CLIMATE_ROOT unavailable'}
        report['stages'].append(rec); report['ok']=False; print('      FAIL (TDDT_CLIMATE_ROOT unavailable)',flush=True); return False
    try:
        rc,out,elapsed=_run_live(['bash','scripts/build_fmu.sh'],root)
    except Exception as exc:
        rc=127; out=repr(exc); elapsed=round(time.perf_counter()-t,3)
    ok=rc==0; fallback=False; docs=[]
    # Preserve the existing safe staging fallback: source package is not patched.
    if not ok and (root/'build/cmake/MicroMet.so').is_file() and (root/'modelDescription.xml').is_file():
        stage=root/'build/fmu_stage_tdoc'; shutil.rmtree(stage,ignore_errors=True); (stage/'binaries/linux64').mkdir(parents=True); (stage/'resources').mkdir(parents=True)
        shutil.copy2(root/'modelDescription.xml',stage/'modelDescription.xml'); shutil.copy2(root/'build/cmake/MicroMet.so',stage/'binaries/linux64/MicroMet.so')
        for doc in (root/'docs').glob('*'):
            if doc.is_file(): shutil.copy2(doc,stage/'resources'/doc.name); docs.append(doc.name)
        if (root/'resources/tddt_layer2').is_dir(): shutil.copytree(root/'resources/tddt_layer2',stage/'resources/tddt_layer2',dirs_exist_ok=True)
        target=root/'fmu/MicroMetTwin.fmu'; target.parent.mkdir(parents=True,exist_ok=True)
        with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
            for fp in stage.rglob('*'):
                if fp.is_file(): z.write(fp,fp.relative_to(stage).as_posix())
        try:
            with zipfile.ZipFile(target) as z: names=set(z.namelist())
            ok={'modelDescription.xml','binaries/linux64/MicroMet.so'}.issubset(names); fallback=ok
        except Exception: ok=False
        out += '\nTDOC staging fallback: '+('PASS' if ok else 'FAIL')
    rec={'index':idx,'stage':'Build Climate Twin FMU','ok':ok,'returncode':rc,'elapsed_s':elapsed,'cwd':str(root),'command':['bash','scripts/build_fmu.sh'],'package_script_ok':rc==0,'tdoc_staging_fallback':fallback,'fallback_docs':docs,'output_tail':out[-16000:]}
    report['stages'].append(rec); print(f"      {'PASS' if ok else 'FAIL'} ({rec['elapsed_s']} s){' via TDOC staging fallback' if fallback else ''}",flush=True)
    if not ok: report['ok']=False
    return ok


def materialize_climate_forcing_link(climate:Path|None, forcing:Path|None, *, hours:int) -> dict:
    """Expose the central downloaded forcing to legacy Climate consumers without duplication.

    The downloaded bytes remain authoritative under ``internet_datasets/climate_twin``.
    Climate package consumers that still use ``climate_dataset/openmeteo_<H>h.csv`` receive
    a relative symlink plus an active manifest.  No network access occurs here.
    """
    if climate is None or forcing is None or not forcing.is_file():
        return {'ok':False,'reason':'central climate forcing unavailable'}
    ds=climate/'climate_dataset'; ds.mkdir(parents=True,exist_ok=True)
    dst=ds/forcing.name
    try:
        if dst.exists() or dst.is_symlink():
            if dst.is_dir() and not dst.is_symlink():
                return {'ok':False,'reason':'runtime forcing target is a directory','target':str(dst)}
            dst.unlink()
        rel=os.path.relpath(forcing.resolve(),start=ds.resolve())
        dst.symlink_to(rel)
        if not dst.is_file() or sha(dst)!=sha(forcing):
            raise RuntimeError('runtime climate forcing link hash mismatch')
        # Optional provenance/meta sidecar follows the same single-copy policy.
        meta_src=Path(str(forcing)+'.meta.json'); meta_dst=Path(str(dst)+'.meta.json')
        if meta_dst.exists() or meta_dst.is_symlink(): meta_dst.unlink()
        if meta_src.is_file(): meta_dst.symlink_to(os.path.relpath(meta_src.resolve(),start=ds.resolve()))
        manifest=write_active_manifest(climate,dst,hours=hours,source='TDOC central internet_datasets runtime materialization')
        return {'ok':True,'central':str(forcing.resolve()),'runtime_link':str(dst.absolute()),'sha256':sha(forcing),'active_manifest':str(manifest),'single_copy':True}
    except Exception as exc:
        return {'ok':False,'reason':repr(exc),'central':str(forcing),'runtime_link':str(dst)}


def source_package_ready(report,stage,root:Path|None,required_paths:list[str],*,semantics:str):
    missing=list(required_paths) if root is None else [x for x in required_paths if not (root/x).exists()]
    ok=bool(root and root.is_dir() and not missing)
    return emit(report,stage,ok,root=str(root) if root else None,missing=missing,preparation_action='source/API readiness; no dedicated compile step',semantics=semantics)


def prepare_tdoc_entrypoint(report:dict, *, no_install:bool=False):
    """Install TDOC when possible, otherwise validate the supported source launcher.

    Offline/reuse preparation must not require downloading a packaging backend when
    the authoritative source tree is already selected by ``environment.sh``.  The
    fallback is accepted only when the public module resolves to this exact release
    and the checked-in launcher successfully invokes the same CLI entry point.
    """
    stage='Install TDOC package in active Python environment'
    if no_install:
        return emit(report,stage,True,skipped=True,reason='--no-install')
    import importlib.util
    try:
        backend_available=importlib.util.find_spec('setuptools.build_meta') is not None
    except (ImportError,ModuleNotFoundError):
        backend_available=False
    if backend_available:
        return run(report,stage,[sys.executable,'-m','pip','install','-e',str(HERE),
                                 '--no-deps','--no-build-isolation'],HERE)

    launcher=HERE/'scripts/tddt-tdoc'
    spec=importlib.util.find_spec('tddt_tdoc')
    origin=Path(spec.origin).resolve() if spec and spec.origin else None
    expected=(HERE/'src/tddt_tdoc/__init__.py').resolve()
    cmd=[str(launcher),'--help']
    rc=127; out='source launcher unavailable'; elapsed=0.0
    if launcher.is_file() and os.access(launcher,os.X_OK) and origin==expected:
        try:
            rc,out,elapsed=_run_live(cmd,HERE)
        except Exception as exc:
            out=repr(exc)
    ok=bool(rc==0 and origin==expected)
    rec=emit(report,stage,ok,
             installation_mode='AUTHORITATIVE_SOURCE_TREE_ENTRYPOINT',
             editable_install=False,build_backend_available=False,
             source_module_origin=str(origin) if origin else None,
             expected_source_module=str(expected),launcher=str(launcher),
             command=cmd,returncode=rc,elapsed_s=elapsed,output_tail=out[-16000:],
             semantics='same public tddt_tdoc.cli entry point; no dependency or validation bypass')
    return ok,rec


def _tep_validation(twins:Path|None):
    details={}; ok=bool(twins)
    if not twins: return False,details
    for twin in ('MESA_NAV','WingMPET'):
        root=twins/'resources/tddt_layer2'/twin/'tep'
        missing=[name for name in TEP_FILES if not (root/name).is_file()]
        details[twin]={'root':str(root),'missing':missing,'ok':not missing}
        ok=ok and not missing
    py=twins/'python/twins_fmi/__init__.py'; txt=py.read_text(errors='ignore') if py.is_file() else ''
    details['DomainTwin']={'path':str(py),'ok':bool('DomainTwin' in txt)}
    ok=ok and bool('DomainTwin' in txt)
    return bool(ok),details


def build_layer2_tep(report,twins:Path|None,*,validation_only:bool=False):
    """Stage 08: use the official Layer-2 TEP builder and validate its documented outputs."""
    idx=len(report['stages'])+1; total=report['_total']; stage='Layer-2 official TEP build + current-runtime API compatibility'
    print(f"[{idx:02d}/{total:02d}] {stage} ...",flush=True); t=time.perf_counter()
    builder=twins/'python/build_tddt_layer2_tep.py' if twins else None
    cmd=[sys.executable,'python/build_tddt_layer2_tep.py']
    rc=0; out='validation-only: builder not executed'; elapsed=0.0
    if not validation_only:
        if not builder or not builder.is_file():
            rc=127; out='official builder missing: python/build_tddt_layer2_tep.py'
        else:
            env=os.environ.copy(); pyroot=str(twins/'python'); env['PYTHONPATH']=pyroot+(os.pathsep+env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
            try: rc,out,elapsed=_run_live(cmd,twins,env=env)
            except Exception as exc: rc=127; out=repr(exc); elapsed=round(time.perf_counter()-t,3)
    valid,details=_tep_validation(twins)
    ok=(rc==0 and valid)
    rec={'index':idx,'stage':stage,'ok':ok,'returncode':rc,'elapsed_s':elapsed,'cwd':str(twins) if twins else None,'command':cmd if not validation_only else None,'builder':str(builder) if builder else None,'builder_executed':not validation_only,'required_files':list(TEP_FILES),'validation':details,'output_tail':out[-16000:]}
    report['stages'].append(rec); print(f"      {'PASS' if ok else 'FAIL'} ({rec['elapsed_s']} s)",flush=True)
    if not ok: report['ok']=False
    return ok,rec


def validate_region(region:Path,report):
    if not region.is_file(): return emit(report,'Validate authoritative region/start/destination',False,path=str(region))
    d=json.loads(region.read_text()); roles={str((f.get('properties') or {}).get('feature_role')):f for f in d.get('features',[])}; req={'region_boundary','start','destination'}
    ok=req.issubset(roles)
    return emit(report,'Validate authoritative region/start/destination',ok,path=str(region),sha256=sha(region),roles=sorted(roles),start=roles.get('start',{}).get('geometry',{}).get('coordinates'),destination=roles.get('destination',{}).get('geometry',{}).get('coordinates'))


def check_roots(roots,report):
    details={}; ok=True
    for key,relpaths in REQUIRED_ROOTS.items():
        root=roots.get(key); missing=[] if root else list(relpaths)
        if root: missing=[r for r in relpaths if not (root/r).exists()]
        item_ok=bool(root and root.is_dir() and not missing); ok &= item_ok; details[key]={'root':str(root) if root else None,'missing':missing,'ok':item_ok}
    emit(report,'Validate all external library roots from environment.sh',ok,roots=details,external_only=True)
    return ok



def validate_layer6_flightstack(report, root):
    required=['FlightStack/ardupilot','FlightStack/jsbsim','FlightStack/models/phoenix_v3',
              'FlightStack/phoenix_environment_adapter_v2']
    missing=['root'] if root is None else [r for r in required if not (root/r).exists()]
    return emit(report,'Validate native Layer6 FlightStack assets',not missing,
                root=str(root) if root else None,missing=missing,
                runtime_started=False,semantics='source/assets check only; GUI/MAVLink readiness is checked before Online')


def _safe_remove_build_target(path:Path, package_root:Path) -> dict:
    """Remove only a known reproducible build/cache path contained by package_root."""
    path=path.resolve(); package_root=package_root.resolve()
    try: path.relative_to(package_root)
    except ValueError: return {'path':str(path),'removed':False,'reason':'outside_package_root'}
    if path == package_root: return {'path':str(path),'removed':False,'reason':'refuse_package_root'}
    if not path.exists() and not path.is_symlink(): return {'path':str(path),'removed':False,'reason':'absent'}
    if path.is_dir() and not path.is_symlink(): shutil.rmtree(path)
    else: path.unlink()
    return {'path':str(path),'removed':True}


def cleanup_fmu_build_caches(report:dict, twins:Path|None, climate:Path|None):
    stage='Refresh FMU build caches after successful compilation'
    targets=[]
    specs=[]
    if twins: specs += [(twins/'build',twins)]
    if climate: specs += [(climate/'build',climate)]
    for path,base in specs: targets.append(_safe_remove_build_target(path,base))
    # Final deployable FMUs must survive cleanup.
    finals=[]
    if twins: finals += [twins/'fmus/MESA_NAV.fmu',twins/'fmus/WingMPET.fmu']
    if climate: finals += [climate/'fmu/MicroMetTwin.fmu']
    missing=[str(x) for x in finals if not x.is_file()]
    return emit(report,stage,not missing,targets=targets,preserved_final_fmus=[str(x) for x in finals if x.is_file()],missing_final_fmus=missing,semantics='cleanup is restricted to reproducible build caches/staging; source/config/scripts/final FMUs are preserved')


def main():
    ap=argparse.ArgumentParser(description='TDOC ordered external-package bootstrap, dataset acquisition, build and route-report preparation')
    ap.add_argument('--no-build',action='store_true',help='Compatibility validation-only mode: do not download/rebuild datasets, FMUs or TEPs; still install/check TDOC and preflight all packages.')
    ap.add_argument('--no-download',action='store_true',help='Deprecated alias for --reuse-downloaded-datasets.')
    ap.add_argument('--reuse-downloaded-datasets',action='store_true',help='Forbid network access and rebuild from the integrated-root internet_datasets cache.')
    ap.add_argument('--no-install',action='store_true',help='Skip editable installation of TDOC into the active environment.')
    ap.add_argument('--refresh-crp-sources',action='store_true',help='Force refresh of OSM/DEM/historical climate/magnetic source data; requires network.')
    ap.add_argument('--build-fmus',action='store_true',help='Rebuild the three Layer2/Climate FMUs (MESA_NAV, WingMPET, MicroMetTwin) before any internet-dependent dataset acquisition.')
    ap.add_argument('--build-fmus-refresh',action='store_true',help='Implies --build-fmus; after successful FMU builds remove only reproducible build caches/staging directories while preserving source/config/scripts/final FMUs.')
    ap.add_argument('--historical-start',default='2010-05-20'); ap.add_argument('--historical-end',default='2025-05-20')
    ap.add_argument('--crp-clusters',type=int,default=32); ap.add_argument('--climate-hours',type=int,default=24); ap.add_argument('--route-hour',type=int,default=14); ap.add_argument('--route-mosaics',type=int,default=3)
    ap.add_argument('--report',default=str(REPORT/'PREPARATION_REPORT.json'))
    a=ap.parse_args()
    if a.climate_hours <= 0: ap.error('--climate-hours must be > 0')
    if a.crp_clusters <= 0: ap.error('--crp-clusters must be > 0')
    validation_only=bool(a.no_build); reuse_downloaded=bool(a.reuse_downloaded_datasets or a.no_download)
    build_fmus=bool(a.build_fmus or a.build_fmus_refresh)
    if validation_only and build_fmus: ap.error('--no-build cannot be combined with --build-fmus/--build-fmus-refresh')
    no_download=bool(reuse_downloaded or validation_only)
    roots={k:pathenv(k) for k in REQUIRED_ROOTS}
    roots.update({k:pathenv(k) for k in ('TDDT_SARG_ROOT','TDDT_MAP_ROOT','TDDT_CRP_ROOT')})
    region=pathenv('TDDT_REGION_FILE')
    internet_root=pathenv('TDDT_INTERNET_DATASETS_ROOT')
    boulder_sources=pathenv('TDDT_BOULDER_INTERNET_DATASETS_ROOT')
    climate_sources=pathenv('TDDT_CLIMATE_INTERNET_DATASETS_ROOT')
    missing_env=[name for name,val in [
        ('TDDT_REGION_FILE',region),
        ('TDDT_INTERNET_DATASETS_ROOT',internet_root),
        ('TDDT_BOULDER_INTERNET_DATASETS_ROOT',boulder_sources),
        ('TDDT_CLIMATE_INTERNET_DATASETS_ROOT',climate_sources),
    ] if val is None]
    if missing_env:
        ap.error('configs/environment.sh is authoritative; missing exports: '+', '.join(missing_env))
    boulder_sources.mkdir(parents=True,exist_ok=True); climate_sources.mkdir(parents=True,exist_ok=True)
    total_stages=26 if a.build_fmus_refresh else 25
    report={'schema':'TDOC-central-preparation-v4','package':'TDOC_v20260911_2052','started_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'mode':'validation_only' if validation_only else ('offline_sources' if no_download else 'online_prepare'),'ok':True,'roots':{k:str(v) if v else None for k,v in roots.items()},'stages':[],'_total':total_stages,'ordering_policy':'All requested FMU compilation and external package preparation complete before generated dataset acquisition or any internet-dependent download begins.','build_fmus':build_fmus,'build_fmus_refresh':bool(a.build_fmus_refresh)}

    # 01: install TDOC itself into the active environment.
    prepare_tdoc_entrypoint(report,no_install=bool(a.no_install))
    # 02: all externally-addressed library roots and mandatory builder paths.
    check_roots(roots,report)

    b=roots['TDDT_BOULDER_ROOT']; map_root=roots.get('TDDT_MAP_ROOT'); crp_root=roots.get('TDDT_CRP_ROOT')
    twins=roots['TDDT_TWINS_ROOT']; climate=roots['TDDT_CLIMATE_ROOT']; sargpkg=roots['TDDT_SARG_PACKAGE_ROOT']; sarg_root=roots.get('TDDT_SARG_ROOT')

    # 03-13: PACKAGE PREPARATION PHASE. No generated mission datasets are built before this phase completes.
    if validation_only:
        twins_build_ok=bool(twins and (twins/'fmus/MESA_NAV.fmu').is_file() and (twins/'fmus/WingMPET.fmu').is_file()); emit(report,'Build/validate Navigation + Flight Simulator Twins',twins_build_ok,skipped=True if twins_build_ok else False,reason='--no-build: validate existing FMUs',root=str(twins) if twins else None)
    else:
        twins_build_ok,_=run(report,'Build/validate Navigation + Flight Simulator Twins',['bash','scripts/build_all.sh'],twins)
    if validation_only:
        climate_build_ok=bool(climate and (climate/'fmu/MicroMetTwin.fmu').is_file()); emit(report,'Build Climate Twin FMU',climate_build_ok,skipped=True if climate_build_ok else False,reason='--no-build: validate existing FMU',root=str(climate) if climate else None)
    else:
        climate_build_ok=bool(build_climate(climate,report))
    if a.build_fmus_refresh:
        if twins_build_ok and climate_build_ok:
            cleanup_fmu_build_caches(report,twins,climate)
        else:
            emit(report,'Refresh FMU build caches after successful compilation',False,skipped=True,reason='Twin FMU builds failed; caches retained for diagnosis')
    source_package_ready(report,'Prepare ISO unit/frame/time adapter package',roots['TDDT_UNIT_ADAPTER_ROOT'],REQUIRED_ROOTS['TDDT_UNIT_ADAPTER_ROOT'],semantics='source-only adapter; final API preflight occurs after datasets')
    source_package_ready(report,'Prepare Boulder map/CRP package',b,REQUIRED_ROOTS['TDDT_BOULDER_ROOT'],semantics='Python dataset builder package; dataset generation occurs after all packages are ready')
    source_package_ready(report,'Prepare SARG package',sargpkg,REQUIRED_ROOTS['TDDT_SARG_PACKAGE_ROOT'],semantics='Python dataset builder package; SARG generation occurs after CRP and Layer2 TEP')
    # Stage 08 is intentionally the official Layer2 TEP builder/compatibility stage.
    build_layer2_tep(report,twins,validation_only=validation_only)
    source_package_ready(report,'Prepare Layer 3 package',roots['TDDT_LAYER3_ROOT'],REQUIRED_ROOTS['TDDT_LAYER3_ROOT'],semantics='Python Layer3 source package; runtime preflight occurs after CRP/SARG/TEP are available')
    source_package_ready(report,'Prepare Layer 4 package',roots['TDDT_LAYER4_ROOT'],REQUIRED_ROOTS['TDDT_LAYER4_ROOT'],semantics='Python Layer4 source package; optimization preflight occurs after Layer3 readiness')
    source_package_ready(report,'Prepare Layer 5 package',roots['TDDT_LAYER5_ROOT'],REQUIRED_ROOTS['TDDT_LAYER5_ROOT'],semantics='Python Layer5 source package; decision preflight occurs after Layer4 readiness')
    source_package_ready(report,'Prepare Layer6 client package',roots['TDDT_LAYER6_ROOT'],REQUIRED_ROOTS['TDDT_LAYER6_ROOT'],semantics='Native GUI/MAVLink execution gateway; no FlightStack session started during preparation')
    source_package_ready(report,'Prepare Layer 7 package',roots['TDDT_LAYER7_ROOT'],REQUIRED_ROOTS['TDDT_LAYER7_ROOT'],semantics='Python Layer7 side module; final feedback/adaptation preflight occurs after core layers')

    # 14: authoritative mission geography after all external package source/build readiness.
    validate_region(region,report)

    # 15-16: acquire/reuse online source data then build CRP.
    if validation_only:
        req=['terrain_obstacles.tif','climate_contexts.tif','magnetic_anomaly.tif','metadata.json']; ok=bool(map_root and all((map_root/x).is_file() for x in req)); emit(report,'Validate downloaded regional source/map data required by CRP',ok,root=str(map_root) if map_root else None,required=req)
        reqc=['crp_cluster_catalog.csv','crp_runtime_statistics.json','crp_cluster_map.tif']; ok=bool(crp_root and all((crp_root/x).is_file() for x in reqc)); emit(report,'Validate canonical CRP dataset',ok,root=str(crp_root) if crp_root else None,required=reqc)
    else:
        map_req=['terrain_obstacles.tif','climate_contexts.tif','magnetic_anomaly.tif','metadata.json']
        reusable_map=bool(no_download and map_root and all((map_root/x).is_file() for x in map_req))
        if reusable_map:
            # --reuse-downloaded-datasets is a strict no-network mode.  If the
            # integrated bundle already contains a complete canonical regional map,
            # validate/reuse it instead of invoking build_regional_map.py (whose OSM
            # acquisition stack may include optional packages such as osmnx).
            emit(report,'Acquire/reuse online terrain/OSM/climate/magnetic data required by CRP',True,
                 skipped=True,reason='offline reuse of complete canonical regional map',
                 root=str(map_root),required=map_req,network_access=False)
        else:
            cmd=[sys.executable,'build_regional_map.py','--region-file',str(region),'--start-date',a.historical_start,'--end-date',a.historical_end,'--output',str(map_root),'--source-dir',str(boulder_sources/'offline_sources'),'--magnetic-dir',str(boulder_sources/'magnetic')]
            if no_download: cmd.append('--offline')
            elif a.refresh_crp_sources: cmd += ['--refresh-osm','--refresh-dem','--refresh-climate','--refresh-magnetic']
            run(report,'Acquire/reuse online terrain/OSM/climate/magnetic data required by CRP',cmd,b)
        run(report,'Build canonical CRP dataset',[sys.executable,'build_crp_dataset.py','--region-file',str(region),'--terrain',str(map_root/'terrain_obstacles.tif'),'--climate',str(map_root/'climate_contexts.tif'),'--magnetic',str(map_root/'magnetic_anomaly.tif'),'--output-root',str(map_root),'--clusters',str(a.crp_clusters)],b)

    # 17: SARG is generated only after packages + official Layer2 TEP + CRP are ready.
    if validation_only:
        ok=bool(sarg_root and (sarg_root/'sarg_records.csv').is_file() and (sarg_root/'routes').is_dir() and any((sarg_root/'routes').glob('*.csv'))); emit(report,'Validate SARG dataset built from Twins + Map + CRP',ok,root=str(sarg_root) if sarg_root else None,route_count=len(list((sarg_root/'routes').glob('*.csv'))) if sarg_root and (sarg_root/'routes').is_dir() else 0)
    else:
        run(report,'Build SARG dataset from Twins + Map + CRP',[sys.executable,'-m','sarg_builder.cli','--twins-library',str(twins),'--maps',str(map_root),'--crp',str(crp_root),'--region-file',str(region),'--output',str(sarg_root)],sargpkg)

    # 18: current forcing horizon is configurable; 24 h remains the default.
    # Downloaded bytes live only in the integrated-root cache.  A relative symlink is
    # materialized into Climate's legacy climate_dataset path so all package consumers
    # resolve the same bytes without a second download/copy.
    forcing=climate_sources/forcing_filename(a.climate_hours) if climate else None
    if validation_only:
        rec=emit(report,'Validate centrally cached Climate Twin forcing dataset',bool(forcing and forcing.is_file()),path=str(forcing) if forcing else None,sha256=sha(forcing) if forcing and forcing.is_file() else None,network_access=False,cache_root=str(climate_sources))
        if forcing and forcing.is_file():
            os.environ['TDDT_CLIMATE_FORCING_FILE']=str(forcing); write_central_active_manifest(forcing,hours=a.climate_hours)
            link=climate/'climate_dataset'/forcing.name if climate else None
            rec['runtime_materialization_ok']=bool(link and link.is_symlink() and link.is_file() and sha(link)==sha(forcing))
            rec['runtime_materialization']=str(link) if link else None
            if not rec['runtime_materialization_ok']: report['ok']=False
    elif no_download:
        rec=emit(report,'Reuse centrally cached Climate Twin forcing dataset',bool(forcing and forcing.is_file()),path=str(forcing) if forcing else None,sha256=sha(forcing) if forcing and forcing.is_file() else None,network_access=False,cache_root=str(climate_sources))
        if forcing and forcing.is_file():
            os.environ['TDDT_CLIMATE_FORCING_FILE']=str(forcing); write_central_active_manifest(forcing,hours=a.climate_hours)
            mat=materialize_climate_forcing_link(climate,forcing,hours=a.climate_hours); rec['runtime_materialization']=mat
            if not mat.get('ok'): report['ok']=False
    else:
        forcing_ok,rec=run(report,'Download current Climate Twin forcing data to central internet dataset cache',[sys.executable,'scripts/download_openmeteo.py','--region',str(region),'--output',str(forcing),'--hours',str(a.climate_hours)],climate)
        if forcing_ok and forcing and forcing.is_file():
            os.environ['TDDT_CLIMATE_FORCING_FILE']=str(forcing); write_central_active_manifest(forcing,hours=a.climate_hours); rec['forcing_hours']=int(a.climate_hours); rec['central_cache']=True
            mat=materialize_climate_forcing_link(climate,forcing,hours=a.climate_hours); rec['runtime_materialization']=mat
            if not mat.get('ok'): report['ok']=False

    # 19: official map CLI route/region inspection + TDOC-owned report copies.
    route_dir=HERE/'reports/preparation/route_cli'; shutil.rmtree(route_dir,ignore_errors=True); route_dir.mkdir(parents=True,exist_ok=True)
    route_work=(b/'outputs/tdoc_route_cli_work') if b else None
    if b and map_root:
        shutil.rmtree(route_work,ignore_errors=True)
        cmd=[sys.executable,'map_mosaic_query.py','route3d','--terrain-input',str(map_root/'terrain_obstacles.tif'),'--climate-input',str(map_root/'climate_contexts.tif'),'--region-file',str(region),'--mosaic-count',str(a.route_mosaics),'--hour',str(a.route_hour),'--output-dir',str(route_work),'--prefix','tdoc_preflight_route','--max-grid','180']
        if (map_root/'magnetic_anomaly.tif').is_file(): cmd[cmd.index('--region-file'):cmd.index('--region-file')] = ['--magnetic-input',str(map_root/'magnetic_anomaly.tif')]
        ok,rec=run(report,'Inspect route/region and generate CLI route reports',cmd,b)
        copied=[]
        if ok:
            for fp in route_work.rglob('*'):
                if fp.is_file() and fp.suffix.lower() in {'.html','.json','.csv'}:
                    dest=route_dir/fp.relative_to(route_work); dest.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(fp,dest); copied.append(dest.relative_to(HERE).as_posix())
            rec['tdoc_report_only_artifacts']=copied; rec['external_work_dir']=str(route_work); rec['raster_payload_copied_into_tdoc']=False
    else: emit(report,'Inspect route/region and generate CLI route reports',False,reason='map root unavailable')

    # 20-23: final package/API preflights after all required generated datasets exist.
    iso=roots['TDDT_UNIT_ADAPTER_ROOT']; emit(report,'Validate ISO unit/frame/time adapter package/API',bool(iso and (iso/'src/tdoc_unit_converter/__init__.py').is_file()),root=str(iso) if iso else None)
    # Validate both the TDOC adapter boundary and the real external Layer-3 runtime.
    # The external CLI owns stricter package/dataset checks, including active climate forcing.
    run(report,'Layer 3 TDOC adapter preflight',[sys.executable,'-c',f'from tddt_tdoc import TDOCConfig; from tddt_tdoc.unit_adapter import UnitAdapterBinding; from tddt_tdoc.adapters.layer2 import Layer2Adapter; from tddt_tdoc.adapters.layer3 import Layer3Adapter; c=TDOCConfig.from_yaml(r"{HERE/"configs/tdoc.yaml"}"); u=UnitAdapterBinding(c); a2=Layer2Adapter(c,u); r=Layer3Adapter(c,u,a2).preflight(); print(r); raise SystemExit(0 if r.get("ok") else 2)'],HERE)
    l3=roots['TDDT_LAYER3_ROOT']
    run(report,'Layer 3 strict external runtime/dataset preflight',[sys.executable,'-m','layer3.cli','preflight','--config','configs/configuration_boulder.yaml'],l3,env={**os.environ,'PYTHONPATH':str(l3/'src')+os.pathsep+os.environ.get('PYTHONPATH','')})
    l4=roots['TDDT_LAYER4_ROOT']; l5=roots['TDDT_LAYER5_ROOT']
    shell=f'cd {l4!s} && PYTHONPATH=src:${{PYTHONPATH:-}} {sys.executable} -m layer4.cli preflight --config configs/configuration_boulder.yaml && cd {l5!s} && PYTHONPATH=src:${{PYTHONPATH:-}} {sys.executable} -c \'from layer5 import Layer5Config,Layer5Engine; c=Layer5Config.from_yaml("configs/configuration_phoenix.yaml"); r=Layer5Engine(c).preflight(strict_external=False); print(r); raise SystemExit(0 if r.get("ok",True) else 2)\''
    run(report,'Layer 4 + Layer 5 model/decision preflight',['bash','-lc',shell],HERE)
    run(report,'Layer 7 feedback/adaptation preflight',[sys.executable,'-m','tddt_layer7.cli','--config','configs/default.json','preflight'],roots['TDDT_LAYER7_ROOT'])

    validate_layer6_flightstack(report,roots.get('TDDT_LAYER6_ROOT'))

    report.pop('_total',None); report['finished_at']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()); report['generated']={'map_root':str(map_root),'crp_root':str(crp_root),'sarg_root':str(sarg_root),'climate_forcing':str(forcing),'route_report_dir':str(route_dir)}
    out=Path(a.report); out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(report,indent=2,ensure_ascii=False)); print(f"Preparation report: {out}\nOVERALL: {'PASS' if report['ok'] else 'FAIL'}")
    return 0 if report['ok'] else 2

if __name__=='__main__': raise SystemExit(main())
