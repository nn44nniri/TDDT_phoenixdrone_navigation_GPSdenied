"""Bound production command/adapter paths; no GUI, FCU, ARM or parameter writes."""
from copy import deepcopy
from datetime import datetime
import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest

from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.native_environment import prepare_binding
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter

TDOC=Path(__file__).resolve().parents[1];ROOT=TDOC.parent
NATIVE=ROOT/'layer6_v20261007_1032';ENV=NATIVE/'FlightStack/phoenix_environment_adapter_v2'
sys.path.insert(0,str(NATIVE/'src'));sys.path.insert(0,str(ENV))
from layer6.binding import forcing_time,environment_status
from phoenix_env_adapter.bound import BoundEnvironment,run_bound_runtime


@pytest.fixture(autouse=True)
def explicit_readonly_reference(isolated_mutable_knowledge_roots,monkeypatch):
    # Only the pure binding reader accesses this immutable reference; no Layer5
    # engine or KStore workspace is created in these tests.
    monkeypatch.setenv('TDDT_LAYER5_KSTORE_ROOT',str(TDOC/'runtime/layer5/kstore'))


@pytest.fixture(scope='module')
def bound(tmp_path_factory):
    p=tmp_path_factory.mktemp('r402')/'binding.json';c=TDOCConfig.from_yaml(TDOC/'configs/tdoc.yaml')
    resolve=c.path_value;c.path_value=lambda k,required=False:TDOC/'runtime/layer5/kstore' if k=='TDDT_LAYER5_KSTORE_ROOT' else resolve(k,required=required)
    b=prepare_binding(c,output_path=p)
    return p,b


def test_launcher_uses_exact_binding_and_surveyed_terrain_origin(bound,tmp_path,monkeypatch):
    import run_flightstack as launcher
    from layer6 import native_session as native
    monkeypatch.setattr(native,'RUNTIME',tmp_path)
    path,b=bound;folder,data=native.create(path)
    cmds=launcher._commands(NATIVE/'FlightStack',path,session_dir=folder)
    assert len(cmds)==4
    for index,cmd in enumerate(cmds,1):
        assert str(folder) in cmd and '-m layer6.native_session' in cmd
        assert '--terminal terminal'+str(index) in cmd
        assert data['session_id'] in cmd
    assert json.loads((folder/'binding.json').read_text())==b
    assert data['aircraft_sha256']==data['source_aircraft_sha256']
    text=(Path(native.__file__).parent/'native_session.py').read_text()
    assert "'terrain_elevation_msl_m'" in text and "'--home',home" in text
    assert 'phoenix_env_adapter.cli' in text and "'prepare'" in text and "'runtime'" in text
    assert not any(x in ''.join(cmds) for x in ('2025-05-20','--dataset-tar','--time ','examples/region','-L PhoenixBoulder'))


def test_launcher_missing_binding_rejects_before_resolving_or_launching(tmp_path,monkeypatch):
    from layer6 import native_session as native
    monkeypatch.setattr(native,'RUNTIME',tmp_path)
    with pytest.raises(FileNotFoundError):native.create(tmp_path/'missing.json')
    assert not (tmp_path/'sessions').exists()


def test_previous_binding_revision_is_explicitly_rejected(bound):
    from tddt_tdoc.native_binding import validate_binding
    old=deepcopy(bound[1]);old['revision']='native-unified-binding-r4.01-v1'
    with pytest.raises(ValueError,match='schema/revision'):validate_binding(old)


@pytest.mark.parametrize('seconds',[0.,.25,900.,3600.])
def test_dataset_clock_is_preserved_and_epoch_only_moves_by_plant_elapsed(bound,seconds):
    _,b=bound;t,elapsed=forcing_time(b,seconds)
    assert elapsed==seconds
    assert (datetime.fromisoformat(t)-datetime.fromisoformat(b['effective_climate_epoch'])).total_seconds()==seconds
    assert datetime.fromisoformat(t).tzinfo is None and not t.endswith('Z')
    assert b['time_contract']['timezone_status']=='unspecified'


