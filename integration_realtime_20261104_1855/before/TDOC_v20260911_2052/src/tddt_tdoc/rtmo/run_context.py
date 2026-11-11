from __future__ import annotations
from dataclasses import dataclass,asdict
import uuid
@dataclass(frozen=True)
class RunContext:
    run_id:str; episode_id:str; mission_version:str; knowledge_version:str|None=None; config_version:str|None=None
    @classmethod
    def create(cls,episode_id:str,*,knowledge_version=None,config_version=None,mission_version='RTMO-11PHASE-v2'):
        return cls('rtmo-'+uuid.uuid4().hex,episode_id,mission_version,knowledge_version,config_version)
    def cycle(self,cycle_id:int,phase:str)->dict:
        return {**asdict(self),'cycle_id':int(cycle_id),'phase_id':phase}
