from __future__ import annotations
import json, time
from .hashing import sha256_obj,chain_hash


class CandidateRegistry:
    def __init__(self,config):
        self.path=config.package_root/config.get("registry.path","runtime/candidates/candidates.jsonl"); self.prov=config.package_root/config.get("registry.provenance_path","runtime/provenance.jsonl")
    def register(self,candidate,model_hash,param_hash,solver_hash, *, snapshot_time=None):
        return self.register_many([candidate],model_hash,param_hash,solver_hash,snapshot_time=snapshot_time)[0]

    def register_many(self,candidates,model_hash,param_hash,solver_hash, *, snapshot_time=None):
        """Sign and append one invocation with a single archival file open.

        Registry persistence is evidence, not a decision or safety gate.  Batching
        only removes repeated deep serialization/open-close work; each candidate
        retains its original hash and hash-chain contract.
        """
        rows=[]
        for candidate in candidates:
            d=candidate.to_dict(); result_hash=sha256_obj({k:v for k,v in d.items() if k not in ("signature","provenance")})
            pi={"snapshot_version":candidate.snapshot_version,"snapshot_hash":candidate.snapshot_hash,"snapshot_time":snapshot_time,
                "candidate_id":candidate.candidate_id,"scenario_id":candidate.scenario_id,"model_hash":model_hash,"parameter_hash":param_hash,
                "solver_hash":solver_hash,"result_hash":result_hash,"registry_time_unix_s":time.time()}
            sig=chain_hash(candidate.snapshot_hash,candidate.candidate_id,model_hash,param_hash,solver_hash,result_hash)
            candidate.provenance.update(pi); candidate.signature=sig
            # ``d`` intentionally shares the provenance mapping with candidate.
            # Scalars captured before signing must be refreshed explicitly.
            d["signature"]=sig
            rows.append(d)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.path.open("a",encoding="utf-8") as f:
            for d in rows:
                f.write(json.dumps(d,sort_keys=True,allow_nan=False)+"\n")
        return candidates
    def event(self,event):
        self.prov.parent.mkdir(parents=True,exist_ok=True)
        with self.prov.open("a",encoding="utf-8") as f: f.write(json.dumps(event,sort_keys=True,allow_nan=False)+"\n")
