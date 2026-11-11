"""Versioned, short-lived certificates for one independently observed reference.

Integrity uses a caller-pinned digest, not a signature or a self-trusted hash.
Unbounded reference observations never become bounded models or flight permits.
The TDOC authority supplies freshly rehashed deployment and environment inputs.
"""
from copy import deepcopy
import math
import time

from .native_provider_contract import digest, validate_request
from .native_prediction_validation import validate_native_pair, validation_policy

SCHEMA = 'TDDT-native-provider-certificate-v1'
REVISION = 'phoenix-native-reference-certificate-r7.05-v1'
USAGE = 'EXPERIMENTAL_REFERENCE_REVIEW'
MAX_LIFETIME_S = 30.


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _finite(v):
    return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)


def _claims(bundle, model, identity, clock_domain, history):
    r,p,e,o = (deepcopy(bundle[k]) for k in ('request','prediction','evidence','observation'))
    _require(validate_request(r)['contract_valid'], 'invalid certificate request')
    _require(model['model_id'] == digest({k:v for k,v in model.items() if k!='model_id'}), 'invalid model identity')
    _require(r['context']['model_id']==model['model_id']==identity['model_id'], 'current model mismatch')
    for key in ('binding_hash','handoff_hash'):
        _require(r['context'][key]==identity[key], 'current environment mismatch: '+key)
    _require(r['context']['forcing_sha256']==identity['forcing_file_sha256'], 'current forcing mismatch')
    _require(r['timing']['monotonic_domain_id']==clock_domain, 'host boot/monotonic domain mismatch')
    _require(r['context']['phase']=='isolated_on_ground_reference' and r['backend']['armed'] is False,
             'certificate requires isolated DISARMED reference phase')
    _require(all(c['u_cmd'][2]==0 and not c['discrete'] and c['mode'] in (None,r['backend']['initial_mode'])
                 for c in r['pilot_plan']), 'certificate input outside zero-throttle constant-mode scope')
    _require(p['uncertainty']['kind']=='unbounded', 'unsupported bounded uncertainty claim')
    _require(history['known_out_of_budget_rejection_preserved'] is True and
             history['scope_expansion_permitted'] is False and history['evidence_sha256'],
             'known negative history must be preserved')
    result = validate_native_pair(r,p,e,o,prediction_trace=bundle['prediction_trace'],
                                 observation_trace=bundle['observation_trace'],policy=validation_policy())
    _require(result['status']=='CONSISTENT_IN_EXPERIMENTAL_SCOPE', 'independent validation rejected: '+str(result['reason']))
    return {'model_dependencies':deepcopy(model),'environment_identity':deepcopy(identity),
            'request_sha256':digest(r),'context':r['context'],'backend':r['backend'],
            'initial_state_sha256':digest(r['initial_state']),'isolation':r['isolation'],'timing':r['timing'],
            'prediction_sha256':digest(p),'forecast_evidence_sha256':digest(e),'observation_sha256':digest(o),
            'prediction_trace_sha256':result['prediction_trace_sha256'],
            'independent_trace_sha256':result['independent_trace_sha256'],
            'prediction_branch_session_id':result['prediction_branch_session_id'],
            'independent_branch_session_id':result['independent_branch_session_id'],
            'validation_sha256':digest(result),'validation_policy':validation_policy(),
            'negative_history':deepcopy(history),'horizon_policy':e['policy'],
            'scope':{'usage':USAGE,'exact_reference_pair_only':True,'future_rollouts_covered':False,
                     'operational_prediction_qualified':False,'bounded_error_model_qualified':False,
                     'mission_checkpoint_available':False,'uncertainty':'unbounded',
                     'navigation_source':'FCU evaluation only; never MESA navigation',
                     'flight_authorized':False,'FINAL_authorized':False}}


