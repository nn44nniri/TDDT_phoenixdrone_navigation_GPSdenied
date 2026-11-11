"""R7.05 TDOC authority for short-lived, non-operational reference certificates.

No Layer4 readiness or Layer5 release integration occurs in this step. Every
issue/consume operation rereads the binding, model, evidence, and negative case.
"""
from pathlib import Path
import json

from layer4.native_provider_contract import digest
from layer4.native_provider_certificate import check_certificate, issue_certificate
from layer4.native_prediction_validation import sha, validate_native_pair, validation_policy
from layer6.binding import context_guard, consumption_identity
from layer6.timebase import monotonic_domain
from .model_compatibility import capture_model_dependencies

COUNTEREXAMPLE_SHA256='d6d8d6b6cd01a1e3561ed68145243a5b87bccacc940ed27dce89a868d1c2edcd'


def read_bundle(case):
    case=Path(case)
    r={key:json.loads((case/name).read_text()) for key,name in (
        ('request','REQUEST.json'),('prediction','PREDICTION.json'),
        ('evidence','FORECAST_EVIDENCE.json'),('observation','OBSERVATION.json'))}
    r.update(prediction_trace=case/'prediction/telemetry.jsonl',observation_trace=case/'observation/telemetry.jsonl')
    for branch,backend in (('prediction',r['request']['backend']),
                           ('observation',r['observation']['reference']['backend'])):
        if json.loads((case/branch/'PARAMETERS.json').read_text())!=backend['parameters']:
            raise ValueError('current parameter evidence changed: '+branch)
    return r


class NativeCertificateAuthority:
    def __init__(self,config,binding_path):
        self.config=config
        self.guard=context_guard(binding_path)

    def _current(self):
        binding=self.guard.check()
        model=capture_model_dependencies(self.config)
        if model!=binding['model_dependencies']:
            raise ValueError('certificate current model/binding mismatch')
        return model,consumption_identity(binding),monotonic_domain()

    def _history(self):
        case=self.config.package_root/'reports/online/R7_04_20261031_independent_validation_v9/native_final/FBWA_01'
        p=case/'VALIDATION.json'
        if sha(p)!=COUNTEREXAMPLE_SHA256:raise ValueError('mandatory negative history changed or missing')
        b=read_bundle(case)
        checked=validate_native_pair(b['request'],b['prediction'],b['evidence'],b['observation'],
            prediction_trace=b['prediction_trace'],observation_trace=b['observation_trace'],policy=validation_policy())
        if checked!=json.loads(p.read_text()) or checked['status']!='REJECTED':
            raise ValueError('mandatory negative history no longer reproducible')
        return {'schema':'TDDT-native-negative-history-v1','evidence_sha256':COUNTEREXAMPLE_SHA256,
                'prediction_trace_sha256':sha(b['prediction_trace']),
                'independent_trace_sha256':sha(b['observation_trace']),
                'known_out_of_budget_rejection_preserved':True,'scope_expansion_permitted':False,
                'historical_model_id':b['request']['context']['model_id'],
                'PWM_observed_max_us':max(r['metrics']['PWM_max_abs_us'] for r in checked['residuals']),
                'PWM_limit_us':validation_policy()['limits']['PWM_max_abs_us']}

    def issue(self,case,*,lifetime_s=30.):
        before=self._current()
        certificate=issue_certificate(read_bundle(case),model=before[0],identity=before[1],
                                      clock_domain=before[2],history=self._history(),lifetime_s=lifetime_s)
        if self._current()!=before:raise ValueError('current inputs changed during certificate issuance')
        # A second public consumer pass includes elapsed hashing/decoding time.
        check=self.consume(certificate,digest(certificate),case)
        if check['status']!='VALID_IN_EXACT_REFERENCE_SCOPE':raise ValueError(check['reason'])
        return certificate

    def consume(self,certificate,expected_sha256,case,*,usage='EXPERIMENTAL_REFERENCE_REVIEW'):
        try:
            before=self._current()
            result=check_certificate(certificate,expected_sha256,read_bundle(case),model=before[0],
                identity=before[1],clock_domain=before[2],history=self._history(),usage=usage)
            if self._current()!=before:raise ValueError('current inputs changed during certificate consumption')
            # Hashing after the pure check must not turn an expired cert valid.
            from layer4.native_provider_certificate import _check_time
            import time
            if result['status']=='VALID_IN_EXACT_REFERENCE_SCOPE':
                _check_time(certificate,time.time(),time.monotonic(),monotonic_domain())
            return result
        except (ValueError,TypeError,KeyError,OSError,OverflowError,IndexError) as exc:
            return {'schema':'TDDT-native-certificate-consumption-v1','status':'REJECTED','reason':str(exc),
                    'certificate_sha256':expected_sha256,'usage':usage,'flight_authorized':False,
                    'operational_prediction_qualified':False,'bounded_error_model_qualified':False}