@pytest.mark.parametrize('value',[-.01,float('nan'),float('inf'),True,86400.])
def test_bad_plant_query_never_clamps_rebases_or_repeats(bound,value):
    with pytest.raises(ValueError):forcing_time(bound[1],value)


def test_adapter_executes_real_micromet_on_bound_forcing_dem_and_epoch(bound):
    path,b=bound;p=BoundEnvironment(path);sample=p.sample(900.)
    assert sample['binding_id']==b['binding_id'] and sample['binding_hash']==b['binding_hash']
    assert sample['forcing_path']==b['forcing_file'] and sample['terrain_path']==b['origin']['terrain_file']['path']
    assert sample['simulation_time_dataset']=='2026-10-28T16:15:00'
    assert sample['timezone_status']=='unspecified' and not sample['UTC_mapping_verified']
    assert sample['model_outputs']['formula_valid']==1. and sample['model_outputs']['wind_3dw_valid']==1.
    assert not sample['ok'] and not sample['readback_verified'] and not sample['actuation_authority']
    assert not sample['legacy_dataset_used'] and not sample['navigation_truth_injection']


def test_micromet_ground_matches_tdoc_bilinear_and_does_not_clamp_aircraft_altitude(bound):
    from terrain_truth import TerrainTruth
    path,b=bound;home=b['mission_origin']
    with TerrainTruth(b['origin']['terrain_file']['path']) as terrain:
        x,y=terrain._xy(home['longitude_deg'],home['latitude_deg'])
        legacy_nearest=terrain._read_nearest(terrain.elevation_band,x,y)
        current=terrain.sample(home['longitude_deg'],home['latitude_deg']).ground_elevation_m
    assert legacy_nearest>home['altitude_msl_m']  # Reproduces the observed before-fix rejection.
    assert current==pytest.approx(home['terrain_elevation_msl_m'],abs=1e-6)
    result=BoundEnvironment(path).sample(0.)
    assert result['environment_query']['altitude_msl_m']==home['altitude_msl_m']
    assert result['environment_query']['altitude_agl_m']==pytest.approx(home['cg_height_agl_m'],abs=1e-6)


@pytest.mark.parametrize('extra',[['--time','2025-05-20T12:00:00Z'],['--dataset-tar','example.tar.gz'],['--region','examples/region.geojson'],['--cache','example_cache']])
def test_bound_cli_rejects_independent_example_inputs(bound,monkeypatch,tmp_path,extra):
    from phoenix_env_adapter import cli
    monkeypatch.setattr(sys,'argv',['phoenix-env','prepare','--binding',str(bound[0]),'--output',str(tmp_path),*extra])
    with pytest.raises(SystemExit) as exc:cli.main()
    assert exc.value.code==2 and not list(tmp_path.iterdir())


def test_dry_runtime_requires_explicit_query_and_does_not_invent_boot_time(bound,tmp_path):
    with pytest.raises(ValueError,match='explicit --plant-time-s'):
        run_bound_runtime(bound[0],tmp_path/'state.json',mavlink='unused',dry_run=True)
    assert not (tmp_path/'state.json').exists()


def test_environment_status_propagates_consumption_but_never_fakes_applied_readback(bound,tmp_path):
    path,b=bound;p=BoundEnvironment(path);state=p.sample(0.);dest=tmp_path/'state.json';p.publish(dest,state)
    status=environment_status(path,dest)
    assert status['binding_consumed'] and status['binding_id']==b['binding_id'] and not status['ok']
    bad=deepcopy(state);bad['binding_id']='other';dest.write_text(json.dumps(bad))
    assert not environment_status(path,dest)['binding_consumed']


def test_environment_output_cannot_overwrite_binding_or_forcing(bound):
    path,b=bound;p=BoundEnvironment(path);state=p.sample(0.)
    for dest in [path,Path(b['forcing_file'])]:
        raw=dest.read_bytes()
        with pytest.raises(ValueError,match='overwrite a bound input'):p.publish(dest,state)
        assert dest.read_bytes()==raw


