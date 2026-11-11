from __future__ import annotations
import time, uuid
from typing import Any
from .config import Layer5Config
from .utils import sha256_obj

class GovernanceManager:
    def __init__(self,config:Layer5Config): self.config=config
    def authorize(self,mode_info:dict[str,Any],candidate_id:str|None,snapshot_version:str,snapshot_hash:str,confidence:float,confidence_parts:dict[str,float],human_approver:str|None=None,
                  decision_weights:dict[str,float]|None=None,policy:dict[str,Any]|None=None,evidence_type:str|None=None,explanation:dict[str,Any]|None=None)->dict[str,Any]:
        identity=bool(self.config.get("governance.identity_valid",True)); policy_valid=bool(self.config.get("governance.policy_valid",True)); authority=bool(self.config.get("governance.authority_valid",True))
        traceable=bool(candidate_id and snapshot_version and snapshot_hash); mode_commit=bool(mode_info.get("commit_authorized",False)); gov=bool(identity and policy_valid and authority and traceable and mode_commit)
        reasons=[]
        if not identity: reasons.append("identity_invalid")
        if not policy_valid: reasons.append("policy_invalid")
        if not authority: reasons.append("authority_invalid")
        if not traceable: reasons.append("decision_not_traceable")
        if not mode_commit:
            reasons.append("mode_commit_not_authorized")
            mode=str(mode_info.get("mode") or "UNKNOWN").upper()
            reasons.append(f"resolved_mode_{mode}")
            if bool(mode_info.get("human_required")) and not bool(mode_info.get("human_approval")):
                reasons.append("human_approval_required")
        decision_id="L5D-"+uuid.uuid4().hex[:16]; et=evidence_type or self.config.get("governance.evidence_type","simulation")
        prov={"decisionID":decision_id,"candidateID":candidate_id,"snapshot_version":snapshot_version,"snapshot_hash":snapshot_hash,
              "policyVersion":((policy or {}).get("theta") or {}).get("version",self.config.get("governance.policy_version")),"policyDigest":(policy or {}).get("digest"),
              "modelSetID":self.config.get("governance.model_set_id"),"decision_weights":decision_weights or self.config.get("objectives.guidance_weights",{}),
              "decision_confidence":confidence,"confidence_components":confidence_parts,"EvidenceType":et,"humanApprover":human_approver,"decision_time_epoch_s":time.time(),
              "network_commit":False,"model_retraining":False,"direct_pwm":False,"source_layer":"Layer5","traceable":traceable,"authorized":bool(identity and authority),"policy_compliant":policy_valid,
              "safety_decision_not_execution_safety":True,"layer6_must_revalidate":{"actuator":True,"range":True,"unit":True,"timing":True,"health":True,"acknowledgment":True,"gateway":True}}
        if explanation is not None: prov["explanation_sha256"]=sha256_obj(explanation)
        return {"gate":gov,"identity":identity,"policy":policy_valid,"authority":authority,"traceable":traceable,"mode_commit":mode_commit,"reasons":reasons,"decision_id":decision_id,"provenance":prov}
