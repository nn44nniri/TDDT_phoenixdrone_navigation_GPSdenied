from __future__ import annotations
import csv,hashlib,math,subprocess,sys,json
from datetime import datetime,timezone
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'python'))
from climate_binding import wind_from_to_ned, verify_climate_binding,ClimateBindingError,sha256_file


def make_env(tmp_path):
    f=tmp_path/'forcing.csv'
    with f.open('w',newline='') as fp:
        writer=csv.DictWriter(fp,fieldnames=['point','time','latitude','longitude','wind_speed_10m_ms','wind_direction_10m_deg'])
        writer.writeheader()
        for point,lat in (('alpha',40.),('beta',41.)):
            for hour in (12,13,14):
                writer.writerow({'point':point,'time':f'2026-10-09T{hour:02d}:00:00','latitude':lat,'longitude':-105,
                                 'wind_speed_10m_ms':2.5,'wind_direction_10m_deg':270})
    model=tmp_path/'model.fmu';model.write_bytes(b'MICROMET_TEST_ONLY_FAKE_FMU_NOT_OPERATIONAL')
    kwargs={'forcing_file':f,'fmu_file':model,'mission_epoch_utc':'2026-10-09T12:00:00Z',
            'plant_elapsed_s':3600,'expected_forcing_sha256':sha256_file(f),'expected_fmu_sha256':sha256_file(model),
            'naive_forecast_is_utc':True}
    return kwargs


def test_binding_is_provenance_bound_not_flight_ready(tmp_path):
    o=verify_climate_binding(**make_env(tmp_path))
    assert o['mapped_forecast_time_utc']=='2026-10-09T13:00:00+00:00'
    assert o['point_count']==2 and o['native_environment_confirmed'] is False
    assert o['flight_authorized'] is False and o['temperature_applied_in_jsbsim'] is False


def test_wind_meteorological_from_matches_micromet_cpp():
    assert wind_from_to_ned(4,0)==pytest.approx((-4,0,0))
    assert wind_from_to_ned(4,90)==pytest.approx((0,-4,0))
    assert wind_from_to_ned(4,180)==pytest.approx((4,0,0))
    assert wind_from_to_ned(4,270)==pytest.approx((0,4,0))
    with pytest.raises(ClimateBindingError,match='negative_speed'):wind_from_to_ned(-1,0)


def test_model_vs_adapter_is_not_jsbsim_feedback(tmp_path):
    kwargs=make_env(tmp_path)
    kwargs.update(model_wind_speed_ms=4,model_wind_from_deg=270,adapter_wind_speed_ms=4,adapter_wind_from_deg=270)
    res=verify_climate_binding(**kwargs)
    assert res['wind_model_adapter_comparable'] and not res['native_property_readback_comparable']
    assert not res['native_environment_confirmed']


def test_bad_adapter_direction_blocked(tmp_path):
    kwargs=make_env(tmp_path)
    kwargs.update(model_wind_speed_ms=4,model_wind_from_deg=270,adapter_wind_speed_ms=4,adapter_wind_from_deg=90)
    with pytest.raises(ClimateBindingError,match='model_adapter_wind_mismatch'):verify_climate_binding(**kwargs)


def test_hash_mutation_or_misbound_fmu_fails(tmp_path):
    kw=make_env(tmp_path)
    kw['forcing_file'].write_text(kw['forcing_file'].read_text()+'\n')
    with pytest.raises(ClimateBindingError,match='forcing:sha256_mismatch'):verify_climate_binding(**kw)
    kw=make_env(tmp_path);kw['fmu_file'].write_text('different')
    with pytest.raises(ClimateBindingError,match='fmu:sha256_mismatch'):verify_climate_binding(**kw)


@pytest.mark.parametrize('value,reason', [(-1,'negative'),(float('nan'),'non_finite'),(999999,'outside_shared')])
def test_invalid_plant_clock_fail_closed(tmp_path,value,reason):
    kw=make_env(tmp_path);kw['plant_elapsed_s']=value
    with pytest.raises(ClimateBindingError,match=reason):verify_climate_binding(**kw)


def test_missing_timezone_for_epoch_or_csv(tmp_path):
    kw=make_env(tmp_path);kw['mission_epoch_utc']='2026-10-09T12:00:00'
    with pytest.raises(ClimateBindingError,match='timezone_required'):verify_climate_binding(**kw)
    kw=make_env(tmp_path);kw['naive_forecast_is_utc']=False
    with pytest.raises(ClimateBindingError,match='naive_time_requires_explicit_utc_policy'):verify_climate_binding(**kw)


