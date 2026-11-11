from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import selectors
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import TDOCConfig
from .climate_forcing import generated_forcing_targets


@dataclass
class ResetItem:
    category: str
    path: str
    action: str
    existed: bool
    removed: bool
    bytes_before: int = 0
    reason: str | None = None


def _size(path: Path) -> int:
    try:
        if path.is_file() or path.is_symlink():
            return path.lstat().st_size
        return sum(p.lstat().st_size for p in path.rglob('*') if p.is_file() or p.is_symlink())
    except Exception:
        return 0


def _reset_identity(path: Path) -> Path:
    """Return the path to delete/stage without dereferencing generated symlinks."""
    q=Path(path).expanduser()
    return q.absolute() if q.is_symlink() else q.resolve()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _dangerous(path: Path, package_root: Path, external_roots: list[Path]) -> str | None:
    p=_reset_identity(path)
    home=Path.home().resolve()
    forbidden={Path('/').resolve(), home, package_root.resolve()}
    forbidden.update(r.resolve() for r in external_roots if r)
    if p in forbidden:
        return 'refusing to delete filesystem/home/package/library root'
    if len(p.parts) < 3:
        return 'path is too shallow for destructive reset'
    return None


def _remove(path: Path, *, category: str, package_root: Path, external_roots: list[Path], dry_run: bool, progress: Callable[[str],None]|None=None) -> ResetItem:
    p=_reset_identity(path)
    existed=p.exists() or p.is_symlink()
    n=_size(p) if existed else 0
    if progress:
        progress(f'[reset] {category}: inspect {p} ({n} bytes)')
    why=_dangerous(p,package_root,external_roots)
    if why:
        item=ResetItem(category,str(p),'skip',existed,False,n,why)
        if progress: progress(f'[reset] {category}: SKIP - {why}')
        return item
    if not existed:
        item=ResetItem(category,str(p),'skip',False,False,0,'not present')
        if progress: progress(f'[reset] {category}: SKIP - not present')
        return item
    if dry_run:
        item=ResetItem(category,str(p),'would_delete',True,False,n,'dry-run')
        if progress: progress(f'[reset] {category}: WOULD DELETE {n} bytes')
        return item
    try:
        if progress: progress(f'[reset] {category}: deleting ...')
        if p.is_dir() and not p.is_symlink():
            # A runtime writer can finish a final atomic rename while reset is
            # entering rmtree().  Retry a bounded number of times on ENOTEMPTY
            # instead of leaving a partially purged runtime tree.  This does not
            # make reset safe to run concurrently with training; it only closes
            # the short final-write race after the writer has stopped.
            import errno, time as _time
            last_exc=None
            for attempt in range(4):
                try:
                    shutil.rmtree(p)
                    last_exc=None
                    break
                except OSError as exc:
                    last_exc=exc
                    if exc.errno not in (errno.ENOTEMPTY, errno.EBUSY) or attempt >= 3:
                        raise
                    _time.sleep(0.05*(attempt+1))
            if last_exc is not None:
                raise last_exc
        else: p.unlink(missing_ok=True)
        item=ResetItem(category,str(p),'delete',True,True,n,None)
        if progress: progress(f'[reset] {category}: DELETED {n} bytes')
        return item
    except Exception as exc:
        item=ResetItem(category,str(p),'error',True,False,n,repr(exc))
        if progress: progress(f'[reset] {category}: ERROR {exc!r}')
        return item