def test_tdoc_preparation_keeps_one_binding_id_after_fcu_connection(monkeypatch,tmp_path):
    import tddt_tdoc.native_environment as env
    calls=[];a=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter)
    a.config=SimpleNamespace(get=lambda k,d=None:d,path_value=lambda *a,**kw:tmp_path)
    a._client=None;a._module=lambda:None
    from layer6 import native_session
    monkeypatch.setattr(native_session,'active_binding',lambda cfg:None)
    a._option=lambda k,d=None:True if k=='require_bound_environment' else False if k=='start_if_needed' else d
    a._fresh=lambda status:True
    def stop(status):
        assert a._environment_binding['binding_id']=='single-ID'
        raise RuntimeError('stop before execution')
    a._environment_ready=stop
    a._get_client=lambda:SimpleNamespace(ensure_mavlink=lambda **kw:None,wait_fcu_ready=lambda **kw:{'plant_time_s':25.},gui_status=lambda **kw:{})
    monkeypatch.setattr(env,'prepare_binding',lambda *args,**kw:calls.append(kw) or {'binding_id':'single-ID','plant_epoch_s':0.})
    result=a.prepare_online_transport_session()
    assert len(calls)==1 and 'plant_epoch_s' not in calls[0]
    assert not result['ok'] and result['stage']=='forcing_confirmation'


def test_live_loop_uses_mavlink_source_time_with_application_isolated(bound,tmp_path,monkeypatch):
    from phoenix_env_adapter import native_forcing
    # This R4.02 test isolates clock consumption. R4.03 tests the real parameter protocol.
    monkeypatch.setattr(native_forcing,'apply_wind',lambda *a,**kw:{'verified':False,'parameters':{}})
    def message(t,kind='ATTITUDE'):return SimpleNamespace(time_boot_ms=t,get_type=lambda:kind,get_srcSystem=lambda:1,get_srcComponent=lambda:1,
        lat=400100000,lon=-1053000000,alt=1798657)
    messages=[message(1000,'GLOBAL_POSITION_INT'),message(2000,'GLOBAL_POSITION_INT')]
    closed=[];master=SimpleNamespace(wait_heartbeat=lambda **kw:message(0),recv_match=lambda **kw:messages.pop(0),close=lambda:closed.append(True))
    fake=SimpleNamespace(mavutil=SimpleNamespace(mavlink_connection=lambda endpoint:master))
    monkeypatch.setitem(sys.modules,'pymavlink',fake)
    state=run_bound_runtime(bound[0],tmp_path/'state.json',mavlink='isolated-fake-link',update_hz=1e9,max_updates=2)
    assert state['plant_time_s']==2. and state['simulation_time_dataset']=='2026-10-28T16:00:02'
    assert state['plant_time_source']=='MAVLink/GLOBAL_POSITION_INT.time_boot_ms' and not state['ok'] and closed


def test_live_loop_has_no_wall_clock_fallback(bound,tmp_path,monkeypatch):
    heartbeat=SimpleNamespace(get_srcSystem=lambda:1,get_srcComponent=lambda:1)
    master=SimpleNamespace(wait_heartbeat=lambda **kw:heartbeat,recv_match=lambda **kw:None,close=lambda:None)
    monkeypatch.setitem(sys.modules,'pymavlink',SimpleNamespace(mavutil=SimpleNamespace(mavlink_connection=lambda endpoint:master)))
    with pytest.raises(RuntimeError,match='no wall-time substitution'):
        run_bound_runtime(bound[0],tmp_path/'state.json',mavlink='isolated-fake-link',max_updates=1)
    state=json.loads((tmp_path/'state.json').read_text())
    assert not state['ok'] and not state['readback_verified'] and state['application_error']


def test_real_below_ground_query_still_rejects_instead_of_adjusting_altitude(bound):
    p,b=bound
    with pytest.raises(ValueError,match='CLIMATE_ALTITUDE_BELOW_GROUND'):
        BoundEnvironment(p).sample(0.,altitude_msl_m=b['mission_origin']['terrain_elevation_msl_m']-.01)


