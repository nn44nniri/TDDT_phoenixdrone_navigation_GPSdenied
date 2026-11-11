"""Execution-eligible Native lane. A TDOC-bound issuer, never JSON, supplies proof.

This does not qualify the existing cold-boot/reference provider. Its unbounded
outputs and review certificates remain in the separate reference lane.
"""
from copy import deepcopy
import math
import time

from .native_provider_contract import DOMAIN, digest, pilot_to_manual, BACKEND_HASH_FIELDS, REQUIRED_PARAMETERS, VECTOR_SPECS
from .native_provider_certificate import _check_time
from .snapshot import Layer3SnapshotAdapter

REQUEST_SCHEMA='Layer4-native-execution-request-v1'
RESULT_SCHEMA='Layer4-native-execution-result-v1'
CERT_SCHEMA='TDDT-native-operational-provider-certificate-v1'
USAGE='OPERATIONAL_PREDICTION_QUALIFIED'
SCOPE='NATIVE_BOUNDED_GROUND_PREDICTION'


def require(condition,reason):
    if not condition:raise ValueError(reason)


def finite(x):
    return type(x) in (int,float) and math.isfinite(x)


def vector(x,n):
    return isinstance(x,list) and len(x)==n and all(finite(v) for v in x)


class NativeOperationalResult:
    def __init__(self,payload):self.payload=payload
    def to_dict(self):return deepcopy(self.payload)


