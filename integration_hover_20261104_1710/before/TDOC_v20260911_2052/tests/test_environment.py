from pathlib import Path


def test_environment_declares_all_authoritative_roots():
    t=(Path(__file__).parents[1]/'configs/environment.sh').read_text()
    for key in ('TDDT_INTEGRATED_ROOT','TDDT_TWINS_ROOT','TDDT_CLIMATE_ROOT',
        'TDDT_UNIT_ADAPTER_ROOT','TDDT_BOULDER_ROOT','TDDT_SARG_PACKAGE_ROOT',
        'TDDT_SARG_ROOT','TDDT_MAP_ROOT','TDDT_CRP_ROOT','TDDT_LAYER3_ROOT',
        'TDDT_LAYER4_ROOT','TDDT_LAYER5_ROOT','TDDT_LAYER6_ROOT','TDDT_LAYER7_ROOT',
        'TDDT_ORCHESTRATOR_ROOT','TDDT_INTERNET_DATASETS_ROOT','TDDT_REGION_FILE'):
        assert f'export {key}=' in t
    assert 'unset TDDT_PTLIB_ROOT EXT_FMU_SIM_ROOT' in t



def test_environment_is_single_authority_and_overrides_stale_shell_values(tmp_path):
    import os, subprocess
    root=Path(__file__).parents[1]
    env=dict(os.environ)
    env.update({'TDDT_INTEGRATED_ROOT':'/tmp/stale','TDDT_LAYER5_ROOT':'/tmp/stale',
                'TDDT_PTLIB_ROOT':'/tmp/retired','EXT_FMU_SIM_ROOT':'/tmp/retired',
                'TDDT_LAYER5_KSTORE_ROOT':'/tmp/stale-kstore'})
    cmd=f'source "{root}/configs/environment.sh"; printf "%s\n" "$TDDT_INTEGRATED_ROOT" "$TDDT_LAYER5_ROOT" "${{TDDT_PTLIB_ROOT-unset}}" "${{EXT_FMU_SIM_ROOT-unset}}" "$TDDT_LAYER6_ROOT" "$TDDT_LAYER5_KSTORE_ROOT"'
    out=subprocess.check_output(['bash','-c',cmd],env=env,text=True).splitlines()
    assert out==[str(root.parent),str(root.parent/'layer5_v20260911_1510'),
                 'unset','unset',str(root.parent/'layer6_v20261007_1032'),str(root/'runtime/layer5/kstore')]



def test_yaml_paths_are_never_fallback_authority(tmp_path, monkeypatch):
    from tddt_tdoc.config import TDOCConfig
    p=tmp_path/'tdoc.yaml'
    p.write_text('paths:\n  TDDT_LAYER4_ROOT: /tmp/from-yaml\n',encoding='utf-8')
    monkeypatch.delenv('TDDT_LAYER4_ROOT',raising=False)
    cfg=TDOCConfig.from_yaml(p)
    assert cfg.path_value('TDDT_LAYER4_ROOT') is None
    monkeypatch.setenv('TDDT_LAYER4_ROOT','/tmp/from-env')
    assert str(cfg.path_value('TDDT_LAYER4_ROOT'))=='/tmp/from-env'


def test_required_path_without_environment_export_fails(tmp_path, monkeypatch):
    from tddt_tdoc.config import TDOCConfig
    p=tmp_path/'tdoc.yaml'; p.write_text('{}',encoding='utf-8')
    monkeypatch.delenv('TDDT_LAYER4_ROOT',raising=False)
    cfg=TDOCConfig.from_yaml(p)
    try:
        cfg.path_value('TDDT_LAYER4_ROOT',required=True)
    except FileNotFoundError as exc:
        assert 'source configs/environment.sh' in str(exc)
    else:
        raise AssertionError('required environment root must fail when environment.sh was not sourced')


def test_dependency_checker_reports_missing_root():
    from tddt_tdoc.dependency import DependencyChecker
    class Cfg:
        def path_value(self, key):
            return None
    report=DependencyChecker(Cfg()).check()
    assert report["dependencies"]["TDDT_UNIT_ADAPTER_ROOT"]["missing"] == ["root"]
    assert report["required_ok"] is False


def test_native_gui_flightstack_is_authoritative_and_retired_fmu_unset():
    t=(Path(__file__).parents[1]/'configs/environment.sh').read_text()
    assert 'layer6_v20261007_1032' in t
    assert 'unset TDDT_PTLIB_ROOT EXT_FMU_SIM_ROOT' in t
    assert 'export TDDT_LAYER6_ROOT=' in t
    assert 'export TDDT_LAYER7_ROOT=' in t
    assert 'pt_lib_v20260914_1308' not in t



def test_integrated_layer3_paths_are_rebound_only_from_tdoc_environment(monkeypatch):
    import os
    from types import SimpleNamespace
    from tddt_tdoc.config import TDOCConfig
    root=Path(__file__).parents[1]
    cfg=TDOCConfig.from_yaml(root/'configs/tdoc.yaml')
    fake=SimpleNamespace(paths=SimpleNamespace(
        twins_root='../fallback/twins', climate_root='../fallback/climate',
        tdoc_root='../fallback/unit', sarg_package_root='../fallback/sarg-package',
        sarg_root='../fallback/sarg', boulder_root='../fallback/boulder',
        map_root='../fallback/map', crp_root='../fallback/crp'))
    bound=cfg.bind_layer3_external_paths(fake,required=True)
    expected={
        'twins_root':'TDDT_TWINS_ROOT','climate_root':'TDDT_CLIMATE_ROOT',
        'tdoc_root':'TDDT_UNIT_ADAPTER_ROOT','sarg_package_root':'TDDT_SARG_PACKAGE_ROOT',
        'sarg_root':'TDDT_SARG_ROOT','boulder_root':'TDDT_BOULDER_ROOT',
        'map_root':'TDDT_MAP_ROOT','crp_root':'TDDT_CRP_ROOT',
    }
    for attr,key in expected.items():
        assert getattr(fake.paths,attr)==str(Path(os.environ[key]).resolve())
        assert bound[attr]==str(Path(os.environ[key]).resolve())
    monkeypatch.delenv('TDDT_CLIMATE_ROOT',raising=False)
    try:
        cfg.bind_layer3_external_paths(fake,required=True)
    except FileNotFoundError as exc:
        assert 'source configs/environment.sh' in str(exc)
    else:
        raise AssertionError('Layer3 integrated dependency must not fall back to its own YAML path')
