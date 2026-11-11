#!/usr/bin/env python3
"""CLI read-only Stage8 Climate forcing / plant time / wind evidence boundary."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'python'))
from climate_binding import ClimateBindingError, verify_climate_binding


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--forcing',required=True,type=Path)
    ap.add_argument('--fmu',type=Path,default=Path('fmu/MicroMetTwin.fmu'))
    ap.add_argument('--forcing-sha256',required=True)
    ap.add_argument('--fmu-sha256',required=True)
    ap.add_argument('--mission-epoch',required=True)
    ap.add_argument('--plant-time-s',required=True,type=float)
    ap.add_argument('--forecast-naive-is-utc',action='store_true',help='Explicit UTC policy for original naive timestamps')
    ap.add_argument('--model-wind-speed-ms',type=float)
    ap.add_argument('--model-wind-from-deg',type=float)
    ap.add_argument('--adapter-wind-speed-ms',type=float)
    ap.add_argument('--adapter-wind-from-deg',type=float)
    ap.add_argument('--native-csv',type=Path)
    ap.add_argument('--native-csv-sha256')
    ap.add_argument('--wind-tolerance-ms',type=float,default=0.1)
    ap.add_argument('--native-time-tolerance-s',type=float,default=0.05)
    ap.add_argument('--require-native',action='store_true')
    ap.add_argument('--output',type=Path,required=True)
    a=ap.parse_args()
    if a.output.exists():
        print(json.dumps({'status':'BLOCKED_REPORT_EXISTS','flight_authorized':False}));return 3
    try:
        res=verify_climate_binding(forcing_file=a.forcing,fmu_file=a.fmu,mission_epoch_utc=a.mission_epoch,
            plant_elapsed_s=a.plant_time_s,expected_forcing_sha256=a.forcing_sha256,
            expected_fmu_sha256=a.fmu_sha256,naive_forecast_is_utc=a.forecast_naive_is_utc,
            model_wind_speed_ms=a.model_wind_speed_ms,model_wind_from_deg=a.model_wind_from_deg,
            adapter_wind_speed_ms=a.adapter_wind_speed_ms,adapter_wind_from_deg=a.adapter_wind_from_deg,
            tolerance_ms=a.wind_tolerance_ms,native_readback_csv=a.native_csv,native_csv_sha256=a.native_csv_sha256,
            readback_tolerance_s=a.native_time_tolerance_s)
        code=0
        if a.require_native and not res['native_environment_confirmed']:
            res['status']='BLOCKED_NATIVE_CONFIRMATION_UNAVAILABLE';code=2
    except (ClimateBindingError,ValueError,TypeError,OSError,OverflowError) as e:
        res={'schema':'TDDT-Climate-ForcingBinding-v1','status':'BLOCKED_INVALID_CLIMATE_BINDING',
             'reason':str(e),'native_environment_confirmed':False,'flight_authorized':False,
             'native_controller_certified':False}
        code=2
    res={'test':'STAGE8_CLIMATE_BINDING',**res}
    # Read-only except explicitly requested new report; never overwrite a previous result.
    if not a.output.parent.is_dir():
        print(json.dumps({'status':'BLOCKED_OUTPUT_DIRECTORY_MISSING','flight_authorized':False}));return 3
    with a.output.open('x',encoding='utf-8') as stream:json.dump(res,stream,ensure_ascii=False,indent=2,allow_nan=False);stream.write('\n')
    print(json.dumps(res,ensure_ascii=False,indent=2,allow_nan=False))
    return code
if __name__=='__main__':raise SystemExit(main())