def _stage_external_for_refresh(path: Path, *, category: str, backup_root: Path, package_root: Path, external_roots: list[Path], progress: Callable[[str],None]|None=None) -> tuple[ResetItem, Path | None]:
    """Transactionally move a generated external target aside before refresh.

    A failed preparation can therefore restore the exact pre-refresh dataset instead
    of leaving the integrated bundle with CRP/SARG/TEP/forcing deleted.  Source
    package roots are still protected by _dangerous() and are never staged.
    """
    p=_reset_identity(path); existed=p.exists() or p.is_symlink(); n=_size(p) if existed else 0
    if progress: progress(f'[reset] {category}: inspect {p} ({n} bytes)')
    why=_dangerous(p,package_root,external_roots)
    if why:
        item=ResetItem(category,str(p),'skip',existed,False,n,why)
        if progress: progress(f'[reset] {category}: SKIP - {why}')
        return item,None
    if not existed:
        item=ResetItem(category,str(p),'skip',False,False,0,'not present')
        if progress: progress(f'[reset] {category}: SKIP - not present')
        return item,None
    backup_root.mkdir(parents=True,exist_ok=True)
    # Preserve basename for diagnostics while guaranteeing uniqueness across package roots.
    import hashlib
    tag=hashlib.sha256(str(p).encode('utf-8')).hexdigest()[:12]
    dst=backup_root/f'{tag}__{p.name}'
    if dst.exists() or dst.is_symlink():
        if dst.is_dir() and not dst.is_symlink(): shutil.rmtree(dst)
        else: dst.unlink(missing_ok=True)
    if progress: progress(f'[reset] {category}: staging transaction backup -> {dst}')
    shutil.move(str(p),str(dst))
    return ResetItem(category,str(p),'stage_for_refresh',True,True,n,f'transaction backup: {dst}'),dst


def _restore_refresh_backups(staged: list[tuple[str,Path,Path]], *, progress: Callable[[str],None]|None=None) -> list[dict[str,Any]]:
    outcomes=[]
    # Restore deepest paths first so nested output structures cannot hide a parent move.
    for category,original,backup in sorted(staged,key=lambda x:len(x[1].parts),reverse=True):
        try:
            if original.exists() or original.is_symlink():
                if original.is_dir() and not original.is_symlink(): shutil.rmtree(original)
                else: original.unlink(missing_ok=True)
            original.parent.mkdir(parents=True,exist_ok=True)
            shutil.move(str(backup),str(original))
            outcomes.append({'category':category,'path':str(original),'status':'RESTORED'})
            if progress: progress(f'[refresh] ROLLBACK restored {category}: {original}')
        except Exception as exc:
            outcomes.append({'category':category,'path':str(original),'status':'RESTORE_FAILED','error':repr(exc)})
            if progress: progress(f'[refresh] ROLLBACK ERROR {category}: {exc!r}')
    return outcomes


def _recover_orphaned_refresh_backups(targets: list[tuple[str,Path]], *,
                                      backup_root: Path,
                                      progress: Callable[[str],None]|None=None) -> list[dict[str,Any]]:
    """Recover a refresh transaction interrupted after staging and before rollback.

    Backup names are deterministic hashes of the exact generated target.  Recovery
    is therefore limited to targets already admitted by ``_safe_external_targets``;
    no source root or inferred dataset path can be restored here.  A present target
    remains authoritative and is staged normally by the new transaction.
    """
    if not backup_root.exists():
        return []
    import hashlib
    outcomes=[]
    for category,target in targets:
        original=_reset_identity(target)
        tag=hashlib.sha256(str(original).encode('utf-8')).hexdigest()[:12]
        backup=backup_root/f'{tag}__{original.name}'
        if not (backup.exists() or backup.is_symlink()):
            continue
        if original.exists() or original.is_symlink():
            outcomes.append({'category':category,'path':str(original),
                             'backup':str(backup),'status':'TARGET_PRESENT'})
            continue
        try:
            original.parent.mkdir(parents=True,exist_ok=True)
            shutil.move(str(backup),str(original))
            outcomes.append({'category':category,'path':str(original),
                             'backup':str(backup),'status':'RESTORED'})
            if progress:
                progress(f'[refresh] RECOVERY restored interrupted transaction target {category}: {original}')
        except Exception as exc:
            outcomes.append({'category':category,'path':str(original),
                             'backup':str(backup),'status':'RESTORE_FAILED','error':repr(exc)})
            if progress:
                progress(f'[refresh] RECOVERY ERROR {category}: {exc!r}')
    try:
        if backup_root.exists() and not any(backup_root.iterdir()):
            backup_root.rmdir()
    except OSError:
        pass
    return outcomes

def _env_path(name: str) -> Path | None:
    v=os.environ.get(name)
    return Path(v).expanduser().resolve() if v else None


