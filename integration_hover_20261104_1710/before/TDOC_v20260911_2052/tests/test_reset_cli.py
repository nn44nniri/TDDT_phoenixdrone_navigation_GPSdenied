from __future__ import annotations
import json
import os
import pytest
from pathlib import Path
import yaml
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.reset import reset_tdoc


@pytest.fixture(autouse=True)
def isolate_reset_test_external_roots(monkeypatch):
    """Reset unit tests must never inherit the user's real prepared data roots.

    Each test may explicitly inject its own temporary external paths afterwards.
    This changes no reset behavior and weakens none of the deletion assertions.
    """
    for key in tuple(os.environ):
        if key.startswith("TDDT_") and "ROOT" in key:
            monkeypatch.delenv(key, raising=False)


def _cfg(tmp_path:Path)->TDOCConfig:
    root=tmp_path/'pkg'; (root/'configs').mkdir(parents=True)
    d={'package':{'version':'test'},'runtime':{'root':'runtime'},'paths':{}}
    p=root/'configs/tdoc.yaml'; p.write_text(yaml.safe_dump(d),encoding='utf-8')
    return TDOCConfig.from_yaml(p)


def test_reset_removes_runtime_reports_but_preserves_source(tmp_path):
    cfg=_cfg(tmp_path); root=cfg.package_root
    (root/'runtime/layer5/knowledge').mkdir(parents=True); (root/'runtime/layer5/knowledge/k.json.gz').write_bytes(b'x')
    (root/'reports/offline').mkdir(parents=True); (root/'reports/offline/a.csv').write_text('x')
    (root/'logs').mkdir(parents=True); (root/'logs/communication.jsonl').write_text('generated\n')
    (root/'src').mkdir(); (root/'src/keep.py').write_text('pass')
    (root/'region').mkdir(); (root/'region/region_v1.geojson').write_text('{}')
    r=reset_tdoc(cfg,include_external=False)
    assert r['ok']
    assert (root/'runtime').is_dir() and not any((root/'runtime').iterdir())
    assert (root/'reports/reset/RESET_REPORT.json').is_file()
    assert not (root/'logs').exists()
    assert (root/'src/keep.py').is_file()
    assert (root/'region/region_v1.geojson').is_file()


def test_reset_external_only_deletes_provably_generated_outputs(tmp_path,monkeypatch):
    cfg=_cfg(tmp_path); root=cfg.package_root
    b=tmp_path/'boulder'; s=tmp_path/'sarg'; c=tmp_path/'climate'
    maproot=b/'outputs/region_v1_map'; crp=maproot/'CRP'; sargroot=s/'generated/SARG'; forcing=c/'climate_dataset/openmeteo_24h.csv'
    crp.mkdir(parents=True); (maproot/'terrain_obstacles.tif').write_bytes(b'x'); (crp/'a.csv').write_text('x')
    sargroot.mkdir(parents=True); (sargroot/'sarg_records.csv').write_text('x')
    forcing.parent.mkdir(parents=True); forcing.write_text('x')
    (b/'source.py').parent.mkdir(parents=True,exist_ok=True); (b/'source.py').write_text('keep')
    for k,v in {'TDDT_BOULDER_ROOT':b,'TDDT_MAP_ROOT':maproot,'TDDT_CRP_ROOT':crp,'TDDT_SARG_PACKAGE_ROOT':s,'TDDT_SARG_ROOT':sargroot,'TDDT_CLIMATE_ROOT':c}.items(): monkeypatch.setenv(k,str(v))
    r=reset_tdoc(cfg,include_external=True)
    assert r['ok']; assert not maproot.exists(); assert not sargroot.exists(); assert not forcing.exists()
    assert (b/'source.py').is_file() and b.is_dir() and s.is_dir() and c.is_dir()


def test_reset_dry_run_changes_nothing(tmp_path):
    cfg=_cfg(tmp_path); root=cfg.package_root
    (root/'runtime').mkdir(); (root/'runtime/x').write_text('x')
    (root/'reports').mkdir(); (root/'reports/x').write_text('x')
    r=reset_tdoc(cfg,dry_run=True,include_external=False)
    assert r['ok']; assert (root/'runtime/x').exists(); assert (root/'reports/x').exists()
    assert not (root/'reports/reset/RESET_REPORT.json').exists()


