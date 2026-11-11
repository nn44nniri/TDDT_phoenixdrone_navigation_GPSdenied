from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any

@dataclass
class DependencyCheck:
    name: str; root: str|None; ok: bool; required: list[str]; missing: list[str]; note: str=""; used_by: list[str]|None=None
    def to_dict(self): return asdict(self)

_EXPECTED={
 "TDDT_LAYER3_ROOT":(["src/layer3","HOW_USAGE.md"],["FullFidelityVerifier","Layer3FMURuntime adapter"]),
 "TDDT_TWINS_ROOT":(["fmus/MESA_NAV.fmu","fmus/WingMPET.fmu","python/twins_fmi","config/phoenix_drone_v3.yaml","resources/tddt_layer2/MESA_NAV/tep/tep.csv","resources/tddt_layer2/MESA_NAV/tep/manifest.json","resources/tddt_layer2/WingMPET/tep/tep.csv","resources/tddt_layer2/WingMPET/tep/manifest.json"],["VTOLProfile","Layer3FMURuntime/MESA_NAV/WingMPET verification","TEP contract/provenance validation via Layer2"]),
 "TDDT_CLIMATE_ROOT":(["python","resources/tddt_layer2/tep"],["Layer3FMURuntime/MicroMetTwin full-fidelity verification","Climate TEP contract/provenance validation via Layer2"]),
 "TDDT_TDOC_ROOT":(["docs"],["Layer3 strict TDOC boundary during full-fidelity verification"]),
 "TDDT_SARG_PACKAGE_ROOT":(["generated/SARG/sarg_records.csv","generated/SARG/sarg_lookup_index.csv","generated/SARG/metadata.json"],["KnowledgeContext contract/provenance validation; authoritative selection remains Layer3 guidance"]),
 "TDDT_BOULDER_ROOT":(["outputs/region_v1_map/CRP/crp_cluster_catalog.csv","outputs/region_v1_map/CRP/crp_runtime_statistics.json","outputs/region_v1_map/terrain_obstacles.tif"],["KnowledgeContext CRP/map contract validation; processed context comes from Layer3 snapshot"]),
}

def check_dependencies(config) -> dict[str,Any]:
    out=[]
    for key,(rel,used_by) in _EXPECTED.items():
        p=config.env_path(key); missing=[] if p else list(rel)
        if p: missing=[r for r in rel if not (p/r).exists()]
        out.append(DependencyCheck(key,str(p) if p else None,not missing,rel,missing,"external; never vendored",used_by).to_dict())
    return {"ok":all(x["ok"] for x in out),"dependencies":out,"vendor_policy":"external-roots-only",
            "runtime_policy":"Layer2/Climate/TDOC are reached through Layer3 full-fidelity runtime; TEP is inherited through Layer2/Layer3 evidence; SARG/CRP are consumed primarily through Layer3 snapshot/guidance and read directly only for contract/provenance validation."}
