"""R7.07 evidence-based readiness for the currently certified reference scope.

This evaluator reports the unmet release contract. It does not manufacture
operational CI/ADM/PSF/RTA/governance passes from a reference-review certificate.
"""
from copy import deepcopy
import time

from .utils import sha256_obj

SCHEMA='Layer5-native-readiness-result-v1'
L4_REFERENCE_SCHEMA='Layer4-native-reference-result-v1'


class NativeReadinessResult:
    def __init__(self,payload):self.payload=payload
    def to_dict(self):return deepcopy(self.payload)


def evaluate_reference_readiness(engine,layer4,checker,*,learning_mode):
    evidence={'status':'REJECTED','reason':None,'certificate_sha256':None}
    blockers=[]
    def require(value,reason):
        if not value:raise ValueError(reason)
    try:
        source=deepcopy(layer4)
        require(source['schema']==L4_REFERENCE_SCHEMA,'unsupported native readiness input')
        require(source['actuator_command'] is False and source['network_commit'] is False,
                'Layer4 boundary violation')
        require(source['verified_candidates']==[],'reference result cannot contain operational verified candidates')
        require(checker is not None,'TDOC current-evidence rechecker not bound')
        proof=checker.check(source)
        require(proof['schema']=='TDOC-L5-native-readiness-proof-v1' and
                proof['layer4_result_sha256']==sha256_obj(source),'current Layer4 evidence mismatch')
        c=proof['certificate'];pin=proof['certificate_sha256'];receipt=proof['consumption']
        require(sha256_obj(c)==pin,'certificate issuer pin mismatch')
        require(receipt['status']=='VALID_IN_EXACT_REFERENCE_SCOPE' and receipt['certificate_sha256']==pin,
                'current certificate rejected: '+str(receipt.get('reason')))
        from layer4.native_provider_certificate import _check_time
        _check_time(c,time.time(),time.monotonic(),c['claims']['timing']['monotonic_domain_id'])
        candidate=source['reference_candidate'];claims=c['claims']
        require(source['verification']['verified'] is True and candidate['verified'] is True,
                'Layer4 reference is not verified')
        require(candidate['candidate_id']==claims['context']['candidate_id'] and
                candidate['snapshot_hash']==claims['context']['snapshot_hash'], 'candidate/snapshot mismatch')
        scope=claims['scope']
        require(scope['usage']=='EXPERIMENTAL_REFERENCE_REVIEW' and scope['exact_reference_pair_only'] is True and
                scope['FINAL_authorized'] is False and scope['flight_authorized'] is False and
                scope['operational_prediction_qualified'] is False and scope['future_rollouts_covered'] is False,
                'unsupported certificate authority/scope claim')
        evidence={'status':'VALID_REFERENCE_ONLY','reason':'fresh exact reference certificate rechecked',
                  'certificate_sha256':pin,'candidate_id':candidate['candidate_id'],
                  'snapshot_hash':candidate['snapshot_hash'],'certificate_scope':deepcopy(scope)}
        blockers.append('provider_certificate_forbids_FINAL_and_future_execution')
    except Exception as exc:
        evidence['reason']=str(exc)
        blockers.append('current_native_reference_evidence_invalid')
    # There is no operational candidate/current MESA snapshot in this schema.
    # Missing safety evidence is neither a passing zero risk nor a numeric zero.
    independent={name:{'status':'NOT_ASSESSED','ok':None,'reason':reason} for name,reason in (
        ('CI','operational_atomic_snapshot_and_current_state_not_supplied'),
        ('ADM','operational_verified_candidate_and_risk_uncertainty_evidence_not_supplied'),
        ('PSF','verified_operational_command_and_safety_envelope_not_supplied'),
        ('RTA','independent_operational_safety_filter_result_not_supplied'),
        ('governance','eligible_operational_candidate_and_independent_gates_not_supplied'),
        ('Layer6_contract','no_authorized_D_exec_to_validate'))}
    blockers.extend(record['reason'] for record in independent.values())
    return NativeReadinessResult({'schema':SCHEMA,'status':'NOT_READY','ok':False,
        'learning_mode':str(learning_mode),'readiness_scope':'CURRENT_NATIVE_REFERENCE_TO_OPERATIONAL_RELEASE',
        'reference_evidence':evidence,'gates':{**independent,'provider':{'status':'REJECTED_FOR_EXECUTION','ok':False},'FINAL':False},
        'blockers':blockers,'D_exec':None,'layer6_package':None,'control_action':None,
        'decision_confidence':None,'nominal_control_ready':False,'flight_authorized':False,
        'environment_feedback':{'executed':False,'reason':'no_authorized_D_exec'},
        'acceptance':{'R7_07_end_condition_met':False,'R7_stage_release_accepted':False},
        'provenance':{'layer':'5','persistent_knowledge_write':False,'network_commit':False,
                      'model_retraining':False,'direct_pwm':False,'gate_passes_invented':False}})
