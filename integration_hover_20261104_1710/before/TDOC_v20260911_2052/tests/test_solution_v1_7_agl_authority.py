from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import rasterio

from tddt_tdoc.adapters.layer3 import Layer3Adapter
from tddt_tdoc.terrain import sample_bilinear


class _Cfg:
    def __init__(self, base:Path):
        self.base=base
    def path_value(self,key):
        if key=="TDDT_LAYER3_ROOT": return self.base/"layer3_v20260911_1131"
        if key=="TDDT_MAP_ROOT": return self.base/"boulder_multilayer_map_builder_v20260814_1215"/"outputs/region_v1_map"
        return None


def _adapter(base):
    return Layer3Adapter(_Cfg(base),unit_adapter=SimpleNamespace())


def test_operational_agl_is_derived_from_mesa_plus_dem_only():
    base=Path(__file__).resolve().parents[2]
    ad=_adapter(base)
    dem=ad.config.path_value("TDDT_MAP_ROOT")/"terrain_obstacles.tif"
    with rasterio.open(dem) as ds:
        east=(ds.bounds.left+ds.bounds.right)/2; north=(ds.bounds.bottom+ds.bounds.top)/2
        terrain=sample_bilinear(ds,east,north)
    desired=12.5
    packets={"MESA_NAV":{"state":{"position_n":north,"position_e":east,"position_d":-(terrain+desired)}}}
    ctx={"online_state_ingress":{"ptlib_ingress":{"state":{"agl_m":999999.0}},"simulation_only":True}}
    out=ad._augment_operational_agl(ctx,packets)
    assert abs(out["altitude_agl_m"]-desired)<1e-4
    assert out["agl_available"] is True
    assert out["agl_authority"]["reason"]=="MESA_STD_PLUS_DEM"
    assert out["agl_authority"]["terrain_interpolation"]=="BILINEAR_PIXEL_CENTRE"
    assert out["agl_authority"]["simulator_truth_used"] is False
    assert out["altitude_agl_m"] != 999999.0


def test_ptlib_or_simulator_agl_is_never_fallback_navigation_authority():
    base=Path(__file__).resolve().parents[2]
    ad=_adapter(base)
    ctx={"online_state_ingress":{"ptlib_ingress":{"state":{"agl_m":123.0}},"simulation_only":True}}
    out=ad._augment_operational_agl(ctx,{"MESA_NAV":{"state":{}}})
    assert out["agl_available"] is False
    assert "altitude_agl_m" not in out
    assert out["agl_authority"]["valid"] is False
    assert out["agl_authority"]["simulator_truth_used"] is False


def test_outside_dem_coverage_is_explicitly_unavailable():
    base=Path(__file__).resolve().parents[2]
    ad=_adapter(base)
    packets={"MESA_NAV":{"state":{"position_n":0.0,"position_e":0.0,"position_d":-1800.0}}}
    out=ad._augment_operational_agl({},packets)
    assert out["agl_available"] is False
    assert "altitude_agl_m" not in out
    assert out["agl_authority"]["reason"]=="mesa_position_outside_dem_coverage"


def test_operational_terrain_is_continuous_across_a_pixel_boundary(tmp_path):
    import numpy as np
    from rasterio.transform import from_origin
    dem=tmp_path/"terrain_obstacles.tif"
    with rasterio.open(dem,"w",driver="GTiff",width=2,height=2,count=1,dtype="float32",
                       crs="EPSG:32613",transform=from_origin(0,2,1,1),nodata=-9999.0) as ds:
        ds.write(np.asarray([[0.0,10.0],[20.0,30.0]],dtype=np.float32),1)
    with rasterio.open(dem) as ds:
        centre=sample_bilinear(ds,1.0,1.0)
        before=sample_bilinear(ds,1.0,1.0+1e-4)
        after=sample_bilinear(ds,1.0,1.0-1e-4)
    assert centre == 15.0
    assert abs(before-after) < 0.01
