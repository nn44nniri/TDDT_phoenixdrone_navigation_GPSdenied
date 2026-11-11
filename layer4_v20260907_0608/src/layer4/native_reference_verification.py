"""R7.06 Layer4 verification of one certified, independently observed reference.

The result has its own schema and an empty operational verified_candidates list.
No qLPV delta action, MESA state, physical-feasibility claim or FINAL is invented.
"""
from copy import deepcopy
from dataclasses import dataclass
import time

from .native_provider_contract import digest, validate_request, validate_prediction
from .native_provider_certificate import USAGE, _check_time

SCHEMA='Layer4-native-reference-result-v1'
SCOPE='NATIVE_EXACT_REFERENCE_REVIEW'


@dataclass
class NativeReferenceResult:
    payload: dict

    def to_dict(self):
        return deepcopy(self.payload)


def verify_reference(verifier,request,service):
    r=deepcopy(request)
    predicted=None;pin=None;receipt=None;attempted=False;valid=False
    def require(value,message):
        if not value:raise ValueError(message)
    try:
        structural=validate_request(r)
        require(structural['contract_valid'],'invalid native reference request: '+','.join(structural['errors']))
        valid=True
        require(r['context']['phase']=='isolated_on_ground_reference', 'native reference certificate cannot verify a mission snapshot')
        require(verifier.config.get('verification.native_reference.enabled',True) is True,
                'native reference verification disabled')
        require(service is not None,'native reference provider service not bound by TDOC')
        attempted=True
        proof=service.evaluate(r)
        require(proof['schema']=='TDOC-L4-native-reference-proof-v1','invalid native provider proof schema')
        require(proof['request_sha256']==digest(r),'provider returned a different request')
        predicted=proof['prediction']
        require(predicted['status']=='PREDICTED' and validate_prediction(r,predicted)['contract_valid'],
                'native reference prediction unavailable or invalid')
        certificate=proof['certificate'];pin=proof['certificate_sha256'];claims=certificate['claims']
        require(digest(certificate)==pin,'certificate differs from TDOC issuer pin')
        require(claims['request_sha256']==digest(r) and claims['prediction_sha256']==digest(predicted),
                'certificate request/prediction mismatch')
        require(claims['context']==r['context'] and claims['backend']==r['backend'] and
                claims['timing']==r['timing'] and claims['isolation']==r['isolation'] and
                claims['initial_state_sha256']==digest(r['initial_state']),
                'certificate does not cover current candidate/snapshot/timing/state')
        scope=claims['scope']
        require(scope['usage']==USAGE and scope['exact_reference_pair_only'] is True and
                scope['uncertainty']=='unbounded' and all(scope[k] is False for k in (
                    'future_rollouts_covered','operational_prediction_qualified','bounded_error_model_qualified',
                    'mission_checkpoint_available','flight_authorized','FINAL_authorized')),
                'native certificate scope expanded or uncertainty hidden')
        receipt=proof['consumption']
        require(receipt['status']=='VALID_IN_EXACT_REFERENCE_SCOPE' and receipt['certificate_sha256']==pin and
                receipt['usage']==USAGE and all(receipt[k] is False for k in (
                    'flight_authorized','operational_prediction_qualified','bounded_error_model_qualified')),
                'current certificate consumption rejected: '+str(receipt.get('reason')))
        _check_time(certificate,time.time(),time.monotonic(),r['timing']['monotonic_domain_id'])
        outcome=verifier.outcome('ATTEMPTED_AND_VERIFIED','native_reference_current_certificate_verified',
            verified=True,attempted=True,executed=False,verification_origin='NATIVE_REFERENCE_CURRENT_CERTIFICATE',
            native_reference_certified=True,native_controller_certified=False,flight_authorized=False,
            verification_scope=SCOPE,certificate_id=pin,certificate_consumption=deepcopy(receipt),
            replay_evidence_observed=True,operational_prediction_qualified=False,uncertainty='unbounded')
    except Exception as exc:
        outcome=verifier.outcome('ATTEMPTED_AND_REJECTED' if attempted else 'NOT_ATTEMPTED',str(exc),
            verified=False,attempted=attempted,executed=False,verification_origin='UNVERIFIED',
            native_reference_certified=False,native_controller_certified=False,flight_authorized=False,
            verification_scope=SCOPE,certificate_id=pin,operational_prediction_qualified=False,
            uncertainty='unbounded',certificate_consumption=deepcopy(receipt))
    # A reference result never enters the ordinary FF candidate release list.
    candidate=None
    if valid:
        candidate={'candidate_id':r['context']['candidate_id'],'candidate_hash':r['context']['candidate_hash'],
                   'snapshot_id':r['context']['snapshot_id'],'snapshot_version':r['context']['snapshot_version'],
                   'snapshot_hash':r['context']['snapshot_hash'],'phase':r['context']['phase'],
                   'pilot_plan':deepcopy(r['pilot_plan']),'timing':deepcopy(r['timing']),
                   'verified':outcome['verified'],'candidate_verified':outcome['candidate_verified'],
                   'verification_scope':SCOPE,'flight_authorized':False}
    return NativeReferenceResult({'schema':SCHEMA,'verification':outcome,'reference_candidate':candidate,
        'verified_reference_candidates':[deepcopy(candidate)] if outcome['verified'] else [],
        'verified_candidates':[],'prediction':deepcopy(predicted) if outcome['verified'] else None,
        'actuator_command':False,'network_commit':False,'flight_authorized':False,
        'operational_prediction_qualified':False,'bounded_error_model_qualified':False,
        'provenance':{'layer':'4','entrypoint':'Layer4Engine.run(native-provider-request)',
                      'verification_scope':SCOPE,'prior_is_not_current_state':True}})
