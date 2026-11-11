#!/usr/bin/env python3
"""Offline paired WingMPET/FlightStack trace comparison; zero actuation.

Never changes FCU/JSBSim.  Requires recorded, timestamped actuator and motor
state and paired native observation, not pilot-domain commands or GPS navigation.
"""
import argparse
import json
import math
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'python'))
from twins_fmi import FormulaFMU
from twins_fmi.native_actuator_binding import ActuatorReplaySample, NativeReplayRejected, replay_native_actuator
from twins_fmi.stage2_native_event_assembler import check_evidence


def verify(lines, fmu, velocity_tol, omega_tol, min_pairs, *, require_source_evidence=False):
    if not isinstance(min_pairs,int) or min_pairs<1:
        raise NativeReplayRejected('min-pairs must be a positive integer')
    comparisons=[]
    last_start=-math.inf
    used_transactions=set()
    for n, row in enumerate(lines,1):
        if require_source_evidence:
            ev = row.get('source_evidence')
            if not isinstance(ev, dict) or ev.get('pair_provenance')!='INDEPENDENT_NATIVE_EXPORT_NOT_CONTROLLER_CERTIFICATION':
                raise NativeReplayRejected('independent native source evidence missing; model-generated fixture is not site data')
            check_evidence(ev.get('sample'), 'sample')
            check_evidence(ev.get('observation'), 'observation')
        sample = ActuatorReplaySample.from_mapping(row['sample'])
        u = sample.validated()
        start=float(sample.plant_time_s)
        if start <= last_start:
            raise NativeReplayRejected('plant time not strictly increasing')
        last_start=start
        if sample.transaction_id in used_transactions:
            raise NativeReplayRejected('duplicate transaction')
        used_transactions.add(sample.transaction_id)
        obs=row['observation']
        if obs.get('transaction_id') != sample.transaction_id or obs.get('snapshot_id') != sample.snapshot_id:
            raise NativeReplayRejected('observation transaction/snapshot does not match')
        if obs.get('controller_identity') != sample.controller_identity or obs.get('model_identity') != sample.model_identity:
            raise NativeReplayRejected('observation model/controller identity mismatch')
        if obs.get('motor_state_source') not in ('jsbsim_property','motor_rpm_telemetry'):
            raise NativeReplayRejected('observed motor state must be measured/native simulation property')
        obs_time=float(obs['plant_time_s'])
        if not (math.isfinite(obs_time) and abs(obs_time-(start+u['dt']))<=.005):
            raise NativeReplayRejected('observation time not aligned with target plant step')
        pred=replay_native_actuator(fmu,sample)['prediction']
        errors={}
        for key, model_key, tol in (("vn","next_vn",velocity_tol),("ve","next_ve",velocity_tol),
                                    ("vd","next_vd",velocity_tol),("omega_left","next_omega_left",omega_tol),
                                    ("omega_right","next_omega_right",omega_tol)):
            actual=float(obs[key])
            if not math.isfinite(actual):
                raise NativeReplayRejected(f'observation {key} is nonfinite')
            errors[key]=abs(actual-float(pred[model_key]))
        comparisons.append({"line":n,"transaction_id":sample.transaction_id,
                            "plant_time_s":start,"error":errors,
                            "within_configured_tolerance":all(errors[k]<= (omega_tol if k.startswith('omega') else velocity_tol) for k in errors)})
    if len(comparisons)<min_pairs:
        raise NativeReplayRejected('insufficient paired native trace rows')
    return comparisons


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--trace',type=Path,help='Real recorded JSONL; each row has sample and observation (see README_STAGE2_FA.md)')
    ap.add_argument('--velocity-tol-m-s',type=float,required=False)
    ap.add_argument('--omega-tol-rad-s',type=float,required=False)
    ap.add_argument('--min-pairs',type=int,default=3)
    ap.add_argument('--output',type=Path,default=Path('stage2_native_trace_report.json'))
    args=ap.parse_args()
    report={'test':'LAYER2_STAGE2_NATIVE_TRACE','flight_authorized':False,
            'native_controller_prediction_certified':False,
            'scope':'OFFLINE_PAIRWISE_SURROGATE_COMPARISON_NOT_ONLINE_RELEASE'}
    rc=2
    try:
        if args.trace is None:
            raise NativeReplayRejected('native trace not supplied; no synthetic Site PASS')
        if not args.trace.is_file():
            raise NativeReplayRejected(f'native trace file missing: {args.trace}')
        if any(x is None or not math.isfinite(x) or x<=0 for x in (args.velocity_tol_m_s,args.omega_tol_rad_s)):
            raise NativeReplayRejected('explicit positive, justified velocity and omega tolerances required')
        with args.trace.open('r',encoding='utf-8') as stream:
            records=[json.loads(line) for line in stream if line.strip()]
        with FormulaFMU(ROOT/'fmus'/'WingMPET.fmu') as fmu:
            results=verify(records,fmu,args.velocity_tol_m_s,args.omega_tol_rad_s,args.min_pairs,require_source_evidence=True)
        report['count']=len(results)
        report['comparisons']=results
        report['thresholds']={'velocity_m_s':args.velocity_tol_m_s,'omega_rad_s':args.omega_tol_rad_s}
        report['native_capture_provenance_checked']=True
        report['threshold_justification_externally_required']=True
        report['status']='SITE_TRACE_COMPARISON_PASS' if all(r['within_configured_tolerance'] for r in results) else 'SITE_TRACE_COMPARISON_FAIL'
        rc=0 if report['status']=='SITE_TRACE_COMPARISON_PASS' else 1
    except (NativeReplayRejected,KeyError,ValueError,TypeError,OSError,IndexError) as e:
        report['status']='BLOCKED_EXTERNAL'; report['reason']=str(e)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(report,indent=2,ensure_ascii=False))
    return rc

if __name__=='__main__':
    raise SystemExit(main())
