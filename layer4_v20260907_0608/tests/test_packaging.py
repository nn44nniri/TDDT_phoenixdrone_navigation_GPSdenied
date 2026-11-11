from pathlib import Path
import json
from layer4.dependency import check_dependencies


def test_no_vendored_external_archives(root):
    bad=[]
    for p in root.rglob('*'):
        if p.is_file() and (p.suffix.lower() in {'.fmu','.tif','.tiff'} or p.name.endswith('.tar.gz')): bad.append(p)
    assert not bad

def test_environment_uses_external_roots(root):
    s=(root/'configs/environment.sh').read_text();
    for key in ['TDDT_TWINS_ROOT','TDDT_CLIMATE_ROOT','TDDT_TDOC_ROOT','TDDT_SARG_PACKAGE_ROOT','TDDT_BOULDER_ROOT','TDDT_LAYER3_ROOT']: assert key in s

def test_formalism_is_shipped(root): assert (root/'docs/Formalism_layer4.md').exists()
def test_reference_model_marked_nonproduction(root):
    d=json.loads((root/'resources/edge_model/qlpv_model.json').read_text()); assert 'replace/fit offline' in d['calibration_status']
def test_no_actuator_command_api(root):
    text='\n'.join(p.read_text(errors='ignore') for p in (root/'src/layer4').glob('*.py'))
    assert 'actuator_command=True' not in text and 'network_commit=True' not in text


def test_packaging_policy_declares_clean_distribution(root):
    rep=json.loads((root/'TEST_REPORT.json').read_text())
    pol=rep.get('packaging_policy',{})
    assert pol.get('runtime_artifacts_shipped') is False
    assert pol.get('python_cache_shipped') is False
    assert pol.get('build_artifacts_shipped') is False
