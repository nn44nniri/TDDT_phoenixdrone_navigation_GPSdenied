"""New issuer/service for future Native predictions; never upgrades review certs.

A bounded model is a trusted TDOC Python dependency, not a JSON flag. No such
qualified model is installed by default. Qualification must be independently
revalidated by that model on every issue/consume; this issuer adds current
deployment/binding/history/transaction/clock checks, not scientific qualification.
"""
from copy import deepcopy
import time

from layer4.native_provider_contract import digest
from layer4.native_provider_certificate import _check_time
from layer4.native_operational import CERT_SCHEMA,USAGE,require,validate_execution_proof
from layer6.binding import consumption_identity
from layer6.timebase import monotonic_domain
from .native_certificate import NativeCertificateAuthority


class NativeOperationalAuthority(NativeCertificateAuthority):
    def __init__(self,config,binding_path,*,bounded_model=None,layer4_config):
        super().__init__(config,binding_path)
        self.bounded_model=bounded_model
        self.layer4_config=layer4_config
        self._issued={}

    def current_backend(self):
        require(self.bounded_model is not None,'bounded Native future model is not qualified/installed')
        self._current()
        return deepcopy(self.bounded_model.current_backend())

    def _material(self,request):
        require(self.bounded_model is not None,'bounded Native future model is not qualified/installed')
        before=self._current();model,identity,domain=before
        require(request['backend']==self.current_backend(),'current Native backend/parameter mismatch')
        q=self.bounded_model.verify_qualification(deepcopy(request),deepcopy(model),deepcopy(identity))
        require(q['schema']=='Native-bounded-model-qualification-v1' and q['status']=='VALIDATED_FOR_FUTURE',
                'Native bounds have not passed independent future qualification')
        require(q['uncertainty_kind']=='bounded' and q['request_sha256']==digest(request),
                'unbounded or out-of-domain Native model')
        require(len(q['qualification_artifact_sha256'])==64 and
                isinstance(q['independent_trace_sha256'],list) and bool(q['independent_trace_sha256']) and
                all(len(h)==64 for h in q['independent_trace_sha256']),
                'independent Native qualification lineage missing')
        require(q['model_id']==model['model_id'] and q['backend_sha256']==digest(request['backend']),
                'qualification model/FCU parameters are no longer current')
        prediction=self.bounded_model.predict(deepcopy(request))
        require(prediction['uncertainty']['qualification_artifact_sha256']==q['qualification_artifact_sha256'],
                'prediction not from currently qualified bounded model')
        history=self._history()
        require(self._current()==before,'Native inputs changed during prediction/qualification')
        return before,q,prediction,history

    def issue(self,request):
        r=deepcopy(request);before,q,p,history=self._material(r)
        wall=time.time();mono=time.monotonic()
        deadline=min(wall+30.,float(r['snapshot']['context']['snapshot_epoch_s'])+
                     float(self.layer4_config.get('snapshot.max_age_s',5.)))
        require(deadline>wall,'Native snapshot expired during qualification/prediction')
        duration=deadline-wall
        certificate={'schema':CERT_SCHEMA,'claims':{
            'request_sha256':digest(r),'snapshot_hash':r['snapshot']['snapshot_hash'],
            'candidate_id':r['candidate_id'],'transaction_id':r['transaction_id'],
            'model_dependencies':before[0],'environment_identity':before[1],'backend':r['backend'],
            'coordinate_origin_sha256':p['coordinate_origin_sha256'],
            'qualification_artifact_sha256':q['qualification_artifact_sha256'],
            'qualification_record_sha256':digest(q),'negative_history':history,
            'prediction_sha256':digest(p),'network_trajectory_sha256':digest(p['network_trajectory']),
            'timing':{'monotonic_domain_id':before[2],'admission_deadline_unix_s':deadline},
            'scope':{'usage':USAGE,'future_rollouts_covered':True,
                'operational_prediction_qualified':True,'bounded_error_model_qualified':True,
                'flight_authorized':False,'ARM_authorized':False,'phase':r['phase'],
                'candidate_and_snapshot_only':True,'commit_segment_s':r['commit_segment_s']}},
            'validity':{'issued_unix_s':wall,'expires_unix_s':deadline,'issued_monotonic_s':mono,
                'expires_monotonic_s':mono+duration,'monotonic_domain_id':before[2],'lifetime_s':duration}}
        pin=digest(certificate)
        for key in list(self._issued):
            if self._issued[key]['certificate']['validity']['expires_monotonic_s']<=mono:
                del self._issued[key]  # Expired transient permits, never KStore/knowledge.
        require(len(self._issued)<32,'Native issuer capacity reached; no silent renewal/eviction')
        self._issued[pin]={'request':r,'certificate':deepcopy(certificate),'prediction':deepcopy(p)}
        proof=self.consume(certificate,pin,r)
        validate_execution_proof(r,proof,self.layer4_config)
        return deepcopy(certificate)

    def consume(self,certificate,pin,request):
        require(pin in self._issued and self._issued[pin]['certificate']==certificate,
                'Native certificate was not pinned by this issuer')
        require(digest(certificate)==pin and request==self._issued[pin]['request'],
                'Native certificate or decision request changed')
        c=deepcopy(certificate)
        _check_time(c,time.time(),time.monotonic(),monotonic_domain())
        before,q,p,history=self._material(request)
        claims=c['claims']
        require(claims['model_dependencies']==before[0] and claims['environment_identity']==before[1] and
                claims['qualification_record_sha256']==digest(q) and claims['prediction_sha256']==digest(p) and
                claims['negative_history']==history,'Native qualification/prediction/source changed')
        _check_time(c,time.time(),time.monotonic(),monotonic_domain())
        proof={'schema':'TDOC-L4-native-execution-proof-v1','request_sha256':digest(request),
            'certificate':c,'certificate_sha256':pin,'prediction':deepcopy(p),
            'consumption':{'status':'VALID_OPERATIONAL_PREDICTION','certificate_sha256':pin}}
        validate_execution_proof(request,proof,self.layer4_config)
        return proof


class NativeOperationalService:
    """One certificate per decision; expiry cannot trigger replay or renewal."""
    def __init__(self,authority):self.authority=authority;self.certificate=None;self.request_hash=None
    def current_backend(self):return self.authority.current_backend()
    def evaluate(self,request):
        key=digest(request)
        if self.request_hash is None:
            self.request_hash=key
            self.certificate=self.authority.issue(request)
        require(key==self.request_hash and self.certificate is not None,
                'Native decision already failed or belongs to another snapshot/candidate')
        return self.authority.consume(self.certificate,digest(self.certificate),request)
