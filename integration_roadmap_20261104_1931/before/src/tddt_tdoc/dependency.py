from __future__ import annotations
import importlib.util
from .unit_adapter import UnitAdapterBinding

class DependencyChecker:
    RUNTIME_MODULES=("yaml","numpy","pandas","plotly","pyproj","rasterio","shapely")
    SPECS={
      "TDDT_TWINS_ROOT":["fmus/MESA_NAV.fmu","fmus/WingMPET.fmu","python/twins_fmi"],
      "TDDT_CLIMATE_ROOT":["fmu/MicroMetTwin.fmu","python/tddt_layer2.py","python/estimate_climate.py"],
      "TDDT_BOULDER_ROOT":["outputs/region_v1_map"],
      "TDDT_SARG_PACKAGE_ROOT":["generated/SARG"],
      "TDDT_LAYER3_ROOT":["src/layer3","configs/configuration_boulder.yaml"],
      "TDDT_LAYER4_ROOT":["src/layer4","configs/configuration_boulder.yaml"],
      "TDDT_LAYER5_ROOT":["src/layer5/__init__.py","configs/configuration_phoenix.yaml"],
      "TDDT_LAYER7_ROOT":["src/tddt_layer7/__init__.py","src/tddt_layer7/tdoc_adapter.py","configs/default.json","schemas/tdoc_layer7_input.schema.json"],
    }
    LAYER6_FILES=("src/layer6/client.py","src/layer6/contracts.py","src/layer6/feedback.py","scripts/run_layer6_stack.sh")
    def __init__(self,config):
        self.config=config; self.unit_adapter=UnitAdapterBinding(config)
    def check(self):
        out={}; required_ok=True
        for key,rels in self.SPECS.items():
            p=self.config.path_value(key)
            missing=["root"] if p is None or not p.exists() else [r for r in rels if not (p/r).exists()]
            ok=bool(p and p.exists() and not missing)
            out[key]={"path":str(p) if p else None,"ok":ok,"missing":missing,"required":True}
            required_ok &= ok
        ua=self.unit_adapter.preflight()
        out["TDDT_UNIT_ADAPTER_ROOT"]={"path":ua.get("root"),"ok":bool(ua.get("ok")),"missing":ua.get("missing",[]),"required":True,
            "expected_package":ua.get("expected_package"),"version":(ua.get("api_probe") or {}).get("version"),"api_ok":(ua.get("api_probe") or {}).get("api_ok"),"smoke_ok":(ua.get("api_probe") or {}).get("smoke_ok")}
        required_ok &= bool(ua.get("ok"))
        mods={name: bool(importlib.util.find_spec(name)) for name in self.RUNTIME_MODULES}; missing_mods=[name for name,ok in mods.items() if not ok]
        out["PYTHON_RUNTIME"]={"ok":not missing_mods,"modules":mods,"missing":missing_mods,"required":True}; required_ok &= not missing_mods
        p6=self.config.path_value("TDDT_LAYER6_ROOT")
        missing=["root"] if not p6 or not p6.is_dir() else [r for r in self.LAYER6_FILES if not (p6/r).exists()]
        out["TDDT_LAYER6_ROOT"]={"path":str(p6) if p6 else None,"ok":not missing,"missing":missing,
            "required":False,"required_when":"ONLINE_JSBSIM","status":"MODE_DEPENDENT_NATIVE_LAYER6"}
        return {"required_ok":required_ok,"dependencies":out,"unit_adapter":ua}
