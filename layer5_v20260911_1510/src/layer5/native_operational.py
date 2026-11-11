"""Bounded Native ground decisions; separate from unbounded reference review.

Every positive result needs a TDOC-bound rechecker, real CI/ADM, a predictive
box safety test, existing RTA/confidence/governance and pre-execution validation.
This module never starts a simulator, submits a command or writes knowledge.
"""
from copy import deepcopy
import math
import time

from layer4.native_operational import RESULT_SCHEMA as L4_SCHEMA, require, vector, finite
from .models import Layer5Candidate
from .utils import sha256_obj

SCHEMA='Layer5-native-execution-result-v1'


class NativeExecutionResult:
    def __init__(self,payload):self.payload=payload
    def to_dict(self):return deepcopy(self.payload)


class _AssemblyAuthorization:
    """In-process capability; no request JSON flag can create this object."""
    def __init__(self,checker,source,arguments):
        self.checker=checker;self.source=deepcopy(source);self.arguments=deepcopy(arguments)
    def validate(self,arguments):
        require(arguments==self.arguments,'assembly differs from approved command/context')
        self.checker.check_layer4(self.source)


def _psf(engine,source,command):
    r=source['request'];constraints=r['snapshot']['constraints']
    policy=constraints['native_safety_policy']
    require(policy['schema']=='Native-pilot-safety-envelope-v1','native safety envelope schema')
    require(policy['phase']=='on_ground' and policy['ARM_authorized'] is False,'unsupported safety phase/ARM')
    lo,hi=policy['state_lower'],policy['state_upper']
    require(vector(lo,17) and vector(hi,17) and all(a<=b for a,b in zip(lo,hi)),
            'physical Native envelope unavailable')
    pilot=policy['pilot_domain']
    require(vector(pilot['lower'],4) and vector(pilot['upper'],4),'pilot envelope unavailable')
    reasons=[]
    for i,(x,a,b) in enumerate(zip(command,pilot['lower'],pilot['upper'])):
        if not a<=x<=b:reasons.append('pilot_envelope_'+str(i))
    if not (-1<=command[0]<=1 and -1<=command[1]<=1 and command[2]==0 and -1<=command[3]<=1):
        reasons.append('pilot_ground_range')
    norm=math.sqrt(sum(x*x for x in command))
    if norm>float(engine.config.get('safety.max_command_norm',2.)):reasons.append('command_norm_limit')
    for row in source['prediction']['tube']:
        if not all(a<=x<=y<=b for a,x,y,b in zip(lo,row['lower'],row['upper'],hi)):
            reasons.append('predicted_uncertainty_tube_exits_safety_envelope')
    # The Native tube's last four coordinates are PWM, never measured RPM.
    # No spool/motor-speed value is invented out of a low PWM observation.
    return {'ok':not reasons,'gate':not reasons,'reasons':sorted(set(reasons)),
            'command':list(command) if not reasons else None,'command_norm':norm,
            'safety_policy_sha256':sha256_obj(policy),'prediction_sha256':sha256_obj(source['prediction']),
            'semantics':'pilot-domain command; bounded NED/BODY_FRD/PWM tube containment',
            'motor_RPM_claimed':False}