def test_bilinear_height_rejects_weighted_nodata_but_keeps_exact_valid_pixel(tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from terrain_truth import TerrainTruth
    p=tmp_path/'ground.tif'
    with rasterio.open(p,'w',driver='GTiff',width=2,height=2,count=1,dtype='float64',
            crs='EPSG:32613',transform=from_origin(0,2,1,1),nodata=-9999) as dst:
        dst.write(np.array([[10.,20.],[30.,-9999.]]),1)
    with TerrainTruth(p) as terrain:
        assert terrain._read_bilinear_elevation(.5,1.5)==10.
        with pytest.raises(ValueError,match='NoData'):terrain._read_bilinear_elevation(1.,1.)


def test_fresh_extraction_overlay_is_idempotent_and_rejects_unknown_source(tmp_path):
    import importlib.util
    p=NATIVE/'scripts/apply_r4_02_environment_binding.py';spec=importlib.util.spec_from_file_location('overlay402',p)
    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
    target=tmp_path/'fresh';pkg=target/'phoenix_env_adapter';pkg.mkdir(parents=True)
    # Use the current standard installation assets. The CLI is already current;
    # bound.py/native_forcing.py are absent in a fresh extraction and must be
    # installed by the real helper. No historical review/report is a fixture.
    assets=NATIVE/'resources/r4_02_v1'
    (pkg/'cli.py').write_bytes((assets/'phoenix_env_adapter/cli.py').read_bytes())
    assert not (pkg/'bound.py').exists() and not (pkg/'native_forcing.py').exists()
    assert helper.apply(target)['status']=='PASS'
    first={p.name:p.read_bytes() for p in pkg.glob('*.py')}
    manifest=json.loads((assets/'OVERLAY_MANIFEST.json').read_text())
    assert first=={Path(name).name:(assets/name).read_bytes() for name in manifest['files']}
    assert helper.apply(target)['status']=='PASS' and first=={p.name:p.read_bytes() for p in pkg.glob('*.py')}
    (pkg/'cli.py').write_text('unknown local source')
    with pytest.raises(ValueError,match='unrecognized'):helper.apply(target)
    assert (pkg/'cli.py').read_text()=='unknown local source'
    assert all((pkg/name).read_bytes()==data for name,data in first.items() if name!='cli.py')


def test_bound_prepare_never_opens_legacy_dataset_cache(bound,tmp_path,monkeypatch,capsys):
    from phoenix_env_adapter import cli
    from phoenix_env_adapter.datasets import DatasetLayout
    monkeypatch.setattr(DatasetLayout,'from_tar',lambda *a,**kw:pytest.fail('legacy dataset path called'))
    monkeypatch.setattr(sys,'argv',['phoenix-env','prepare','--binding',str(bound[0]),'--output',str(tmp_path)])
    cli.main();state=json.loads(capsys.readouterr().out)
    assert state['binding_consumed'] and state['forcing_path']==bound[1]['forcing_file']


def test_actual_gui_status_method_exposes_same_consumption_identity(bound,tmp_path,monkeypatch):
    import ast,os
    p,b=bound;provider=BoundEnvironment(p);state=provider.sample(0.);dest=tmp_path/'state.json';provider.publish(dest,state)
    tree=ast.parse((NATIVE/'src/gui/phoenix_f310_gui.py').read_text())
    method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_bound_environment_status')
    ns={'os':os};exec(compile(ast.Module(body=[method],type_ignores=[]),'<actual GUI consumption method>','exec'),ns)
    monkeypatch.setenv('TDDT_NATIVE_BINDING_FILE',str(p));monkeypatch.setenv('TDDT_NATIVE_ENVIRONMENT_STATE',str(dest))
    result=ns['_bound_environment_status'](SimpleNamespace())
    assert result['binding_consumed'] and result['binding_id']==b['binding_id'] and not result['ok']