def _safe_external_targets(cfg: TDOCConfig) -> tuple[list[tuple[str,Path]], list[dict[str,Any]]]:
    """Return only generated/downloaded dataset targets whose containment is provable.

    External source package roots themselves are never returned.
    """
    notes=[]; targets=[]
    boulder=_env_path('TDDT_BOULDER_ROOT')
    sargpkg=_env_path('TDDT_SARG_PACKAGE_ROOT')
    climate=_env_path('TDDT_CLIMATE_ROOT')
    twins=_env_path('TDDT_TWINS_ROOT')
    map_root=_env_path('TDDT_MAP_ROOT')
    crp_root=_env_path('TDDT_CRP_ROOT')
    sarg_root=_env_path('TDDT_SARG_ROOT')

    if boulder and map_root:
        expected=(boulder/'outputs').resolve()
        if _is_relative_to(map_root.resolve(), expected):
            targets.append(('dataset.map_crp',map_root.resolve()))
        else:
            notes.append({'target':'TDDT_MAP_ROOT','path':str(map_root),'status':'skipped','reason':'not provably under TDDT_BOULDER_ROOT/outputs'})
    elif map_root:
        notes.append({'target':'TDDT_MAP_ROOT','path':str(map_root),'status':'skipped','reason':'TDDT_BOULDER_ROOT unavailable'})

    # CRP may be configured separately. Only add it when it is not already covered by map_root
    # and containment under Boulder outputs can still be proven.
    if crp_root and not (map_root and _is_relative_to(crp_root.resolve(),map_root.resolve())):
        if boulder and _is_relative_to(crp_root.resolve(),(boulder/'outputs').resolve()):
            targets.append(('dataset.crp',crp_root.resolve()))
        else:
            notes.append({'target':'TDDT_CRP_ROOT','path':str(crp_root),'status':'skipped','reason':'not provably under TDDT_BOULDER_ROOT/outputs'})

    if boulder:
        targets.append(('dataset.route_work',(boulder/'outputs/tdoc_route_cli_work').resolve()))

    if sargpkg and sarg_root:
        if _is_relative_to(sarg_root.resolve(),(sargpkg/'generated').resolve()):
            targets.append(('dataset.sarg',sarg_root.resolve()))
        else:
            notes.append({'target':'TDDT_SARG_ROOT','path':str(sarg_root),'status':'skipped','reason':'not provably under TDDT_SARG_PACKAGE_ROOT/generated'})
    elif sarg_root:
        notes.append({'target':'TDDT_SARG_ROOT','path':str(sarg_root),'status':'skipped','reason':'TDDT_SARG_PACKAGE_ROOT unavailable'})

    if twins:
        # TEP is generated Layer-2 compatibility/runtime material. Delete only the exact
        # per-Twin TEP directories, never the surrounding resources or FMU library.
        for twin_name in ('MESA_NAV','WingMPET'):
            tep=(twins/'resources'/'tddt_layer2'/twin_name/'tep').resolve()
            if _is_relative_to(tep,(twins/'resources'/'tddt_layer2').resolve()):
                targets.append((f'dataset.tep.{twin_name}',tep))
    else:
        notes.append({'target':'Layer2 TEP','path':None,'status':'skipped','reason':'TDDT_TWINS_ROOT unavailable'})

    if climate:
        # Remove every TDOC-managed Open-Meteo horizon plus its active-forcing pointer.
        # Static Climate source, FMUs and unrelated climate_dataset files are preserved.
        for fp in generated_forcing_targets(climate):
            targets.append(('dataset.climate_forcing',_reset_identity(fp)))

    return targets,notes



