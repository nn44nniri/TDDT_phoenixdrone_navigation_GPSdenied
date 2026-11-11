from __future__ import annotations
import os
from pathlib import Path
import pytest
from layer3 import Layer3Config
from site_forecast_fixture import test_epoch

ROOT = Path(__file__).resolve().parents[1]
_DEPS_RAW = os.environ.get("L3_TEST_DEPS_ROOT", "").strip()
DEPS = Path(_DEPS_RAW).resolve() if _DEPS_RAW else None

@pytest.fixture
def cfg_factory(tmp_path):
    if DEPS is None or not DEPS.is_dir():
        pytest.skip("L3_TEST_DEPS_ROOT with the five external dependency packages is required")
    def make(*, meso=2, slow=2):
        cfg = Layer3Config.default(ROOT)
        mapping = {
            "twins_root": DEPS/"twins_layer2_v20260820_1233",
            "climate_root": DEPS/"climate_twin_v20260911_0922",
            "tdoc_root": DEPS/"TDOC_ISO_Universal_Unit_Conversion_Standard20260824_0914",
            "sarg_package_root": DEPS/"SARG_v20260821_0945",
            "sarg_root": DEPS/"SARG_v20260821_0945/generated/SARG",
            "boulder_root": DEPS/"boulder_multilayer_map_builder_v20260814_1215",
            "map_root": DEPS/"boulder_multilayer_map_builder_v20260814_1215/outputs/region_v1_map",
            "crp_root": DEPS/"boulder_multilayer_map_builder_v20260814_1215/outputs/region_v1_map/CRP",
            "ss_kstore": tmp_path/"ss.jsonl", "setd_kstore": tmp_path/"setd.jsonl",
            "snapshots_dir": tmp_path/"snap", "versions_dir": tmp_path/"versions",
            "provenance_log": tmp_path/"provenance.jsonl", "tdoc_health": tmp_path/"tdoc_health.json",
        }
        for k,v in mapping.items(): setattr(cfg.paths,k,str(v))
        cfg.time.meso_fast_steps=meso; cfg.time.slow_meso_windows=slow
        cfg.time.mission_epoch_utc=test_epoch()
        cfg.runtime["persist_every_snapshot"]=False
        return cfg
    return make

# Strict TDOC-boundary helpers used by runtime integration/conformance tests.
def sig(value, unit, frame, semantic, *, uncertainty=None, time_reference=None, source="pytest"):
    d={"value":value,"unit":unit,"frame":frame,"semantic":semantic,"uncertainty":uncertainty,"source":source,
       "provenance":{"origin":source,"contract":"explicit_test_envelope"}}
    if time_reference is not None: d["time_reference"]=time_reference
    return d

def strict_climate(lat=40.017498782173554, lon=-105.28999780562658, time=None, agl=100.0):
    time = time if time is not None else test_epoch()
    return {
      "time":sig(time,"ISO8601","SCALAR","time",time_reference="UTC"),
      "latitude":sig(lat,"deg","WGS84","latitude"),
      "longitude":sig(lon,"deg","WGS84","longitude"),
      "elevation_ground_level":sig(agl,"m","AGL","elevation_ground_level"),
    }

def strict_context(target=(100.0,0.0,1800.0), *, geodetic_origin=None):
    d={"target_enu_m":sig(list(target),"m","ENU","target_position")}
    if geodetic_origin is not None:
        lat,lon,h=geodetic_origin
        d["geodetic_origin"]={
          "latitude":sig(lat,"deg","WGS84","latitude"),
          "longitude":sig(lon,"deg","WGS84","longitude"),
          "altitude_msl_m":sig(h,"m","MSL","altitude_msl_m"),
        }
    return d

def strict_anchor(*, residual=0.0, jac_n=0.1, jac_e=0.1, wind_enabled=True, pressure_enabled=True, nis_gate=None):
    wind={"enabled":wind_enabled,
          "terrain_flow_anchor_residual":sig(residual,"1","MAP","terrain_flow_anchor_residual"),
          "jacobian_n":sig(jac_n,"1","NED","jacobian_n"),
          "jacobian_e":sig(jac_e,"1","NED","jacobian_e")}
    if nis_gate is not None: wind["nis_gate"]=sig(nis_gate,"1","SCALAR","nis_gate")
    return {"wind":wind,"pressure":{"enabled":pressure_enabled}}