def test_reset_external_removes_layer2_tep_all_climate_horizons_and_active_pointer(tmp_path,monkeypatch):
    cfg=_cfg(tmp_path)
    twins=tmp_path/'twins'; climate=tmp_path/'climate'
    for twin in ('MESA_NAV','WingMPET'):
        tep=twins/'resources/tddt_layer2'/twin/'tep'; tep.mkdir(parents=True); (tep/'generated.json').write_text('{}')
        (tep.parent/'keep_contract.json').write_text('{}')
    ds=climate/'climate_dataset'; ds.mkdir(parents=True)
    for h in (24,72,120):(ds/f'openmeteo_{h}h.csv').write_text('time\n2026-01-01T00:00:00')
    (ds/'tdoc_active_forcing.json').write_text('{"file":"openmeteo_72h.csv"}')
    (ds/'static_reference.csv').write_text('keep')
    monkeypatch.setenv('TDDT_TWINS_ROOT',str(twins)); monkeypatch.setenv('TDDT_CLIMATE_ROOT',str(climate))
    r=reset_tdoc(cfg,include_external=True)
    assert r['ok']
    for twin in ('MESA_NAV','WingMPET'):
        assert not (twins/'resources/tddt_layer2'/twin/'tep').exists()
        assert (twins/'resources/tddt_layer2'/twin/'keep_contract.json').is_file()
    assert not list(ds.glob('openmeteo_*h.csv'))
    assert not (ds/'tdoc_active_forcing.json').exists()
    assert (ds/'static_reference.csv').is_file()


def test_reset_refresh_external_is_opt_in_and_uses_prepare_pipeline(tmp_path,monkeypatch):
    cfg=_cfg(tmp_path)
    calls=[]; progress=[]
    def fake_stream(cmd,cwd,**kwargs):
        calls.append((cmd,cwd,kwargs));
        if kwargs.get('progress'): kwargs['progress']('[refresh] [08/23] Layer-2 official TEP build + current-runtime API compatibility ... PASS')
        return {'returncode':0,'stdout_tail':'prepared','timed_out':False,'elapsed_s':1.25}
    monkeypatch.setattr('tddt_tdoc.reset._run_streaming',fake_stream)
    r=reset_tdoc(cfg,include_external=True,refresh_external=True,refresh_options={'climate_hours':72,'crp_clusters':32,'refresh_timeout_s':600},progress=progress.append)
    assert r['ok'] and r['refresh']['ok']
    assert calls and '--climate-hours' in calls[0][0] and '72' in calls[0][0]
    assert '--crp-clusters' in calls[0][0]
    assert calls[0][2]['timeout_s']==600
    assert any('external purge complete' in x for x in progress)
    assert any('Layer-2 official TEP build' in x for x in progress)


def test_reset_refresh_recovers_interrupted_staging_before_new_transaction(tmp_path,monkeypatch):
    import hashlib
    cfg=_cfg(tmp_path); root=cfg.package_root
    twins=tmp_path/'twins'; target=twins/'resources/tddt_layer2/MESA_NAV/tep'
    monkeypatch.setenv('TDDT_TWINS_ROOT',str(twins))
    backup_root=root/'.refresh_external_transaction_backup'
    tag=hashlib.sha256(str(target.resolve()).encode('utf-8')).hexdigest()[:12]
    backup=backup_root/f'{tag}__{target.name}'
    backup.mkdir(parents=True); (backup/'tep.csv').write_text('recovered')
    seen={}
    def fake_stream(cmd,cwd,**kwargs):
        seen['staged_during_prepare']=backup.is_dir() and (backup/'tep.csv').read_text()=='recovered'
        return {'returncode':0,'stdout_tail':'ok','timed_out':False,'elapsed_s':0.1}
    monkeypatch.setattr('tddt_tdoc.reset._run_streaming',fake_stream)
    r=reset_tdoc(cfg,include_external=True,refresh_external=True,
                 refresh_options={'reuse_downloaded_datasets':True})
    assert r['ok'] and seen['staged_during_prepare'] is True
    assert any(x['status']=='RESTORED' and x['path']==str(target.resolve())
               for x in r['interrupted_refresh_recovery'])
    assert not backup_root.exists()


def test_reset_refresh_refuses_no_external(tmp_path):
    cfg=_cfg(tmp_path)
    r=reset_tdoc(cfg,include_external=False,refresh_external=True)
    assert not r['ok']
    assert r['refresh']['status']=='REFUSED'


