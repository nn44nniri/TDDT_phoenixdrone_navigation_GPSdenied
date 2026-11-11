"""Independent, non-actuating consumer of qualified Native model decisions.

The bound service must implement actual future-model qualification, issuer
consumption and live backend observation. Reference-review services cannot be
substituted. No method here predicts ExecutionEvidence or submits commands.
"""
from copy import deepcopy
import time

from layer4.native_operational import require,finite,RESULT_SCHEMA
from layer4.native_provider_contract import digest
from layer6.binding import context_guard
from layer6.contracts import DecisionEnvelope
from layer6.validation import validate


class NativeExecutionRechecker:
    def __init__(self,config,binding_path,layer4_adapter,qualified_service):
        self.config=config
        self.guard=context_guard(binding_path)
        self.layer4=layer4_adapter
        self.service=qualified_service
        self._decisions={}
        self._last_source=None
        self._engine=None

    def bind_engine(self,engine):
        from layer5.engine import Layer5Engine
        require(isinstance(engine,Layer5Engine),'actual Layer5 engine required for independent gate recheck')
        self._engine=engine

    def check_layer4(self,source):
        binding=self.guard.check()
        require(source['schema']==RESULT_SCHEMA,'operational Layer4 result required')
        r=source['request']
        fresh=self.layer4.verify_native_execution(deepcopy(r),self.service)
        require(fresh['verification']['verified'] is True,
                'current Layer4 execution verification rejected: '+str(fresh['verification'].get('reason')))
        fields=('schema','request','prediction','candidates','verified_candidates','snapshot_hash','snapshot_version')
        require(digest({k:source[k] for k in fields})==digest({k:fresh[k] for k in fields}),
                'Layer5 input differs from current Layer4 Native result')
        require(source['proof']['certificate_sha256']==fresh['proof']['certificate_sha256'],
                'certificate changed; no silent renewal of decision authority')
        claims=fresh['proof']['certificate']['claims']
        require(claims['coordinate_origin_sha256']==digest(binding['mission_origin']),
                'Native operational coordinate origin differs from accepted binding')
        require(claims['model_dependencies']==binding['model_dependencies'],'Native operational model identity mismatch')
        identity=claims['environment_identity']
        for key in ('model_id','binding_hash','handoff_hash'):
            require(identity[key]==binding[key],'Native operational binding mismatch: '+key)
        require(identity['forcing_file_sha256']==binding['forcing_file_sha256'],'Native forcing mismatch')
        # A live Python dependency must supply the actual FCU map/session; the
        # request/certificate parameter snapshots cannot approve each other.
        require(self.service.current_backend()==r['backend'],'live Native FCU parameter/session mismatch')
        self.guard.check()
        self._last_source=deepcopy(fresh)
        return fresh

    def operational_readiness(self):
        try:
            require(self._last_source is not None,'no current qualified operational context')
            self.check_layer4(self._last_source)
            binding=self.guard.check()
            return {'ok':True,'scope':'QUALIFIED_NATIVE_GROUND_CONTEXT','ARM_authorized':False,
                    'prior_identity':{k:binding[k] for k in ('binding_hash','handoff_hash','forcing_file_sha256')}}
        except Exception as exc:return {'ok':False,'reason':str(exc),'ARM_authorized':False}

    def verify_package(self,package,source,gates):
        """Validate complete D_exec before FINAL. It is not execution evidence."""
        fresh=self.check_layer4(source)
        r=source['request'];c=source['proof']['certificate'];p=deepcopy(package)
        for gate in ('CI','ADM','VP','PSF','RTA','governance','budget'):
            require(gates[gate]['ok'] is True,'independent gate unavailable/rejected: '+gate)
        require(p['transaction_id']==r['transaction_id'] and p['candidate_id']==r['candidate_id'],
                'Native package transaction/candidate mismatch')
        require(p['snapshot_hash']==source['snapshot_hash'] and p['snapshot_version']==source['snapshot_version'],
                'Native package snapshot mismatch')
        require(p['certificate_sha256']==source['proof']['certificate_sha256'],'Native package certificate mismatch')
        require(p['u_cmd']==r['pilot_plan'][0]['u_cmd'] and
                p['execution_intent']=={'requested_pilot_mode':r['pilot_plan'][0]['mode'],'discrete':[]},
                'Native command/mode differs from qualified first move')
        segment=p['execution_segment']
        require(segment['max_duration_s']==r['commit_segment_s'] and
                segment['close_guard_s']==min(.02,r['commit_segment_s']/2),'Native commit segment mismatch')
        require(p['execution_rate_hz']==30. and p['phase']==r['phase'],'Native cadence/phase mismatch')
        require(p['C_exec']['direct_pwm'] is False and all(p['S_safe'][k] is True for k in ('psf','rta')),
                'Native package safety/domain invalid')
        original={k:v for k,v in gates.items() if k!='FINAL'}
        gh=digest(original)
        require(p['S_safe']['gate_records_sha256']==p['pi_decision']['gate_records_sha256']==gh,
                'Native independent gate records changed')
        require(p['S_safe']['safety_policy_sha256']==digest(r['snapshot']['constraints']['native_safety_policy']),
                'Native current safety envelope mismatch')
        require(p['prediction_sha256']==digest(source['prediction']),'Native package prediction mismatch')
        require(all(finite(p[k]) for k in ('tau_apply','tau_deadline','rho_decision')) and
                p['tau_apply']<=time.time()<p['tau_deadline']<=c['validity']['expires_unix_s'],
                'Native action package expired or outside certificate validity')
        for canonical,alias in (('u_cmd','command'),('C_exec','execution_constraints'),
                ('S_safe','safety_policy'),('tau_apply','apply_time'),('tau_deadline','deadline_time'),
                ('rho_decision','decision_confidence'),('pi_decision','provenance'),('nu','version')):
            require(p.get(alias)==p[canonical],'conflicting canonical package alias: '+canonical)
        require(p['A_target']==p['actuator_targets'] and bool(p['A_target']),'Native target metadata mismatch')
        require(p['formal_contract']=={k:p[k] for k in ('u_cmd','A_target','C_exec','S_safe','tau_apply',
                    'tau_deadline','rho_decision','pi_decision','nu')},'formal Native action contract changed')
        verdict=validate(DecisionEnvelope.from_dict(p))
        require(verdict.ok,'Layer6 canonical contract rejected: '+','.join(verdict.reasons))
        from layer5.native_operational import evaluate_native_execution
        require(getattr(self,'_engine',None) is not None,'current Layer5 gate evaluator not bound')
        current=evaluate_native_execution(self._engine,fresh,self,learning_mode='ONLINE_JSBSIM',assess_only=True).to_dict()
        require(current['ok'] is True,'current independent safety/governance recheck rejected: '+str(current['blockers']))
        self.guard.check()
        # Register only after validation. A FINAL bit outside this exact package
        # and live decision context is never a credential.
        pin=digest(p)
        self._decisions[r['transaction_id']]={'package_sha256':pin,'source':deepcopy(source),'gates':deepcopy(original)}
        while len(self._decisions)>32:self._decisions.pop(next(iter(self._decisions)))
        return {'ok':True,'native_execution_model_matched':True,'package_sha256':pin,
                'scope':'PRE_EXECUTION_QUALIFIED_NATIVE_GROUND','execution_confirmed':False}

    def verify_result(self,result,*,transaction_id):
        try:
            require(result['schema']=='Layer5-native-execution-result-v1' and result['gates']['FINAL'] is True,
                    'current Layer5 Native FINAL required')
            require(result['flight_authorized'] is False,'Native result widens qualified ground authority')
            row=self._decisions.get(transaction_id)
            require(row is not None,'Native decision was not independently registered')
            require(result['D_exec']==result['layer6_package'] and digest(result['D_exec'])==row['package_sha256'],
                    'Native decision package mutated or rebound')
            require(result['D_exec']['transaction_id']==transaction_id and
                    result['decision']['candidate_id']==result['D_exec']['candidate_id'],'Native decision identity mismatch')
            require(result['decision']['decision_id']==result['D_exec']['pi_decision']['decisionID'],
                    'Native decision provenance mismatch')
            require(digest({k:v for k,v in result['gates'].items() if k!='FINAL'})==digest(row['gates']),
                    'Native decision gate records mutated')
            return self.verify_package(result['D_exec'],row['source'],row['gates'])
        except Exception as exc:
            return {'ok':False,'native_execution_model_matched':False,'reason':str(exc),'execution_confirmed':False}
