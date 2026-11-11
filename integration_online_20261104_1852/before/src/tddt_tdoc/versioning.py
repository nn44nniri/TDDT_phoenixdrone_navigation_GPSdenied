from __future__ import annotations
import json
from pathlib import Path
from .utils import utc_now
class VersionManager:
    def __init__(self,root:Path,version:str):
        self.root=root; root.mkdir(parents=True,exist_ok=True); self.active=version; self.safe=version; self._write()
    def _write(self):
        (self.root/"active.json").write_text(json.dumps({"version":self.active,"time":utc_now()},indent=2),encoding="utf-8")
        (self.root/"safe.json").write_text(json.dumps({"version":self.safe,"time":utc_now()},indent=2),encoding="utf-8")
    def snapshot(self): return {"active":self.active,"safe":self.safe}
    def safe_commit(self,candidate:str,*,authorized:bool,validated:bool)->dict:
        if not (authorized and validated): return {"committed":False,"active":self.active,"reason":"authorization_or_validation_failed"}
        self.active=str(candidate); self.safe=self.active; self._write(); return {"committed":True,"active":self.active}

    def activate_external(self, version:str, *, mark_safe:bool=False)->dict:
        previous=self.active; self.active=str(version)
        if mark_safe: self.safe=self.active
        self._write(); return {"activated":True,"previous":previous,"active":self.active,"safe":self.safe}
    def restore_external(self, version:str)->dict:
        previous=self.active; self.active=str(version); self._write(); return {"restored":True,"previous":previous,"active":self.active,"safe":self.safe}

    def rollback(self)->dict:
        self.active=self.safe; self._write(); return {"rolled_back":True,"active":self.active}
