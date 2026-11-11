"""Current Layer4/certificate revalidation for the Layer5 readiness boundary."""
from copy import deepcopy

from layer4.native_provider_contract import digest
from .native_certificate import read_bundle


class Layer5NativeReadinessChecker:
    def __init__(self,layer4_adapter,native_reference_service):
        self.layer4=layer4_adapter
        self.service=native_reference_service

    def check(self,layer4_result):
        if not self.service.started or self.service.certificate is None:
            raise ValueError('readiness cannot launch a forecast or issue a missing certificate')
        request=read_bundle(self.service.case)['request']
        fresh=self.layer4.verify_native_reference(request,self.service)
        if not fresh['verification']['verified']:
            raise ValueError('Layer4 recheck rejected: '+fresh['verification']['reason'])
        if digest(fresh)!=digest(layer4_result):
            raise ValueError('Layer5 input differs from current Layer4 reference result')
        proof=self.service.evaluate(request)
        return {'schema':'TDOC-L5-native-readiness-proof-v1','layer4_result_sha256':digest(fresh),
                'certificate':deepcopy(proof['certificate']),'certificate_sha256':proof['certificate_sha256'],
                'consumption':deepcopy(proof['consumption'])}
