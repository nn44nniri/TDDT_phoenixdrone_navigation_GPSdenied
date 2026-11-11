"""Read-only Stage-8 Climate/FlightStack evidence and forcing contract.

An input CSV or self-reported MAVLink parameter ACK is NOT independent proof that
wind was applied inside JSBSim. This module never emits flight authorization.
"""
from __future__ import annotations

import csv
import hashlib
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "TDDT-Climate-ForcingBinding-v1"
NATIVE_PROPERTY_COLUMNS = {
    "north": "atmosphere/wind-north-fps",
    "east": "atmosphere/wind-east-fps",
    "down": "atmosphere/wind-down-fps",
}

class ClimateBindingError(ValueError):
    pass


def sha256_file(p: str | Path) -> str:
    h=hashlib.sha256()
    with Path(p).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1048576), b''):
            h.update(chunk)
    return h.hexdigest()


def _finite(v: Any, what: str) -> float:
    if isinstance(v, bool): raise ClimateBindingError(f'{what}:boolean_invalid')
    try: n=float(v)
    except (TypeError,ValueError) as e: raise ClimateBindingError(f'{what}:non_numeric') from e
    if not math.isfinite(n): raise ClimateBindingError(f'{what}:non_finite')
    return n


def parse_utc(value: str, what: str) -> datetime:
    if not isinstance(value,str) or not value.strip():
        raise ClimateBindingError(f'{what}:missing')
    try: t=datetime.fromisoformat(value.strip().replace('Z','+00:00'))
    except ValueError as e: raise ClimateBindingError(f'{what}:invalid_iso_time') from e
    # Forecast time rows in original Climate dataset omit their UTC designator.
    # The original file has no embedded timezone authority, so reject ambiguous
    # offsets at this STRICT boundary; accept naive only when caller opts in.
    if t.tzinfo is None:
        raise ClimateBindingError(f'{what}:timezone_required')
    return t.astimezone(timezone.utc)


def _forecast_utc(value: str, *, naive_is_utc: bool) -> datetime:
    try: t=datetime.fromisoformat(str(value).strip().replace('Z','+00:00'))
    except ValueError as e: raise ClimateBindingError('forcing:invalid_timestamp') from e
    if t.tzinfo is None:
        if not naive_is_utc: raise ClimateBindingError('forcing:naive_time_requires_explicit_utc_policy')
        t=t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def wind_from_to_ned(speed_ms: Any, from_deg: Any) -> tuple[float,float,float]:
    """Meteorological wind FROM bearing clockwise from North, NED wind TO.

    Matches MicroMet C++: u(East)=-V*sin(theta), v(North)=-V*cos(theta).
    """
    v=_finite(speed_ms,'wind_speed_ms')
    theta=_finite(from_deg,'wind_from_deg')
    if v < 0: raise ClimateBindingError('wind:negative_speed')
    rad=math.radians(theta % 360.)
    return (-v*math.cos(rad), -v*math.sin(rad), 0.)


def _point_range(forcing: Path, *, naive_is_utc: bool) -> dict[str,Any]:
    observed:dict[str,list[datetime]]={}
    seen=set(); rows=0
    required={'time','latitude','longitude','wind_speed_10m_ms','wind_direction_10m_deg'}
    with forcing.open(newline='',encoding='utf-8') as stream:
        reader=csv.DictReader(stream)
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ClimateBindingError('forcing:required_fields_missing')
        for row in reader:
            rows+=1
            if not row['time']:raise ClimateBindingError('forcing:missing_timestamp')
            ts=_forecast_utc(row['time'],naive_is_utc=naive_is_utc)
            lat=_finite(row['latitude'],'latitude');lon=_finite(row['longitude'],'longitude')
            if not (-90<=lat<=90 and -180<=lon<=180): raise ClimateBindingError('forcing:invalid_coordinates')
            speed=_finite(row['wind_speed_10m_ms'],'wind_speed_ms')
            _ = wind_from_to_ned(speed,row['wind_direction_10m_deg'])
            key=str(row.get('point') or f'{lat:.8f},{lon:.8f}')
            observed.setdefault(key,[]).append(ts)
            pair=(key,ts)
            if pair in seen:raise ClimateBindingError('forcing:duplicate_point_time')
            seen.add(pair)
    if not observed: raise ClimateBindingError('forcing:empty')
    lo=max(min(v) for v in observed.values())
    hi=min(max(v) for v in observed.values())
    if lo > hi: raise ClimateBindingError('forcing:no_shared_time_window')
    return {'common_start_utc':lo,'common_end_utc':hi,'point_count':len(observed),'row_count':rows,
            'per_point_min_samples':min(len(v) for v in observed.values())}


