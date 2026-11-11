from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_no_embedded_dependencies_and_config_renamed():
    assert not (ROOT/'vendor').exists()
    assert (ROOT/'configs/configuration_boulder.yaml').is_file()
    assert not (ROOT/'configs/example_boulder.yaml').exists()

def test_config_names_all_five_exact_external_versions():
    text=(ROOT/'configs/configuration_boulder.yaml').read_text(encoding='utf-8')
    for name in ('twins_layer2_v20260820_1233','climate_twin_v20260911_0922','TDOC_ISO_Universal_Unit_Conversion_Standard20260824_0914','SARG_v20260821_0945','boulder_multilayer_map_builder_v20260814_1215'):
        assert name in text

def test_environment_script_exists_and_uses_exact_deployment_paths():
    env = ROOT/'configs/environment.sh'
    assert env.is_file()
    text = env.read_text(encoding='utf-8')
    expected = {
        'TDDT_TWINS_ROOT': '/media/p1/DigitalTwin/TDDT_navigation/layer2/navigation_simulator/code/TDDT_Dynamic_NTwin/twins_layer2_v20260820_1233',
        'TDDT_CLIMATE_ROOT': '/media/p1/DigitalTwin/TDDT_navigation/layer2/climate/code/TDDT_Dynamic_NTwin/climate_twin_v20260911_0922',
        'TDDT_TDOC_ROOT': '/media/p1/DigitalTwin/TDDT_navigation/adapter/code/TDOC_ISO_Universal_Unit_Conversion_Standard20260824_0914',
        'TDDT_BOULDER_ROOT': '/media/p1/DigitalTwin/TDDT_navigation/datasets/CRP_map/code/boulder_multilayer_map_builder_v20260814_1215',
        'TDDT_SARG_PACKAGE_ROOT': '/media/p1/DigitalTwin/TDDT_navigation/datasets/SARG/code/SARG_v20260821_0945',
    }
    for key, value in expected.items():
        assert f'export {key}="{value}"' in text
    assert 'export TDDT_MAP_ROOT="${TDDT_BOULDER_ROOT}/outputs/region_v1_map"' in text
    assert 'export TDDT_CRP_ROOT="${TDDT_MAP_ROOT}/CRP"' in text
    assert 'export TDDT_SARG_ROOT="${TDDT_SARG_PACKAGE_ROOT}/generated/SARG"' in text


def test_yaml_documents_env_default_syntax():
    text=(ROOT/'configs/configuration_boulder.yaml').read_text(encoding='utf-8')
    assert 'twins_root: "${TDDT_TWINS_ROOT:-../dependencies/twins_layer2_v20260820_1233}"' in text
    assert 'source configs/environment.sh' in text

def test_boulder_import_dependency_is_declared_by_layer3():
    pyproject = (ROOT/'pyproject.toml').read_text(encoding='utf-8')
    requirements = (ROOT/'requirements.txt').read_text(encoding='utf-8')
    assert 'shapely>=2.0' in pyproject
    assert 'shapely>=2.0' in requirements
    assert 'plotly>=6.0' in pyproject
    assert 'plotly>=6.0' in requirements


def test_run_demo_executes_preflight_first():
    text = (ROOT/'run_demo.sh').read_text(encoding='utf-8')
    assert 'layer3.cli preflight' in text
    assert text.index('layer3.cli preflight') < text.index('layer3.cli demo')

def test_release_identity_matches_directory_name():
    import re, tomllib
    m=re.fullmatch(r'layer3_v(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})',ROOT.name)
    assert m, ROOT.name
    y,mo,d,h,mi=m.groups(); expected=f'{int(y)}.{int(mo)}.{int(d)}.{h}{mi}'
    pyproject=tomllib.loads((ROOT/'pyproject.toml').read_text(encoding='utf-8'))
    assert pyproject['project']['version']==expected
    from layer3 import __version__
    assert __version__==expected
    for rel in ('README.md','HOW_USAGE.md','docs/CONFORMANCE_REPORT.md'):
        assert ROOT.name in (ROOT/rel).read_text(encoding='utf-8')


def test_release_tree_has_no_embedded_vendor_or_egg_info():
    # Python/pytest create __pycache__ while the suite itself is running, so cache
    # cleanliness is verified on the final tarball rather than inside pytest.
    forbidden=[]
    for p in ROOT.rglob('*'):
        rel=p.relative_to(ROOT)
        if 'vendor' in rel.parts:
            forbidden.append(str(rel))
        if any(part.endswith('.egg-info') for part in rel.parts):
            forbidden.append(str(rel))
    assert not forbidden, forbidden
