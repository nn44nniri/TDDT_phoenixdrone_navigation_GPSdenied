from __future__ import annotations
from typing import Any,Mapping

def execution_gate_trace(cycle:Mapping[str,Any], base_trace:Mapping[str,Any]|None=None)->dict[str,Any]:
    """Apply downstream execution precedence over upstream L5 FINAL."""
    out=dict(base_trace or {})
    ds=cycle.get('downstream_layers') or {}
    d6=ds.get('6') or {}; o6=d6.get('output') or {}
    status=str(d6.get('status') or o6.get('status') or 'NOT_RUN')
    act=bool(o6.get('actuation_executed') is True or status=='EXECUTED')
    if status=='FAILED':
        out['first_stop_gate']='EXECUTION_FAILED'; out['first_stop_reasons']=[str(o6.get('reason') or 'layer6_failed')]
    elif status not in ('EXECUTED','OBSERVATION_ONLY'):
        if out.get('first_stop_gate')=='FINAL':
            out['first_stop_gate']='EXECUTION_NOT_EXECUTED'; out['first_stop_reasons']=[f'layer6_status={status}']
    out['executed_final']=bool(out.get('first_stop_gate')=='FINAL' and status=='EXECUTED' and act)
    out['rtmo_execution_precedence']=True
    return out
