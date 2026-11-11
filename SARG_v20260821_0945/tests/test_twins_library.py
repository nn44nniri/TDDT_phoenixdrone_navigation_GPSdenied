from pathlib import Path
import textwrap
import pytest
from sarg_builder.twins_library import discover_assets, TwinsLibrary, TwinsLibraryError


def make_fake(tmp_path: Path):
    root = tmp_path / 'twins'
    package = root / 'python' / 'twins_fmi'
    package.mkdir(parents=True)
    (root / 'fmus').mkdir()
    (root / 'config').mkdir()
    (root / 'fmus' / 'WingMPET.fmu').write_bytes(b'x')
    (root / 'fmus' / 'MESA_NAV.fmu').write_bytes(b'x')
    (root / 'config' / 'phoenix_drone_v3.yaml').write_text(
        'model: {gravity_m_s2: 9.81}\n'
        'mass: {empty_or_flight_mass_kg: 0.65}\n'
        'geometry: {wing_span_m: 0.64, wing_area_m2: 0.12}\n'
        'aerodynamics: {cd0: 0.04, cl_max: 1.0, oswald_efficiency: 0.7}\n'
        'battery: {capacity_Ah: 2.2, initial_soc: 1.0}\n'
    )
    (package / 'native_fmu.py').write_text('class NativeFMU: pass\n')
    (package / 'drone_config.py').write_text(textwrap.dedent("""
        import yaml
        class DroneConfig:
            def __init__(self, path):
                self.data = yaml.safe_load(open(path))
                self.flat = {'mass.empty_or_flight_mass_kg': 0.65}
    """))
    (package / 'formula_fmi.py').write_text(textwrap.dedent("""
        class FormulaFMU:
            def __init__(self, path, config=None, **kwargs):
                self.path = path
                self.config = config
                self.operations = {1: {}}
            def evaluate_operation(self, op, use_config=True):
                return {'formalism_version': 1.0}
            def close(self, **kwargs):
                pass
    """))
    (package / 'library.py').write_text(textwrap.dedent("""
        from .formula_fmi import FormulaFMU
        class TwinsLayer2:
            def __init__(self, mesa_fmu, wingmpet_fmu, config=None, **kwargs):
                self.mesa = FormulaFMU(mesa_fmu, config=config)
                self.wingmpet = FormulaFMU(wingmpet_fmu, config=config)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
    """))
    (package / '__init__.py').write_text(textwrap.dedent("""
        from .drone_config import DroneConfig
        from .formula_fmi import FormulaFMU
        from .library import TwinsLayer2
    """))
    return root


def test_discovers_and_uses_public_twins_fmi_package(tmp_path):
    root = make_fake(tmp_path)
    assets = discover_assets(root)
    lib = TwinsLibrary(assets)
    assert lib.load_drone_config().flat['mass.empty_or_flight_mass_kg'] == 0.65
    probes = lib.probe_all()
    assert probes['WingMPET']['mode'] == 'twins_fmi.TwinsLayer2'
    assert probes['MESA_NAV']['configuration_attached'] is True
    assert lib.provenance()['integration'] == 'public_twins_fmi_package'


def test_rejects_legacy_flat_only_library(tmp_path):
    root = tmp_path / 'bad'
    (root / 'python').mkdir(parents=True)
    (root / 'python' / 'formula_fmi.py').write_text('')
    with pytest.raises(TwinsLibraryError):
        discover_assets(root)
