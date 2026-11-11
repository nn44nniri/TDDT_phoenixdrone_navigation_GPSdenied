from pathlib import Path
from types import SimpleNamespace
import pytest
from layer4.dependency import check_dependencies, _EXPECTED

class _Config:
    def __init__(self, roots):
        self.roots=roots
    def env_path(self,key):
        return self.roots.get(key)


def _make_twins(tmp_path):
    path=tmp_path/'twins_layer2_v20260820_1233'
    for rel in _EXPECTED['TDDT_TWINS_ROOT'][0]:
        target=path/rel
        if '.' not in target.name: target.mkdir(parents=True,exist_ok=True)
        else:
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text('nonempty',encoding='utf8')
    return path


def _twins_report(path):
    d=check_dependencies(_Config({'TDDT_TWINS_ROOT':path}))
    return next(x for x in d['dependencies'] if x['name']=='TDDT_TWINS_ROOT')


def test_real_stage2_tep_tree_is_the_required_layout(tmp_path):
    path=_make_twins(tmp_path)
    assert not (path/'resources/tddt_layer2/tep').exists()
    d=_twins_report(path)
    assert d['ok'] and d['missing']==[]


@pytest.mark.parametrize('missing', [
    'resources/tddt_layer2/MESA_NAV/tep/tep.csv',
    'resources/tddt_layer2/MESA_NAV/tep/manifest.json',
    'resources/tddt_layer2/WingMPET/tep/tep.csv',
    'resources/tddt_layer2/WingMPET/tep/manifest.json',
])
def test_each_missing_native_tep_evidence_is_reported(tmp_path,missing):
    path=_make_twins(tmp_path)
    (path/missing).unlink()
    report=_twins_report(path)
    assert not report['ok']
    assert missing in report['missing']