def _run_streaming(cmd:list[str], cwd:Path, *, progress:Callable[[str],None]|None=None, timeout_s:float|None=None) -> dict[str,Any]:
    """Run the preparation pipeline with live output and an optional whole-run timeout."""
    started=time.perf_counter(); lines=[]; timed_out=False
    if progress:
        progress('[refresh] command: '+' '.join(str(x) for x in cmd))
    p=subprocess.Popen(cmd,cwd=str(cwd),text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,bufsize=1)
    sel=selectors.DefaultSelector()
    if p.stdout is not None: sel.register(p.stdout,selectors.EVENT_READ)
    while True:
        events=sel.select(timeout=0.5)
        if events:
            for key,_ in events:
                line=key.fileobj.readline()
                if line:
                    line=line.rstrip('\n'); lines.append(line)
                    if len(lines)>1200: lines=lines[-1200:]
                    if progress: progress('[refresh] '+line)
        if timeout_s and timeout_s>0 and time.perf_counter()-started>timeout_s and p.poll() is None:
            timed_out=True
            if progress: progress(f'[refresh] TIMEOUT after {timeout_s:.1f} s; terminating preparation')
            p.terminate()
            try: p.wait(timeout=5)
            except subprocess.TimeoutExpired: p.kill(); p.wait()
        if p.poll() is not None:
            if p.stdout is not None:
                rest=p.stdout.read()
                if rest:
                    for line in rest.splitlines():
                        lines.append(line)
                        if progress: progress('[refresh] '+line)
            break
    try: sel.close()
    except Exception: pass
    return {'returncode':int(p.returncode or 0),'stdout_tail':'\n'.join(lines)[-12000:],'timed_out':timed_out,'elapsed_s':round(time.perf_counter()-started,3)}