def validate_execution_proof(request,proof,config):
    """Check a fresh trusted-service response; no declarative FF flag is authority."""
    r=deepcopy(request)
    require(r['schema']==REQUEST_SCHEMA,'native operational request schema')
    s=Layer3SnapshotAdapter(config).adapt(r['snapshot'])
    require(r['snapshot'].get('snapshot_hash')==s.hash,'declared atomic snapshot hash required')
    require(r['snapshot']['provenance'].get('snapshot_kind')=='atomic_layer3_network_snapshot',
            'actual Layer3 atomic snapshot provenance required')
    epoch=s.context.get('snapshot_epoch_s')
    require(finite(epoch) and 0<=time.time()-epoch<=float(config.get('snapshot.max_age_s',5.)),
            'current snapshot expired or absolute freshness unavailable')
    require((s.context.get('rtmo_data_authority') or {}).get('navigation')=='MESA_STD',
            'MESA navigation authority required; FCU truth is not navigation')
    require(s.context.get('truth_injection_to_mesa') is False,'explicit no-truth-injection provenance required')
    plan=r['pilot_plan'];dt=r['prediction_dt_s'];commit=r['commit_segment_s']
    require(isinstance(plan,list) and 1<=len(plan)<=10,'native pilot plan length')
    require(finite(dt) and finite(commit) and .1<=commit<=dt<=1. and len(plan)*dt<=3.,'native timing scope')
    require(isinstance(r['transaction_id'],str) and bool(r['transaction_id']) and bool(r['candidate_id']),
            'native transaction/candidate identity required')
    require(r['phase']=='on_ground' and s.context.get('phase')==r['phase'],'only explicitly qualified ground phase')
    mode=plan[0]['mode']
    require(mode in ('QHOVER','QSTABILIZE'),'ground operational mode outside implemented interface')
    backend=r['backend'];parameters=backend['parameters']
    require(backend['armed'] is False and backend['initial_mode']==mode,'Native backend outside ground scope')
    require(all(isinstance(backend[k],str) and len(backend[k])==64 and
            all(x in '0123456789abcdef' for x in backend[k]) for k in BACKEND_HASH_FIELDS),
            'Native backend hashes missing or invalid')
    require(all(k in parameters for k in REQUIRED_PARAMETERS) and
            all(finite(v) for v in parameters.values()) and digest(parameters)==backend['parameter_snapshot_sha256'],
            'Native critical parameters missing or hash mismatch')
    for i,function in enumerate((77,78,73,74),1):
        require(parameters['SERVO%d_FUNCTION'%i]==function and
            parameters['SERVO%d_MIN'%i]<=parameters['SERVO%d_TRIM'%i]<=parameters['SERVO%d_MAX'%i] and
            parameters['SERVO%d_MIN'%i]<parameters['SERVO%d_MAX'%i], 'Phoenix FCU mixer mapping mismatch')
    for row in plan:
        require(row['domain']==DOMAIN and row['mode']==mode and row['discrete']==[],
                'constant mode, pilot domain and no discrete action required')
        pilot_to_manual(row['u_cmd'],{'max_pitch':1.,'max_roll':1.,'max_yaw':1.})
        require(row['u_cmd'][2]==0.,'initial operational scope forbids throttle/ARM')
    require(proof['schema']=='TDOC-L4-native-execution-proof-v1' and proof['request_sha256']==digest(r),
            'current Native service/request mismatch')
    c=proof['certificate'];pin=proof['certificate_sha256'];claims=c['claims'];scope=claims['scope']
    require(c['schema']==CERT_SCHEMA and digest(c)==pin,'unsupported certificate or issuer pin mismatch')
    require(scope['usage']==USAGE and scope['future_rollouts_covered'] is True and
            scope['operational_prediction_qualified'] is True and scope['bounded_error_model_qualified'] is True,
            'provider has no qualified bounded future-execution scope')
    require(scope['flight_authorized'] is False and scope['ARM_authorized'] is False,
            'unsupported expansion beyond ground-only qualification')
    require(claims['request_sha256']==digest(r) and claims['snapshot_hash']==s.hash and
            claims['candidate_id']==r['candidate_id'] and claims['transaction_id']==r['transaction_id'],
            'certificate snapshot/candidate/transaction mismatch')
    require(claims['backend']==r['backend'] and len(claims['backend']['parameters'])>0,
            'Native FCU parameter/backend binding missing')
    receipt=proof['consumption']
    require(receipt['status']=='VALID_OPERATIONAL_PREDICTION' and receipt['certificate_sha256']==pin,
            'current issuer consumption rejected')
    # Time/model are rechecked by TDOC as well. A field in the request cannot
    # choose an issuer, override a clock, or install a model.
    from layer6.timebase import monotonic_domain
    _check_time(c,time.time(),time.monotonic(),monotonic_domain())
    p=proof['prediction']
    require(p['request_sha256']==digest(r) and p['uncertainty']['kind']=='bounded',
            'bounded current Native prediction required')
    require(p['components']==VECTOR_SPECS['plant_state'][1]+VECTOR_SPECS['post_mixer_pwm'][1] and
            p['units']==VECTOR_SPECS['plant_state'][2]+VECTOR_SPECS['post_mixer_pwm'][2],
            'Native output coordinates/units are not the declared physical contract')
    require(p['coordinate_origin_sha256']==s.context['coordinate_origin_sha256']==claims['coordinate_origin_sha256'],
            'Native/Layer3 coordinate origin mismatch')
    require(claims['prediction_sha256']==digest(p),'prediction differs from qualified certificate')
    require(p['uncertainty']['qualification_artifact_sha256']==claims['qualification_artifact_sha256'] and
            len(claims['qualification_artifact_sha256'])==64,'uncertainty qualification lineage missing')
    expected=sorted(set([commit]+[(i+1)*dt for i in range(len(plan))]))
    require(len(p['tube'])==len(expected),'first move/horizon coverage missing')
    for row,offset in zip(p['tube'],expected):
        require(finite(row['offset_s']) and abs(row['offset_s']-offset)<1e-9,'wrong Native source-time grid')
        require(row['frame']=='NED_BODY_FRD_AND_PWM_US' and vector(row['lower'],17) and vector(row['upper'],17)
                and vector(row['center'],17),
                'Native bounded tube units/dimension')
        require(all(a<=x<=b for a,x,b in zip(row['lower'],row['center'],row['upper'])),'empty Native output bound')
        require(abs(sum(q*q for q in row['center'][6:10])-1.)<=.002,'invalid predicted body quaternion')
    safety=s.constraints['native_safety_policy']
    require(safety['schema']=='Native-pilot-safety-envelope-v1' and
            vector(safety['state_lower'],17) and vector(safety['state_upper'],17),
            'current independent safety envelope missing')
    require(all(all(a<=x<=y<=b for a,x,y,b in zip(safety['state_lower'],row['lower'],
            row['upper'],safety['state_upper'])) for row in p['tube']),
            'Layer4 Native prediction infeasible within current safety envelope')
    require(isinstance(p['objective_vector'],dict) and bool(p['objective_vector']) and
            all(finite(v) for v in p['objective_vector'].values()),'Native objective evidence missing/nonfinite')
    require(all(k in p['objective_vector'] for k in ('mission','time','energy','risk','uncertainty',
        'consistency','margin','control_effort','sensitivity')),'incomplete Native objective evidence; no zero fill')
    require(finite(p['risk_upper_bound']) and 0<=p['risk_upper_bound']<=1 and
            bool(p['risk_derivation']) and len(p['risk_evidence_sha256'])==64,'risk evidence unavailable')
    require(isinstance(p['network_trajectory'],list) and len(p['network_trajectory'])==len(plan),
            'current predicted network trajectory missing')
    for row in p['network_trajectory']:
        require(all(finite(row.get(k)) for k in ('J_C','mu')),'network metrics unavailable; no zero fill')
    require(claims['network_trajectory_sha256']==digest(p['network_trajectory']),
            'network evidence not bound to certificate')
    return s,p,c


