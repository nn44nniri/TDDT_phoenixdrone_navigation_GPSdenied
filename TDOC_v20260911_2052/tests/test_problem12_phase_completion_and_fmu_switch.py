from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path
import inspect

ROOT=Path(__file__).resolve().parents[1]

spec=spec_from_file_location('wf_p12', ROOT/'scripts'/'run_training_workflows.py')
wf=module_from_spec(spec); spec.loader.exec_module(wf)

pspec=spec_from_file_location('prep_p12', ROOT/'scripts'/'prepare_all.py')
prep=module_from_spec(pspec); pspec.loader.exec_module(prep)


def _row(phase, ok=False, *, agl=10.0, goal=100.0):
    return {
        'phase':phase,
        'l4_candidate_verified':bool(ok),
        'l5_final_gate':bool(ok),
        'layer2_control_effect_applied':bool(ok),
        'agl_m':float(agl),
        'goal_distance_m':float(goal),
    }


def test_all_phase_labels_seen_does_not_mean_strict_completion_when_hold_exists():
    rows=[_row('on_ground',False,agl=0.0,goal=1000.0)]
    for p in wf.CONTROL_REQUIRED_PHASES:
        rows.append(_row(p,p in {'transition_forward','climb','cruise'}))
    rows.append(_row('touchdown',False,agl=0.0,goal=0.0))
    out=wf._mission_phase_completion(rows)
    assert out['all_required_phases_seen'] is True
    assert out['all_required_control_phases'] is False
    assert out['all_required_phases'] is False
    assert out['phase_completion']['on_ground']['completed'] is True
    assert out['phase_completion']['touchdown']['completed'] is True
    assert out['phase_completion']['hover']['completed'] is False


def test_strict_11phase_completion_requires_joint_control_proof_for_every_controlled_phase():
    rows=[_row('on_ground',False,agl=0.0,goal=1000.0)]
    rows += [_row(p,True) for p in wf.CONTROL_REQUIRED_PHASES]
    rows.append(_row('touchdown',False,agl=0.0,goal=0.0))
    out=wf._mission_phase_completion(rows)
    assert out['all_required_phases_seen'] is True
    assert out['all_required_control_phases'] is True
    assert out['all_required_phases'] is True


def test_touchdown_label_without_terminal_boundary_evidence_is_not_complete():
    rows=[_row('on_ground',False,agl=0.0,goal=1000.0)]
    rows += [_row(p,True) for p in wf.CONTROL_REQUIRED_PHASES]
    rows.append(_row('touchdown',False,agl=5.0,goal=80.0))
    out=wf._mission_phase_completion(rows)
    assert out['all_required_phases_seen'] is True
    assert out['all_required_control_phases'] is True
    assert out['phase_completion']['touchdown']['completed'] is False
    assert out['all_required_phases'] is False


def test_native_refresh_removes_only_twin_climate_build_caches(tmp_path):
    twins=tmp_path/'twins';climate=tmp_path/'climate';native=tmp_path/'native'
    for d in (twins,climate,native): d.mkdir()
    for d in (twins,climate,native):
        (d/'build').mkdir();(d/'build/junk').write_text('temporary')
        (d/'src').mkdir();(d/'src/keep.cpp').write_text('source')
    for rel in ('fmus/MESA_NAV.fmu','fmus/WingMPET.fmu'):
        p=twins/rel;p.parent.mkdir(exist_ok=True);p.write_bytes(b'fmubinary')
    fmu=climate/'fmu/MicroMetTwin.fmu';fmu.parent.mkdir();fmu.write_bytes(b'climatefmu')
    native_src=native/'src/keep.cpp'
    report={'stages':[],'_total':1,'ok':True}
    rec=prep.cleanup_fmu_build_caches(report,twins,climate)
    assert rec['ok'] is True
    assert len(rec['preserved_final_fmus'])==3
    assert not (twins/'build').exists() and not (climate/'build').exists()
    assert (native/'build/junk').read_text()=='temporary'
    assert native_src.read_text()=='source'
    assert (twins/'src/keep.cpp').read_text()=='source'
    assert (climate/'src/keep.cpp').read_text()=='source'
    assert all(p.is_file() for p in (twins/'fmus/MESA_NAV.fmu',twins/'fmus/WingMPET.fmu',fmu))



def test_prepare_pipeline_builds_twin_fmus_before_internet_and_never_exec_fmus():
    src=inspect.getsource(prep.main)
    before='Build/validate Navigation + Flight Simulator Twins'
    after='Acquire/reuse online terrain/OSM/climate/magnetic data required by CRP'
    climate='Download current Climate Twin forcing data to central internet dataset cache'
    assert src.index(before)<src.index(after)
    assert src.index(before)<src.index(climate)
    assert src.index("source_package_ready(report,'Prepare Layer6 client package'")<src.index(after)
    assert 'build_execution_fmus' not in src
    assert 'Prepare Layer6 client package' in src