def issue_certificate(bundle, *, model, identity, clock_domain, history, lifetime_s=30.,
                      now_unix=None, now_monotonic=None):
    """Raise on unavailable/negative/stale evidence; never issue a dummy approval."""
    started_mono=time.monotonic() if now_monotonic is None else now_monotonic
    started_wall=time.time() if now_unix is None else now_unix
    _require(all(_finite(v) for v in (started_mono,started_wall,lifetime_s)) and
             started_mono>=0 and 0<lifetime_s<=MAX_LIFETIME_S, 'invalid certificate clock/lifetime')
    claims=_claims(bundle,model,identity,clock_domain,history)
    deadline=claims['timing']['admission_deadline_unix_s']
    duration=min(lifetime_s,deadline-started_wall)
    _require(duration>0, 'request admission deadline expired before issuance')
    certificate={'schema':SCHEMA,'revision':REVISION,'claims':claims,
                 'validity':{'issued_unix_s':started_wall,'expires_unix_s':started_wall+duration,
                             'issued_monotonic_s':started_mono,'expires_monotonic_s':started_mono+duration,
                             'monotonic_domain_id':clock_domain,'lifetime_s':duration}}
    # Production issuance must still be inside its validity after raw decoding.
    if now_unix is None and now_monotonic is None:
        _check_time(certificate,time.time(),time.monotonic(),clock_domain)
    return certificate


def _check_time(certificate, wall, mono, domain):
    v=certificate['validity']
    _require(set(v)=={'issued_unix_s','expires_unix_s','issued_monotonic_s','expires_monotonic_s',
                     'monotonic_domain_id','lifetime_s'}, 'invalid validity fields')
    _require(all(_finite(v[k]) for k in v if k!='monotonic_domain_id') and
             _finite(wall) and _finite(mono), 'nonfinite certificate clock')
    _require(0<v['lifetime_s']<=MAX_LIFETIME_S and v['issued_monotonic_s']>=0,
             'invalid certificate lifetime')
    _require(abs(v['expires_unix_s']-v['issued_unix_s']-v['lifetime_s'])<1e-6 and
             abs(v['expires_monotonic_s']-v['issued_monotonic_s']-v['lifetime_s'])<1e-6,
             'inconsistent certificate validity interval')
    _require(domain==v['monotonic_domain_id']==certificate['claims']['timing']['monotonic_domain_id'],
             'certificate host boot/clock domain changed')
    _require(v['issued_unix_s']<=wall<v['expires_unix_s'] and
             v['issued_monotonic_s']<=mono<v['expires_monotonic_s'], 'certificate expired or clock reversed')
    _require(abs((wall-v['issued_unix_s'])-(mono-v['issued_monotonic_s']))<=2.,
             'wall/monotonic clock discontinuity')
    _require(v['expires_unix_s']<=certificate['claims']['timing']['admission_deadline_unix_s'] and
             wall<certificate['claims']['timing']['admission_deadline_unix_s'], 'request admission deadline expired')


def check_certificate(certificate, expected_sha256, bundle, *, model, identity, clock_domain, history,
                      usage=USAGE, now_unix=None, now_monotonic=None):
    """Recheck all claims and raw evidence on each consumption; no approval cache.

    now_* are deterministic test inputs. Production uses the TDOC authority,
    whose public consumption API has no caller-controlled clock/model override.
    """
    result={'schema':'TDDT-native-certificate-consumption-v1','status':'REJECTED','reason':None,
            'certificate_sha256':expected_sha256,'usage':usage,'flight_authorized':False,
            'operational_prediction_qualified':False,'bounded_error_model_qualified':False}
    try:
        c=deepcopy(certificate)
        _require(isinstance(expected_sha256,str) and len(expected_sha256)==64 and digest(c)==expected_sha256,
                 'certificate differs from externally pinned digest')
        _require(set(c)=={'schema','revision','claims','validity'} and c['schema']==SCHEMA and c['revision']==REVISION,
                 'unsupported certificate schema/revision')
        _require(usage==USAGE, 'certificate scope forbids requested usage')
        wall=time.time() if now_unix is None else now_unix
        mono=time.monotonic() if now_monotonic is None else now_monotonic
        _check_time(c,wall,mono,clock_domain)
        _require(c['claims']==_claims(bundle,model,identity,clock_domain,history),
                 'certificate claims differ from current inputs or raw evidence')
        if now_unix is None and now_monotonic is None:
            _check_time(c,time.time(),time.monotonic(),clock_domain)
        result.update(status='VALID_IN_EXACT_REFERENCE_SCOPE',reason='live identity and independent raw evidence rechecked')
    except (ValueError,TypeError,KeyError,OSError,OverflowError,IndexError) as exc:
        result['reason']=str(exc)
    return result