def verify_operational(engine,request):
    base={'schema':RESULT_SCHEMA,'verified_candidates':[],'candidates':[],
          'verification':{'verified':False,'reason':None},'flight_authorized':False,
          'actuator_command':False,'network_commit':False,'prediction':None}
    try:
        service=getattr(engine,'native_execution_service',None)
        require(service is not None,'TDOC qualified Native execution service not bound')
        proof=service.evaluate(deepcopy(request))
        s,p,c=validate_execution_proof(request,proof,engine.config)
        radii=[[max(x-a,b-x) for a,x,b in zip(row['lower'],row['center'],row['upper'])] for row in p['tube']]
        # n*diag(r^2) bounds a box-supported error second moment even with
        # unknown correlations (Cauchy-Schwarz); it is not a fitted covariance.
        uncertainty=[[[17*r[i]*r[i] if i==j else 0. for j in range(17)] for i in range(17)] for r in radii]
        candidate={'candidate_id':request['candidate_id'],'snapshot_version':s.version,'snapshot_hash':s.hash,
            'actions':[deepcopy(row['u_cmd']) for row in request['pilot_plan']],
            'candidate_verified':True,'feasible':True,'violation_probability':p['risk_upper_bound'],
            'network_trajectory':deepcopy(p['network_trajectory']),'uncertainty':uncertainty,
            'objective_vector':deepcopy(p['objective_vector']),'normalized_objectives':{},
            'verification':{'verified':True,'verification_scope':SCOPE,'certificate_id':digest(c),
                            'verification_origin':'CURRENT_NATIVE_OPERATIONAL_ISSUER'},
            'provenance':{'verification_scope':SCOPE,'command_domain':DOMAIN,'qlpv_correction_applied':False}}
        base.update(candidates=[candidate],verified_candidates=[deepcopy(candidate)],prediction=deepcopy(p),
            verification={'verified':True,'reason':'current bounded Native qualification rechecked'},
            request=deepcopy(request),proof=deepcopy(proof),snapshot_version=s.version,snapshot_hash=s.hash,
            snapshot_epoch_s=s.context['snapshot_epoch_s'])
    except Exception as exc:
        base['verification']['reason']=str(exc)
    return NativeOperationalResult(base)