def test_cli_rejects_refresh_with_no_external_before_reset(tmp_path,capsys):
    from tddt_tdoc.cli import main
    cfg=_cfg(tmp_path)
    rc=main(['--config',str(cfg.package_root/'configs/tdoc.yaml'),'reset','--refresh-external','--no-external'])
    assert rc==2
    assert 'cannot be combined' in capsys.readouterr().out


def test_reset_progress_lists_external_delete_targets(tmp_path,monkeypatch):
    cfg=_cfg(tmp_path); twins=tmp_path/'twins'
    tep=twins/'resources/tddt_layer2/MESA_NAV/tep'; tep.mkdir(parents=True); (tep/'tep.csv').write_text('x')
    monkeypatch.setenv('TDDT_TWINS_ROOT',str(twins))
    lines=[]; r=reset_tdoc(cfg,include_external=True,progress=lines.append)
    assert r['ok']
    assert any('dataset.tep.MESA_NAV' in x and 'deleting' in x for x in lines)
    assert any('dataset.tep.MESA_NAV' in x and 'DELETED' in x for x in lines)


def test_reset_retries_transient_enotempty(tmp_path,monkeypatch):
    import errno, shutil as _shutil
    cfg=_cfg(tmp_path); root=cfg.package_root
    (root/'runtime').mkdir(); (root/'runtime/x').write_text('x')
    real=_shutil.rmtree; calls={'n':0}
    def flaky(path,*args,**kwargs):
        if Path(path).resolve()==(root/'runtime').resolve() and calls['n']==0:
            calls['n']+=1
            raise OSError(errno.ENOTEMPTY,'Directory not empty')
        return real(path,*args,**kwargs)
    monkeypatch.setattr('tddt_tdoc.reset.shutil.rmtree',flaky)
    r=reset_tdoc(cfg,include_external=False)
    assert r['ok'] and calls['n']==1
    assert (root/'runtime').is_dir() and not any((root/'runtime').iterdir())


def test_reset_refresh_reuse_downloaded_datasets_passes_network_free_switch_and_allows_install(tmp_path,monkeypatch):
    cfg=_cfg(tmp_path); calls=[]
    monkeypatch.setattr('tddt_tdoc.reset._run_streaming',lambda cmd,cwd,**kw:(calls.append(cmd) or {'returncode':0,'stdout_tail':'ok','timed_out':False,'elapsed_s':0.1}))
    r=reset_tdoc(cfg,include_external=True,refresh_external=True,refresh_options={'reuse_downloaded_datasets':True,'no_download':True})
    assert r['ok'] and '--reuse-downloaded-datasets' in calls[0]
    assert '--no-install' not in calls[0]


def test_reset_preserves_integrated_internet_dataset_cache(tmp_path,monkeypatch):
    cfg=_cfg(tmp_path); cache=tmp_path/'internet_datasets'; (cache/'climate_twin').mkdir(parents=True); marker=cache/'climate_twin/openmeteo_24h.csv'; marker.write_text('cached')
    monkeypatch.setenv('TDDT_INTERNET_DATASETS_ROOT',str(cache))
    r=reset_tdoc(cfg,include_external=True)
    assert r['ok'] and marker.read_text()=='cached'


def test_reset_refresh_passes_fmu_build_and_cleanup_switches_before_prepare(tmp_path,monkeypatch):
    cfg=_cfg(tmp_path); calls=[]
    monkeypatch.setattr('tddt_tdoc.reset._run_streaming',lambda cmd,cwd,**kw:(calls.append(cmd) or {'returncode':0,'stdout_tail':'ok','timed_out':False,'elapsed_s':0.1}))
    r=reset_tdoc(cfg,include_external=True,refresh_external=True,refresh_options={'build_fmus':True,'build_fmus_refresh':True})
    assert r['ok'] and calls
    cmd=calls[0]
    assert '--build-fmus' in cmd and '--build-fmus-refresh' in cmd


def test_cli_rejects_build_fmus_without_refresh_external(tmp_path,capsys):
    from tddt_tdoc.cli import main
    cfg=_cfg(tmp_path)
    rc=main(['--config',str(cfg.package_root/'configs/tdoc.yaml'),'reset','--build-fmus'])
    assert rc==2
    assert 'require --refresh-external' in capsys.readouterr().out
