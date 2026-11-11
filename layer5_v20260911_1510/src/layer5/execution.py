from __future__ import annotations
from typing import Any
from .config import Layer5Config
from .models import Layer6ActionPackage
from .utils import sha256_obj


def mission_execution_intent(phase, status):
    """Layer5 owns mode/lifecycle intent; the FCU must confirm it before sticks."""
    modes={'on_ground':'QHOVER','vertical_takeoff':'QHOVER','hover':'QHOVER',
           'transition_forward':'FBWA','climb':'FBWA','cruise':'FBWA','turn':'FBWA',
           'approach':'FBWA','transition_back':'QHOVER','vertical_landing':'QHOVER',
           'touchdown':'QHOVER'}
    if phase not in modes:
        raise ValueError('unsupported SITL mission phase')
    actions=[]
    if phase=='touchdown':
        return {'requested_pilot_mode':modes[phase],
                'discrete':['DISARM'] if status.get('armed') is True else []}
    if status.get('armed') is False:
        if phase not in {'on_ground','vertical_takeoff'}:
            raise ValueError('disarmed FCU outside ground/takeoff phase')
        # The execution consumer independently checks ground contact and prearm.
        if float(status.get('throttle',-1))!=0.0:
            raise ValueError('ARM requires observed zero pilot throttle')
        actions=['ARM']
    return {'requested_pilot_mode':modes[phase],'discrete':actions}


class _SITLMissionAuthorization:
    """Process-local assembly token created after the real MPC safety gates."""
    def __init__(self, args, *, selected, final_gate, intent):
        if not (final_gate and selected.ci.ok and selected.adm.ok and selected.vp.ok
                and list(args['command'])==list(selected.solver['corrected_actions'][0])):
            raise ValueError('SITL assembly requires current verified MPC first move')
        self.args_hash=sha256_obj(args)
        self.intent=dict(intent)

    def validate(self,args):
        if sha256_obj(args)!=self.args_hash:
            raise ValueError('SITL assembly changed after MPC authorization')


class _SITLMissionPackage(Layer6ActionPackage):
    def to_dict(self):
        result=super().to_dict()
        result['execution_intent']=dict(self.provenance['execution_intent'])
        result['phase']=self.execution_constraints['tdoc_phase']
        return result

class _NativeActionPackage(Layer6ActionPackage):
    def to_dict(self):
        value=super().to_dict()
        value.update(self.provenance['native_execution_context'])
        return value

class DecisionExplainer:
    """Constructs the explicit reason/evidence package required by the base Layer-5 document."""
    def build(self, *, selected, ranking:list[str], recommendation:dict[str,Any], confidence:float,
              active_constraints:dict[str,Any], human:dict[str,Any], policy:dict[str,Any])->dict[str,Any]:
        net=selected.network_trajectory or []
        jc=max([float(n.get("J_C",0.0)) for n in net if isinstance(n,dict)] or [0.0])
        mu=min([float(n.get("mu",0.0)) for n in net if isinstance(n,dict)] or [0.0])
        u=(selected.adm.metrics if selected.adm else {}).get("mean_cov_trace",0.0)
        return {"id_selected":selected.candidate_id,"rank":ranking.index(selected.candidate_id)+1 if selected.candidate_id in ranking else None,
                "ranking":ranking,"tradeoff":recommendation.get("tradeoff",{}),"risk":selected.violation_probability,
                "uncertainty":u,"J_C":jc,"mu":mu,"constraints":active_constraints,"human":human,
                "decision_confidence":confidence,"policy_version":policy.get("theta",{}).get("version"),
                "explainability_schema":"L5-reason-v1"}

class Layer6Assembler:
    """Single owner of the formal Layer5->Layer6 execution contract (§21 / base §28)."""
    def __init__(self,config:Layer5Config): self.config=config
    def assemble(self, command:list[float], actuator_targets:list[str], constraints:dict[str,Any], safety_policy:dict[str,Any],
                 apply_time:float, deadline_time:float, confidence:float, provenance:dict[str,Any],
                 *,execution_authorization=None)->Layer6ActionPackage:
        if ((provenance or {}).get('verification_scope')=='NATIVE_EXACT_REFERENCE_REVIEW' or
                (provenance or {}).get('certificate_usage')=='EXPERIMENTAL_REFERENCE_REVIEW'):
            raise RuntimeError('native_reference_review_has_no_FINAL_or_D_exec_authority')
        if execution_authorization is not None:
            if isinstance(execution_authorization,_SITLMissionAuthorization):
                args=dict(command=command,actuator_targets=actuator_targets,constraints=constraints,
                    safety_policy=safety_policy,apply_time=apply_time,deadline_time=deadline_time,
                    confidence=confidence,provenance=provenance)
                execution_authorization.validate(args)
                return _SITLMissionPackage(list(command),list(actuator_targets),dict(constraints),
                    dict(safety_policy),apply_time,deadline_time,confidence,dict(provenance),
                    'Layer5-SITL-closed-loop-D-exec-v1')
            from .native_operational import _AssemblyAuthorization
            from layer4.native_operational import finite, require, vector
            import time
            require(isinstance(execution_authorization,_AssemblyAuthorization),'untrusted Native assembly authorization')
            args=dict(command=command,actuator_targets=actuator_targets,constraints=constraints,
                safety_policy=safety_policy,apply_time=apply_time,deadline_time=deadline_time,
                confidence=confidence,provenance=provenance)
            execution_authorization.validate(args)
            require(vector(command,4) and all(finite(v) for v in (apply_time,deadline_time,confidence)),
                    'nonfinite or malformed Native action package')
            require(0<=confidence<=1 and 0<=apply_time<=time.time()<deadline_time,'Native package validity window')
            require(bool(actuator_targets) and constraints.get('direct_pwm') is False and
                    all(safety_policy.get(k) is True for k in ('psf','rta')),'Native package safety/domain invalid')
            return _NativeActionPackage(list(command),list(actuator_targets),dict(constraints),dict(safety_policy),
                apply_time,deadline_time,confidence,dict(provenance),'Layer5-Native-ground-D-exec-v1')
        # The source release intentionally has no validated provider capable of
        # replaying ArduPilot pilot -> FCU controller -> post-mixer actuator.
        # An untrusted provenance flag must not re-enable native actuation.
        raise RuntimeError("native_pilot_controller_prediction_unavailable")

class Layer7EvidenceRouter:
    """Builds versioned evidence only; it has no persistent-update authority."""
    def build(self, *, decision_id:str, candidate_id:str|None, result_kind:str, observations:dict[str,Any]|None,
              truth:dict[str,Any]|None, events:dict[str,Any]|None, knowledge_faults:list[str]|None=None)->dict[str,Any]:
        payload={"schema":"L5-to-L7-evidence-v1","decision_id":decision_id,"candidate_id":candidate_id,"result_kind":result_kind,
                 "sensor_observation":observations,"environment_truth":truth,"simulator_event":events,"knowledge_faults":knowledge_faults or [],
                 "persistent_update_authority":"Layer7","layer5_persistent_write":False}
        payload["sha256"]=sha256_obj(payload); return payload
