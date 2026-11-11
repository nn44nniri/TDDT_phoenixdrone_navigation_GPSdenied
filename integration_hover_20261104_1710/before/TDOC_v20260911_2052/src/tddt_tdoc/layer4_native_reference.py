"""TDOC-owned R7.06 provider/independent-observer/certificate bridge into Layer4.

One prepared isolated branch per service; a replay is never repeated on expiry
or a changed candidate. Stored artifacts are evidence, not a cached approval.
"""
from copy import deepcopy
from pathlib import Path

from layer4.native_provider_contract import digest,validate_request
from layer4.native_reference_provider import NativeReferenceProvider
from layer6.native_session import write
from .native_certificate import read_bundle


class Layer4NativeReferenceService:
    def __init__(self,runner,observe_independently,authority,case_directory):
        self.provider=NativeReferenceProvider(runner)
        self.observe_independently=observe_independently
        self.authority=authority
        self.case=Path(case_directory)
        self.request_sha256=None
        self.certificate=None
        self.pin=None
        self.started=False
        self.prediction_calls=0
        self.observation_calls=0

    def evaluate(self,request):
        r=deepcopy(request)
        if not validate_request(r)['contract_valid']:
            raise ValueError('invalid Layer4 native reference request')
        key=digest(r)
        if self.started:
            if key!=self.request_sha256:raise ValueError('Layer4 provider is bound to a different snapshot/candidate')
            if self.certificate is None:raise ValueError('Layer4 reference attempt already failed; new isolated reference required')
        else:
            self.started=True;self.request_sha256=key
            self.prediction_calls+=1
            prediction=self.provider.predict(r)
            write(self.case/'REQUEST.json',r);write(self.case/'PREDICTION.json',prediction)
            write(self.case/'FORECAST_EVIDENCE.json',self.provider.last_evidence)
            if prediction['status']!='PREDICTED':
                self.provider.runner.stop()
                raise ValueError('Layer4 native prediction unavailable: '+prediction['reason'])
            self.observation_calls+=1
            observation=self.observe_independently(deepcopy(r))
            write(self.case/'OBSERVATION.json',observation)
            certificate=self.authority.issue(self.case)
            self.certificate=deepcopy(certificate);self.pin=digest(certificate)
            write(self.case/'CERTIFICATE.json',certificate)
            write(self.case/'CERTIFICATE_PIN.json',{'sha256':self.pin,'trust':'TDOC issuer output; never a request/snapshot flag'})
        # Reload bytes even on the second call; no recorded receipt can renew a
        # stale certificate or hide parameter, trace, or deployment drift.
        bundle=read_bundle(self.case)
        if digest(bundle['request'])!=key:raise ValueError('Layer4 native request evidence changed')
        receipt=self.authority.consume(self.certificate,self.pin,self.case)
        return {'schema':'TDOC-L4-native-reference-proof-v1','request_sha256':key,
                'prediction':bundle['prediction'],'certificate':deepcopy(self.certificate),
                'certificate_sha256':self.pin,'consumption':receipt}