def evaluate_native_execution(engine,source,checker,*,learning_mode,assess_only=False):
    result={'schema':SCHEMA,'ok':False,'status':'HOLD','learning_mode':learning_mode,
        'gates':{'FINAL':False},'D_exec':None,'layer6_package':None,'decision':None,
        'blockers':[],'flight_authorized':False,'environment_feedback':{'executed':False},
        'provenance':{'persistent_knowledge_write':False,'network_commit':False,
                      'model_retraining':False,'direct_pwm':False}}
    t0=time.perf_counter()
    try:
        require(learning_mode=='ONLINE_JSBSIM','Native execution decisions require Online semantics')
        require(isinstance(source,dict) and source.get('schema')==L4_SCHEMA,'Native execution result schema')
        require(checker is not None,'TDOC Native execution rechecker not bound')
        checker.bind_engine(engine)
        # This re-executes Layer4 verification and checks the current binding,
        # model, prior/forcing hashes and short-lived issuer receipt.
        checker.check_layer4(source)
        require(source['verification']['verified'] is True and len(source['verified_candidates'])==1,
                'no execution-eligible Layer4 candidate')
        require(source['candidates']==source['verified_candidates'],'candidate registry mismatch')
        raw=deepcopy(source['candidates'][0]);r=source['request']
        require(raw['snapshot_hash']==source['snapshot_hash'] and raw['candidate_id']==r['candidate_id'],
                'candidate identity mismatch')
        c=Layer5Candidate(candidate_id=raw['candidate_id'],raw=raw,snapshot_version=raw['snapshot_version'],
            snapshot_hash=raw['snapshot_hash'],actions=raw['actions'],objective_vector=raw['objective_vector'],
            normalized_objectives={},network_trajectory=raw['network_trajectory'],uncertainty=raw['uncertainty'],
            feasible=raw['feasible'],violation_probability=raw['violation_probability'],verified=True,
            verification=raw['verification'],provenance=raw['provenance'])
        require(c.uncertainty and all(all(finite(x) for row in cov for x in row) for cov in c.uncertainty),
                'uncertainty second-moment bound missing/nonfinite')
        c.ci=engine.ci_gate.evaluate(c,source)
        result['gates']['CI']=c.ci.to_dict()
        require(c.ci.ok,'CI rejected: '+','.join(c.ci.reasons))
        c.adm=engine.adm.evaluate(c)
        result['gates']['ADM']=c.adm.to_dict()
        require(c.adm.ok,'ADM rejected: '+','.join(c.adm.reasons))
        preserved=c.actions==[row['u_cmd'] for row in r['pilot_plan']]
        result['gates']['VP']={'ok':preserved,'reason':'nominal qualified pilot plan unchanged; no qLPV correction'}
        require(preserved,'Native nominal command changed after qualification')
        command=list(c.actions[0]);psf=_psf(engine,source,command)
        result['gates']['PSF']=psf
        rta=engine.rta.choose(psf,engine.baseline.action(None),c.ci.ok and c.adm.ok)
        result['gates']['RTA']={**rta,'ok':rta['gate'] is True and rta['source']=='advanced'}
        require(psf['ok'] and result['gates']['RTA']['ok'],'PSF/RTA did not approve Native command')
        engine.normalizer.normalize([c],{})
        weights=engine.config.get('objectives.guidance_weights',{})
        sensitivity=engine.sensitivity.compute([c],weights,top_k=1)[c.candidate_id]['q_S']
        rho,parts=engine.confidence.compute([c],c,sensitivity,weights=weights)
        require(finite(rho),'decision confidence unavailable')
        mode=engine.mode.resolve(rho,c.violation_probability,
            engine.config.get('operational.autonomous_authorized',True),False,
            learning_mode=learning_mode,simulation_authorized=False)
        gov=engine.gov.authorize(mode,c.candidate_id,c.snapshot_version,c.snapshot_hash,rho,parts,
            evidence_type='qualified_native_ground_prediction')
        result['gates']['governance']={**gov,'ok':gov['gate'] is True}
        require(gov['gate'] is True,'governance rejected: '+','.join(gov['reasons']))
        elapsed=(time.perf_counter()-t0)*1000
        limit=float(engine.config.get('edge.deadline_ms',75.))
        result['gates']['budget']={'ok':elapsed<=limit,'elapsed_ms':elapsed,'limit_ms':limit}
        require(elapsed<=limit,'Native Layer5 deadline exceeded')
        if assess_only:
            result.update(ok=True,status='GATES_APPROVED_NO_RELEASE')
            return NativeExecutionResult(result)
        c.vp=c.ci.__class__(True,[],{'scope':'qualified nominal Native pilot; qLPV correction absent'})
        certificate=source['proof']['certificate'];validity=certificate['validity']
        apply=time.time();deadline=min(validity['expires_unix_s'],apply+float(engine.config.get('contract.max_decision_age_s',5.)))
        require(deadline>apply,'Native decision admission deadline expired')
        context={'transaction_id':r['transaction_id'],'candidate_id':c.candidate_id,
            'snapshot_hash':c.snapshot_hash,'snapshot_version':c.snapshot_version,
            'certificate_sha256':source['proof']['certificate_sha256'],'phase':r['phase'],
            'execution_intent':{'requested_pilot_mode':r['pilot_plan'][0]['mode'],'discrete':[]},
            'execution_segment':{'schema':'Layer6-first-move-segment-v1',
                'max_duration_s':r['commit_segment_s'],'close_guard_s':min(.02,r['commit_segment_s']/2)},
            'execution_rate_hz':30.,'prediction_sha256':sha256_obj(source['prediction'])}
        constraints={'pilot_domain':dict(zip(('a','e','t','r'),zip(
            r['snapshot']['constraints']['native_safety_policy']['pilot_domain']['lower'],
            r['snapshot']['constraints']['native_safety_policy']['pilot_domain']['upper']))),
            'direct_pwm':False,'command_domain':'LAYER6_PILOT_NORMALIZED','tdoc_phase':r['phase']}
        constraints['pilot_domain']={k:list(v) for k,v in constraints['pilot_domain'].items()}
        gate_digest=sha256_obj({k:v for k,v in result['gates'].items() if k!='FINAL'})
        provenance={**gov['provenance'],'verification_scope':'NATIVE_BOUNDED_GROUND_PREDICTION',
            'native_execution_context':context,'gate_records_sha256':gate_digest,
            'qlpv_correction_applied':False}
        safety={'psf':True,'rta':True,'gate_records_sha256':gate_digest,
                'safety_policy_sha256':psf['safety_policy_sha256']}
        args={'command':command,'actuator_targets':list(engine.phoenix.PHYSICAL_ACTUATORS),
              'constraints':constraints,'safety_policy':safety,'apply_time':apply,'deadline_time':deadline,
              'confidence':rho,'provenance':provenance}
        permit=_AssemblyAuthorization(checker,source,args)
        package=engine.l6assembler.assemble(**args,execution_authorization=permit).to_dict()
        # FINAL is published only after independent validation of the complete
        # canonical package. No command is submitted by either verification call.
        receipt=checker.verify_package(package,source,result['gates'])
        require(receipt['ok'] is True and receipt['native_execution_model_matched'] is True,
                'independent pre-execution verifier rejected')
        require((time.perf_counter()-t0)*1000<=limit,'Native Layer5 deadline exceeded after verifier')
        result.update(ok=True,status='FINAL_IN_QUALIFIED_GROUND_SCOPE',D_exec=package,
            layer6_package=deepcopy(package),decision={'decision_id':gov['decision_id'],
                'candidate_id':c.candidate_id,'mode':mode['mode'],'confidence':rho},
            execution_verification=receipt,layer4_result=deepcopy(source))
        result['gates']['FINAL']=True
    except Exception as exc:
        result['blockers'].append(str(exc))
    return NativeExecutionResult(result)