def verify_climate_binding(*, forcing_file: str|Path, fmu_file: str|Path,
                           mission_epoch_utc: str, plant_elapsed_s: Any,
                           expected_forcing_sha256: str, expected_fmu_sha256: str,
                           naive_forecast_is_utc: bool = False,
                           model_wind_speed_ms: Any = None, model_wind_from_deg: Any = None,
                           adapter_wind_speed_ms: Any = None, adapter_wind_from_deg: Any = None,
                           tolerance_ms: float = 0.1,
                           native_readback_csv: str|Path|None = None,
                           native_csv_sha256: str|None = None,
                           readback_tolerance_s: float = 0.05) -> dict[str,Any]:
    forcing=Path(forcing_file).resolve(strict=True)
    fmu=Path(fmu_file).resolve(strict=True)
    for name, val, path in (('forcing',expected_forcing_sha256,forcing),('fmu',expected_fmu_sha256,fmu)):
        if not isinstance(val,str) or len(val)!=64 or any(c not in '0123456789abcdef' for c in val):
            raise ClimateBindingError(f'{name}:expected_sha256_required')
        if sha256_file(path)!=val:raise ClimateBindingError(f'{name}:sha256_mismatch')
    epoch=parse_utc(mission_epoch_utc,'mission_epoch_utc')
    elapsed=_finite(plant_elapsed_s,'plant_elapsed_s')
    if elapsed < 0:raise ClimateBindingError('plant_elapsed_s:negative')
    t=epoch+timedelta(seconds=elapsed)
    cover=_point_range(forcing, naive_is_utc=naive_forecast_is_utc)
    if not (cover['common_start_utc']<=t<=cover['common_end_utc']):
        raise ClimateBindingError('mission_time_outside_shared_forecast_window')
    result={'schema':SCHEMA, 'status':'FORCING_EPOCH_BOUND_DIAGNOSTIC_ONLY',
            'forcing_sha256':sha256_file(forcing), 'fmu_sha256':sha256_file(fmu),
            'forcing_file':str(forcing),'fmu_file':str(fmu),'mission_epoch_utc':epoch.isoformat(),
            'plant_elapsed_s':elapsed,'mapped_forecast_time_utc':t.isoformat(),
            'common_start_utc':cover['common_start_utc'].isoformat(),
            'common_end_utc':cover['common_end_utc'].isoformat(),
            'point_count':cover['point_count'],'row_count':cover['row_count'],
            'naive_forecast_interpreted_utc':bool(naive_forecast_is_utc),
            'wind_frame':'NED','wind_bearing_convention':'METEOROLOGICAL_FROM_DEG_CLOCKWISE_FROM_NORTH',
            'wind_model_adapter_comparable':False,'native_property_readback_comparable':False,
            'native_environment_confirmed':False,'temperature_applied_in_jsbsim':False,
            'humidity_applied_in_jsbsim':False,'native_controller_certified':False,
            'flight_authorized':False,'evidence_scope':'FILE_HASH_AND_TIME_DIAGNOSTIC_ONLY'}
    if any(x is not None for x in (model_wind_speed_ms,model_wind_from_deg,adapter_wind_speed_ms,adapter_wind_from_deg)):
        if any(x is None for x in (model_wind_speed_ms,model_wind_from_deg,adapter_wind_speed_ms,adapter_wind_from_deg)):
            raise ClimateBindingError('wind:both_model_and_adapter_vector_required')
        model=wind_from_to_ned(model_wind_speed_ms,model_wind_from_deg)
        adapter=wind_from_to_ned(adapter_wind_speed_ms,adapter_wind_from_deg)
        tolerance=_finite(tolerance_ms,'wind_tolerance_ms')
        if tolerance<0: raise ClimateBindingError('wind_tolerance_ms:negative')
        diff=math.dist(model,adapter)
        result.update({'model_wind_ned_ms':model,'adapter_claim_wind_ned_ms':adapter,'wind_difference_ms':diff,
                       'wind_tolerance_ms':tolerance})
        if diff>tolerance:raise ClimateBindingError('model_adapter_wind_mismatch')
        result['wind_model_adapter_comparable']=True
    if native_readback_csv is not None:
        if not native_csv_sha256:raise ClimateBindingError('native_readback:independent_trace_sha256_required')
        native=Path(native_readback_csv).resolve(strict=True)
        if sha256_file(native)!=native_csv_sha256:raise ClimateBindingError('native_readback:sha256_mismatch')
        if not result['wind_model_adapter_comparable']:
            raise ClimateBindingError('native_readback:wind_reference_required')
        result.update(_compare_native_csv(native,tol=_finite(tolerance_ms,'wind_tolerance_ms'),
                                         time_tol=_finite(readback_tolerance_s,'native_time_tolerance_s'),
                                         elapsed=elapsed,reference=result['adapter_claim_wind_ned_ms']))
        result['native_csv_sha256']=sha256_file(native)
        result['evidence_scope']='CSV_PROPERTY_VALUES_UNAUTHENTICATED_DIAGNOSTIC_ONLY'
    return result


def _compare_native_csv(path:Path,*,tol:float,time_tol:float,elapsed:float,reference:tuple[float,float,float])->dict:
    if time_tol<0 or tol<0: raise ClimateBindingError('native_readback:negative_tolerance')
    with path.open(newline='',encoding='utf-8') as stream:
        rows=csv.DictReader(stream)
        fields=rows.fieldnames or []
        def find(suffix):
            matches=[h for h in fields if str(h).strip().strip('\"').split('/fdm/jsbsim/')[-1].endswith(suffix)]
            if len(matches)!=1:raise ClimateBindingError(f'native_readback:missing_or_ambiguous_column:{suffix}')
            return matches[0]
        tcol=find('simulation/sim-time-sec')
        wcols=[find(v) for v in NATIVE_PROPERTY_COLUMNS.values()]
        best=None
        for row in rows:
            ts=_finite(row[tcol],'native_time')
            d=abs(ts-elapsed)
            if best is None or d<best[0]:best=(d,row,ts)
        if best is None or best[0]>time_tol:raise ClimateBindingError('native_readback:no_time_matched_sample')
        readback=tuple(_finite(best[1][col],col)*0.3048 for col in wcols)
        discrepancy=math.dist(readback,reference)
        if discrepancy>tol:raise ClimateBindingError('native_readback:wind_vector_mismatch')
        return {'native_property_readback_comparable':True,'native_readback_plant_time_s':best[2],
                'native_readback_time_difference_s':best[0],
                'native_readback_wind_ned_ms':readback,'native_wind_difference_ms':discrepancy,
                'native_environment_confirmed':False,
                'native_readback_note':'CSV header/value match alone cannot authenticate independent JSBSim capture'}