def test_bad_point_only_window_fails_closed(tmp_path):
    kw=make_env(tmp_path)
    p=kw['forcing_file'];lines=p.read_text().splitlines()
    # Move every timestamp for beta to future, with a zero-overlap window.
    header=lines[0]
    shifted=[]
    for line in lines[1:]:
        if line.startswith('beta,'):
            for old,new in [('T12:00:00','T18:00:00'),('T13:00:00','T19:00:00'),('T14:00:00','T20:00:00')]:
                line=line.replace(old,new)
        shifted.append(line)
    lines=[header]+shifted
    p.write_text('\n'.join(lines)+'\n');kw['expected_forcing_sha256']=sha256_file(p)
    with pytest.raises(ClimateBindingError,match='no_shared_time_window'):verify_climate_binding(**kw)


def native_csv(tmp_path, north_ms=0,east_ms=4, down_ms=0, t=3600):
    p=tmp_path/'native.csv'
    with p.open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['/fdm/jsbsim/simulation/sim-time-sec',
            '/fdm/jsbsim/atmosphere/wind-north-fps','/fdm/jsbsim/atmosphere/wind-east-fps',
            '/fdm/jsbsim/atmosphere/wind-down-fps'])
        w.writeheader();w.writerow({'/fdm/jsbsim/simulation/sim-time-sec':t,
            '/fdm/jsbsim/atmosphere/wind-north-fps':north_ms/0.3048,
            '/fdm/jsbsim/atmosphere/wind-east-fps':east_ms/0.3048,
            '/fdm/jsbsim/atmosphere/wind-down-fps':down_ms/0.3048})
    return p


def test_native_property_csv_comparison_still_uncertified(tmp_path):
    kw=make_env(tmp_path)
    kw.update(model_wind_speed_ms=4,model_wind_from_deg=270,adapter_wind_speed_ms=4,adapter_wind_from_deg=270)
    csvpath=native_csv(tmp_path);kw.update(native_readback_csv=csvpath,native_csv_sha256=sha256_file(csvpath))
    out=verify_climate_binding(**kw)
    assert out['native_property_readback_comparable'] is True
    assert out['native_environment_confirmed'] is False
    assert out['evidence_scope']=='CSV_PROPERTY_VALUES_UNAUTHENTICATED_DIAGNOSTIC_ONLY'


def test_native_time_or_value_or_sha_mismatch_rejected(tmp_path):
    for mode in ('time','wind','sha'):
        kw=make_env(tmp_path);kw.update(model_wind_speed_ms=4,model_wind_from_deg=270,
            adapter_wind_speed_ms=4,adapter_wind_from_deg=270)
        p=native_csv(tmp_path,east_ms=5 if mode=='wind' else 4,t=3599 if mode=='time' else 3600)
        kw['native_readback_csv']=p;kw['native_csv_sha256']=('0'*64 if mode=='sha' else sha256_file(p))
        with pytest.raises(ClimateBindingError,match={'time':'no_time_matched_sample','wind':'wind_vector_mismatch','sha':'sha256_mismatch'}[mode]):
            verify_climate_binding(**kw)


def test_readonly_cli_new_report_and_strict_native_fail_closed(tmp_path):
    kw=make_env(tmp_path); cli=Path(__file__).resolve().parents[1]/'scripts/validate_stage8_binding.py'
    args=[sys.executable,str(cli),'--forcing',str(kw['forcing_file']),'--fmu',str(kw['fmu_file']),
          '--forcing-sha256',kw['expected_forcing_sha256'],'--fmu-sha256',kw['expected_fmu_sha256'],
          '--mission-epoch','2026-10-09T12:00:00Z','--plant-time-s','3600','--forecast-naive-is-utc',
          '--require-native','--output',str(tmp_path/'diag.json')]
    before=sha256_file(kw['forcing_file'])
    result=subprocess.run(args,capture_output=True,text=True)
    assert result.returncode==2,result.stderr
    out=json.loads((tmp_path/'diag.json').read_text())
    assert out['status']=='BLOCKED_NATIVE_CONFIRMATION_UNAVAILABLE'
    assert sha256_file(kw['forcing_file'])==before
    assert subprocess.run(args,capture_output=True,text=True).returncode==3