def reset_tdoc(cfg: TDOCConfig, *, dry_run: bool=False, include_external: bool=True, output: str|Path|None=None, refresh_external: bool=False, refresh_options: dict[str,Any]|None=None, progress: Callable[[str],None]|None=None) -> dict[str,Any]:
    root=cfg.package_root.resolve()
    if progress: progress(f'[reset] package={cfg.get("package.version","unknown")} dry_run={dry_run} include_external={include_external} refresh_external={refresh_external}')
    # Do not call cfg.runtime_root here: it auto-creates the directory before deletion.
    raw_runtime=Path(str(cfg.get('runtime.root','runtime')))
    runtime=(raw_runtime if raw_runtime.is_absolute() else root/raw_runtime).resolve()

    external_roots=[p for p in [
        _env_path('TDDT_TWINS_ROOT'),_env_path('TDDT_CLIMATE_ROOT'),_env_path('TDDT_INTERNET_DATASETS_ROOT'),_env_path('TDDT_UNIT_ADAPTER_ROOT'),
        _env_path('TDDT_BOULDER_ROOT'),_env_path('TDDT_SARG_PACKAGE_ROOT'),_env_path('TDDT_LAYER3_ROOT'),
        _env_path('TDDT_LAYER4_ROOT'),_env_path('TDDT_LAYER5_ROOT'),_env_path('TDDT_LAYER6_ROOT'),_env_path('TDDT_LAYER7_ROOT')
    ] if p]

    items=[]
    # TDOC-owned runtime contains cycles, all Layer2-7 per-layer knowledge/records,
    # Layer3/4 native mirrors, Layer5/7 KStores, provenance, feedback and version state.
    items.append(_remove(runtime,category='tdoc.runtime_and_knowledge',package_root=root,external_roots=external_roots,dry_run=dry_run,progress=progress))

    # Explicit mutable knowledge overrides may live outside the default runtime tree.
    # Accept only clearly named KStore/state/runtime paths and never a library/package root.
    knowledge_candidates=[]
    for key in ('TDDT_LAYER5_KSTORE_ROOT','TDDT_KSTORE_ROOT','TDDT_LAYER7_KSTORE_BASE','TDDT_LAYER7_STATE_ROOT'):
        kp=_env_path(key) or cfg.path_value(key)
        if not kp:
            continue
        rp=kp.resolve()
        if _is_relative_to(rp,runtime):
            continue
        tokens={x.lower() for x in rp.parts}
        named=any(('kstore' in x or x in {'state','runtime','knowledge'}) for x in tokens)
        if named:
            knowledge_candidates.append((f'knowledge.{key}',rp))
    seen=set()
    for cat,kp in knowledge_candidates:
        if kp in seen: continue
        seen.add(kp); items.append(_remove(kp,category=cat,package_root=root,external_roots=external_roots,dry_run=dry_run,progress=progress))

    # Run/preparation/validation reports contain generated mission/dataset material and are
    # intentionally reset. Static top-level release metadata and documentation are preserved.
    reports=root/'reports'
    items.append(_remove(reports,category='tdoc.generated_reports',package_root=root,external_roots=external_roots,dry_run=dry_run,progress=progress))
    # TDOC-owned communication/data-exchange logs are generated runtime evidence and
    # can dominate lightweight release size. They are safe to purge because static
    # documentation/source lives outside logs/.
    logs=root/'logs'
    items.append(_remove(logs,category='tdoc.generated_logs',package_root=root,external_roots=external_roots,dry_run=dry_run,progress=progress))

    external_notes=[]; orphan_recovery=[]
    if include_external:
        targets,external_notes=_safe_external_targets(cfg)
        # Deduplicate nested targets: deleting map_root already covers CRP.
        unique=[]
        for cat,p in targets:
            if any(p==q or _is_relative_to(p,q) for _,q in unique):
                continue
            unique.append((cat,p))
        staged_external=[]
        if refresh_external and not dry_run:
            backup_root=root/'.refresh_external_transaction_backup'
            orphan_recovery=_recover_orphaned_refresh_backups(
                unique,backup_root=backup_root,progress=progress)
            recovery_failed=[x for x in orphan_recovery if x.get('status')=='RESTORE_FAILED']
            if recovery_failed:
                for failure in recovery_failed:
                    items.append(ResetItem(
                        'refresh.orphan_recovery',str(failure.get('path')),'error',
                        True,False,0,str(failure.get('error') or 'restore failed')))
            else:
                for cat,p in unique:
                    item,bak=_stage_external_for_refresh(p,category=cat,backup_root=backup_root,package_root=root,external_roots=external_roots,progress=progress)
                    items.append(item)
                    if bak is not None: staged_external.append((cat,p.resolve(),bak))
        else:
            for cat,p in unique:
                items.append(_remove(p,category=cat,package_root=root,external_roots=external_roots,dry_run=dry_run,progress=progress))
    else:
        staged_external=[]
        backup_root=root/'.refresh_external_transaction_backup'

    deleted_bytes=sum(x.bytes_before for x in items if x.removed)
    errors=[asdict(x) for x in items if x.action=='error']
    report={
        'schema':'TDOC-reset-v2',
        'package':str(cfg.get('package.version','unknown')),
        'executed_at':datetime.now(timezone.utc).isoformat(),
        'dry_run':bool(dry_run),
        'include_external':bool(include_external),
        'refresh_external_requested':bool(refresh_external),
        'policy':{
            'deleted':'TDOC runtime/knowledge + generated reports/logs + provably-contained generated/downloaded external datasets including CRP/Map, SARG, Layer-2 TEP and all TDOC Open-Meteo horizons',
            'preserved':['source code','configs','formalism/docs','region/region_v1.geojson','external library source roots','Twin/Climate library FMUs'],
            'external_safety':'external generated data is removed only at explicit known dataset paths or when containment under its authoritative output/generated/resource root is provable',
            'refresh_semantics':'refresh is opt-in; reset alone remains purge-only. With --refresh-external, generated external datasets are transactionally staged and automatically restored if preparation fails.'

        },
        'items':[asdict(x) for x in items],
        'external_notes':external_notes,
        'interrupted_refresh_recovery':orphan_recovery,
        'deleted_bytes':deleted_bytes,
        'ok':not errors,
        'errors':errors,
    }

    if not dry_run:
        # Recreate only empty TDOC-owned roots plus a reset audit. No preparation/training data
        # are recreated unless refresh_external was explicitly requested.
        runtime.mkdir(parents=True,exist_ok=True)
        reset_dir=root/'reports/reset'; reset_dir.mkdir(parents=True,exist_ok=True)
        out=Path(output).resolve() if output else reset_dir/'RESET_REPORT.json'
        out.parent.mkdir(parents=True,exist_ok=True)
        report['report_path']=str(out)
        if refresh_external:
            if not include_external:
                report['refresh']={'ok':False,'status':'REFUSED','reason':'--refresh-external requires external reset; remove --no-external'}
                report['ok']=False
            elif errors:
                report['refresh']={'ok':False,'status':'REFUSED','reason':'reset or interrupted-refresh recovery failed before preparation'}
                report['ok']=False
            else:
                opts=dict(refresh_options or {})
                if progress: progress('[refresh] external purge complete; starting ordered package preparation + dataset refresh')
                cmd=[sys.executable,str(root/'scripts'/'prepare_all.py')]
                if not opts.get('reuse_downloaded_datasets'): cmd.append('--no-install')
                scalar_flags={
                    'climate_hours':'--climate-hours','crp_clusters':'--crp-clusters',
                    'historical_start':'--historical-start','historical_end':'--historical-end',
                    'route_hour':'--route-hour','route_mosaics':'--route-mosaics',
                }
                for key,flag in scalar_flags.items():
                    if opts.get(key) is not None: cmd.extend([flag,str(opts[key])])
                if opts.get('refresh_crp_sources'): cmd.append('--refresh-crp-sources')
                if opts.get('build_fmus'): cmd.append('--build-fmus')
                if opts.get('build_fmus_refresh'): cmd.append('--build-fmus-refresh')
                if opts.get('reuse_downloaded_datasets'):
                    cmd.append('--reuse-downloaded-datasets')
                    # Offline/reuse refresh must not make a valid integrated bundle
                    # unrebuildable merely because an optional network-oriented builder
                    # dependency is absent.  Keep the transaction backup authoritative,
                    # but seed the documented generated targets from that backup so
                    # prepare_all can validate/reuse canonical Map/CRP/SARG/TEP inputs
                    # without network access.  A failed preparation still rolls back
                    # from the untouched backup below.
                    for _cat,_target,_backup in staged_external:
                        try:
                            if _target.exists():
                                if _target.is_dir() and not _target.is_symlink(): shutil.rmtree(_target)
                                else: _target.unlink()
                            _target.parent.mkdir(parents=True,exist_ok=True)
                            if _backup.is_dir() and not _backup.is_symlink():
                                shutil.copytree(_backup,_target,symlinks=True)
                            elif _backup.is_symlink():
                                _target.symlink_to(os.readlink(_backup))
                            else:
                                shutil.copy2(_backup,_target,follow_symlinks=False)
                            if progress: progress(f'[refresh] seeded offline-reuse target from transaction backup: {_target}')
                        except Exception as seed_exc:
                            if progress: progress(f'[refresh] seed warning for {_target}: {seed_exc!r}')
                elif opts.get('no_download'): cmd.append('--no-download')
                started=datetime.now(timezone.utc).isoformat()
                timeout_s=float(opts.get('refresh_timeout_s') or 0)
                try:
                    rr=_run_streaming(cmd,root,progress=progress,timeout_s=timeout_s if timeout_s>0 else None)
                    refresh_ok=(rr['returncode']==0 and not rr['timed_out'])
                    report['refresh']={
                        'ok':refresh_ok,'status':'OK' if refresh_ok else ('TIMEOUT' if rr['timed_out'] else 'FAILED'),
                        'started_at':started,'command':cmd,'returncode':rr['returncode'],
                        'elapsed_s':rr['elapsed_s'],'timed_out':rr['timed_out'],'stdout_tail':rr['stdout_tail'],
                        'semantics':'Official TDOC central preparation pipeline with live progress; external package source trees remain unmodified except their documented generated/output/resource dataset locations.'
                    }
                    if refresh_ok:
                        # Refresh succeeded: the staged pre-refresh datasets are obsolete.
                        if backup_root.exists(): shutil.rmtree(backup_root,ignore_errors=True)
                        report['refresh']['transaction']={'status':'COMMITTED','staged_count':len(staged_external)}
                    else:
                        rollback=_restore_refresh_backups(staged_external,progress=progress)
                        if backup_root.exists() and not any(x.get('status')=='RESTORE_FAILED' for x in rollback): shutil.rmtree(backup_root,ignore_errors=True)
                        report['refresh']['transaction']={'status':'ROLLED_BACK','staged_count':len(staged_external),'restore':rollback}
                        report['ok']=False
                except Exception as exc:
                    rollback=_restore_refresh_backups(staged_external,progress=progress)
                    report['refresh']={'ok':False,'status':'FAILED','started_at':started,'command':cmd,'error':repr(exc),'transaction':{'status':'ROLLED_BACK','staged_count':len(staged_external),'restore':rollback}}
                    if backup_root.exists() and not any(x.get('status')=='RESTORE_FAILED' for x in rollback): shutil.rmtree(backup_root,ignore_errors=True)
                    if progress: progress(f'[refresh] ERROR {exc!r}')
                    report['ok']=False
        out.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    elif output:
        out=Path(output); out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    return report
