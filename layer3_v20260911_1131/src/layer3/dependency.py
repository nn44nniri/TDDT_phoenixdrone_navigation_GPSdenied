from __future__ import annotations

import ast
import csv
import importlib
import importlib.util
import inspect
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Layer3Config


@dataclass(frozen=True)
class DependencySpec:
    key: str
    root: Path
    expected_basename: str
    required: tuple[str, ...]


class DependencyRegistry:
    """Strict registry for the five externally deployed Layer-3 dependencies.

    Validation covers exact package version/basename, files explicitly required
    by each HOW_USAGE contract, callable API signatures used by Layer 3, and the
    canonical SARG/CRP dataset invariants. Dependencies remain external.
    """

    def __init__(self, config: Layer3Config):
        self.config = config
        self.specs = {
            "twins": DependencySpec("twins", config.twins_root, config.EXPECTED_DEPENDENCY_BASENAMES["twins"], (
                "HOW_USAGE.md", "fmus/MESA_NAV.fmu", "fmus/WingMPET.fmu", "config/phoenix_drone_v3.yaml",
                "resources/MESA_NAV_signal_schema.json", "resources/WingMPET_signal_schema.json",
                "resources/tddt_layer2/MESA_NAV/twin_contract.json", "resources/tddt_layer2/WingMPET/twin_contract.json",
                "resources/tddt_layer2/MESA_NAV/twin_output_schema.json", "resources/tddt_layer2/WingMPET/twin_output_schema.json",
                "resources/tddt_layer2/MESA_NAV/tep/tep.csv", "resources/tddt_layer2/WingMPET/tep/tep.csv",
                "resources/tddt_layer2/MESA_NAV/tep/VERSION", "resources/tddt_layer2/WingMPET/tep/VERSION",
                "python/twins_fmi/domain_twin.py", "python/twins_fmi/formula_fmi.py",
            )),
            "climate": DependencySpec("climate", config.climate_root, config.EXPECTED_DEPENDENCY_BASENAMES["climate"], (
                "HOW_USAGE.md", "fmu/MicroMetTwin.fmu", "python/estimate_climate.py", "python/tddt_layer2.py",
                "resources/tddt_layer2/twin_contract.json", "resources/tddt_layer2/twin_output_schema.json",
                "resources/tddt_layer2/tep/tep.csv", "resources/tddt_layer2/tep/VERSION",
            )),
            "tdoc": DependencySpec("tdoc", config.tdoc_root, config.EXPECTED_DEPENDENCY_BASENAMES["tdoc"], (
                "HOW_USAGE.md", "docs/TDOC_ISO_Universal_Unit_Conversion_Standard_v2.md",
                "src/tdoc_unit_converter/converter.py", "src/tdoc_unit_converter/adapters.py",
            )),
            "sarg": DependencySpec("sarg", config.sarg_package_root, config.EXPECTED_DEPENDENCY_BASENAMES["sarg"], (
                "HOW_USAGE.md", "sarg_builder/__init__.py", "sarg_builder/selector.py", "generated/SARG/schema.json",
                "generated/SARG/metadata.json", "generated/SARG/sarg_patterns.csv", "generated/SARG/sarg_records.csv",
                "generated/SARG/sarg_lookup_index.csv", "generated/SARG/sarg_stage_summary.csv",
                "generated/SARG/scenario_climate_contexts.csv", "generated/SARG/status_codes.csv",
                "generated/SARG/fmi_execution_trace.csv", "generated/SARG/scenarios.csv",
                "generated/SARG/region_context.json", "generated/SARG/region_v1.geojson",
                "generated/SARG/crs_rejections.csv", "generated/SARG/sarg_samples.csv.gz", "generated/SARG/routes",
            )),
            "boulder": DependencySpec("boulder", config.boulder_root, config.EXPECTED_DEPENDENCY_BASENAMES["boulder"], (
                "HOW_USAGE.md", "map_mosaic_query.py", "region/region_v1.geojson", "outputs/region_v1_map/terrain_obstacles.tif",
                "outputs/region_v1_map/climate_contexts.tif", "outputs/region_v1_map/magnetic_anomaly.tif",
                "outputs/region_v1_map/metadata.json", "outputs/region_v1_map/CRP/crp_runtime_statistics.json",
                "outputs/region_v1_map/CRP/crp_cluster_catalog.csv", "outputs/region_v1_map/CRP/crp_cell_assignments.csv.gz",
                "outputs/region_v1_map/CRP/crp_cluster_map.tif", "outputs/region_v1_map/CRP/metadata.json",
            )),
        }
        self.last_status: dict[str, Any] = {}

    def _expose_paths(self) -> None:
        for root in (self.config.twins_root / "python", self.config.climate_root / "python",
                     self.config.tdoc_root / "src", self.config.sarg_package_root, self.config.boulder_root):
            if root.is_dir() and str(root) not in sys.path:
                sys.path.insert(0, str(root))

    def validate_python_runtime(self, *, strict: bool = True) -> dict[str, Any]:
        required = {"numpy":"numpy>=1.24", "yaml":"PyYAML>=6.0", "rasterio":"rasterio>=1.3",
                    "pyproj":"pyproj>=3.5", "pandas":"pandas>=2.0", "plotly":"plotly>=6.0"}
        if bool(self.config.runtime.get("use_boulder_runtime_sampling", True)):
            required["shapely"] = "shapely>=2.0"
        details = {m:{"pip_requirement":r,"available":importlib.util.find_spec(m) is not None} for m,r in required.items()}
        missing=[v["pip_requirement"] for v in details.values() if not v["available"]]
        status={"all_ok":not missing,"missing":missing,"details":details}
        if strict and missing:
            raise RuntimeError("Layer-3 Python runtime dependency validation failed. Missing: "+", ".join(missing))
        return status

    def validate(self, *, strict: bool = True) -> dict[str, Any]:
        status: dict[str, Any] = {}; errors: list[str] = []
        for key,spec in self.specs.items():
            missing=[rel for rel in spec.required if not (spec.root/rel).exists()]
            basename_ok=(spec.root.name==spec.expected_basename)
            if key=="climate": basename_ok=spec.root.name.startswith("climate_twin_v")
            ok=spec.root.is_dir() and basename_ok and not missing
            status[key]={"root":str(spec.root),"expected_basename":spec.expected_basename,"actual_basename":spec.root.name,
                         "exists":spec.root.is_dir(),"basename_ok":basename_ok,"missing":missing,"ok":ok,
                         "how_usage":str(spec.root/"HOW_USAGE.md")}
            if not ok: errors.append(f"{key}: root={spec.root}; basename_ok={basename_ok}; missing={missing}")
        # Climate forcing is a runtime dataset and may live in the integrated-root cache.
        climate_forcing = None
        env_forcing = __import__("os").environ.get("TDDT_CLIMATE_FORCING_FILE", "").strip()
        if env_forcing:
            climate_forcing = Path(env_forcing).expanduser().resolve()
        else:
            cache = __import__("os").environ.get("TDDT_CLIMATE_INTERNET_DATASETS_ROOT", "").strip()
            if cache:
                mp=Path(cache).expanduser().resolve()/"tdoc_active_forcing.json"
                if mp.is_file():
                    try:
                        raw=str((json.loads(mp.read_text()) or {}).get("file") or "").strip(); q=Path(raw)
                        climate_forcing=(q if q.is_absolute() else mp.parent/q).resolve()
                    except Exception: climate_forcing=None
            if climate_forcing is None:
                climate_forcing=(self.config.climate_root/"climate_dataset"/"openmeteo_24h.csv").resolve()
        forcing_ok=bool(climate_forcing and climate_forcing.is_file())
        status["climate_forcing"]={"path":str(climate_forcing) if climate_forcing else None,"ok":forcing_ok,"source":"environment/central-cache/package-fallback"}
        if not forcing_ok: errors.append(f"climate forcing unavailable: {climate_forcing}")
        status["all_ok"]=not errors; status["errors"]=errors; self.last_status=status
        if strict and errors: raise RuntimeError("Layer-3 dependency validation failed: "+" | ".join(errors))
        return status

    @staticmethod
    def _has_params(obj: Any, names: set[str]) -> tuple[bool, list[str], str]:
        sig=inspect.signature(obj); params=set(sig.parameters)
        missing=sorted(names-params)
        return not missing, missing, str(sig)

    def validate_api_contracts(self, *, strict: bool = True) -> dict[str, Any]:
        self._expose_paths(); checks: dict[str,Any]={}; errors=[]
        try:
            from twins_fmi import DomainTwin, FormulaFMU  # type: ignore
            ok1,miss1,sig1=self._has_params(DomainTwin.evaluate_packet,{"operation_code","context","context_id","stage_id","inputs","freshness_age_s"})
            ok2,miss2,sig2=self._has_params(FormulaFMU.operation_signature,{"operation_code"})
            checks["twins"]={"ok":ok1 and ok2,"DomainTwin.evaluate_packet":sig1,"FormulaFMU.operation_signature":sig2,"missing":miss1+miss2}
        except Exception as exc: checks["twins"]={"ok":False,"error":str(exc)}
        try:
            ec=importlib.import_module("estimate_climate"); cl=importlib.import_module("tddt_layer2")
            ok1,miss1,sig1=self._has_params(ec.estimate,{"args"})
            ok2,miss2,sig2=self._has_params(cl.ClimateDomainTwin.packet_from_estimate,{"result","context_id","stage_id","operation_code","data_age_s"})
            ok3,miss3,sig3=self._has_params(cl.ClimateDomainTwin.match_tep,{"result"})
            checks["climate"]={"ok":ok1 and ok2 and ok3,"estimate":sig1,"packet_from_estimate":sig2,"match_tep":sig3,"missing":miss1+miss2+miss3}
        except Exception as exc: checks["climate"]={"ok":False,"error":str(exc)}
        try:
            from tdoc_unit_converter import UnitConverter, adapters  # type: ignore
            required=["enu_to_ned","ned_to_enu","ned_enu_covariance","frd_to_flu","flu_to_frd","frd_flu_covariance",
                      "wgs84_to_ecef","ecef_to_wgs84","msl_to_agl","agl_to_msl","pressure_reference",
                      "pressure_to_log_ratio","log_ratio_to_pressure","validate_data_semantics","common_time_to_elapsed"]
            missing=[n for n in required if not callable(getattr(adapters,n,None))]
            okc,missc,sigc=self._has_params(UnitConverter.convert,{"value","from_unit","to_unit","uncertainty"})
            checks["tdoc"]={"ok":not missing and okc,"UnitConverter.convert":sigc,"missing_adapters":missing,"missing_params":missc}
        except Exception as exc: checks["tdoc"]={"ok":False,"error":str(exc)}
        try:
            import sarg_builder  # type: ignore
            ok1,miss1,sig1=self._has_params(sarg_builder.SARGSelector.select,{"regime_id","phase_id","crp_id"})
            ok2,miss2,sig2=self._has_params(sarg_builder.SARGSelector.select_candidates,{"phase_id","crp_id","regime_ids"})
            pk=tuple(sarg_builder.PRIMARY_KEY)
            checks["sarg"]={"ok":ok1 and ok2 and pk==("regime_id","phase_id","crp_id"),"PRIMARY_KEY":list(pk),
                            "select":sig1,"select_candidates":sig2,"missing":miss1+miss2}
        except Exception as exc: checks["sarg"]={"ok":False,"error":str(exc)}
        try:
            # Boulder and Climate intentionally ship some same-named helper modules
            # (for example region_geojson.py). Static API validation avoids global
            # sys.modules collisions; the runtime adapter imports Boulder in an
            # isolated path context and is exercised by integration tests.
            pyfile=self.config.boulder_root/"map_mosaic_query.py"
            symbols={x["name"] for x in self._public_symbols(pyfile)}
            req=["sample_file","point","time_bin","band_map"]; missing=[n for n in req if n not in symbols]
            checks["boulder"]={"ok":not missing,"public_runtime_api":req,"missing":missing,
                               "validation":"AST public-symbol check + runtime integration test"}
        except Exception as exc: checks["boulder"]={"ok":False,"error":str(exc)}
        for k,v in checks.items():
            if not v.get("ok"): errors.append(f"{k}: {v}")
        out={"all_ok":not errors,"checks":checks,"errors":errors,"authority":"each dependency HOW_USAGE.md"}
        if strict and errors: raise RuntimeError("HOW_USAGE API conformance failed: "+" | ".join(errors))
        return out

    @staticmethod
    def _csv_rows(path: Path) -> list[dict[str,str]]:
        with path.open(newline="",encoding="utf-8") as f: return list(csv.DictReader(f))

    def validate_datasets(self, *, strict: bool = True) -> dict[str, Any]:
        errors=[]; out: dict[str,Any]={}
        try:
            s=self.config.sarg_root; meta=json.loads((s/"metadata.json").read_text()); schema=json.loads((s/"schema.json").read_text())
            records=self._csv_rows(s/"sarg_records.csv"); patterns=self._csv_rows(s/"sarg_patterns.csv"); index=self._csv_rows(s/"sarg_lookup_index.csv")
            keys=[(r["regime_id"],r["phase_id"],r["crp_id"]) for r in records]
            expected=int(meta.get("expected_record_count",len(records)))
            pk=tuple(meta.get("primary_key") or schema.get("primary_key") or [])
            ok=(pk==("regime_id","phase_id","crp_id") and len(records)==len(patterns)==len(index)==expected and len(keys)==len(set(keys)))
            scenario_rows=self._csv_rows(s/"scenarios.csv")
            stage_rows=self._csv_rows(s/"sarg_stage_summary.csv")
            context_rows=self._csv_rows(s/"scenario_climate_contexts.csv")
            status_rows=self._csv_rows(s/"status_codes.csv")
            fmi_rows=self._csv_rows(s/"fmi_execution_trace.csv")
            route_count=len(list((s/"routes").glob("S*_C*.csv")))
            expected_routes=int(meta.get("scenario_count",0))*int(meta.get("crp_cluster_count",0))
            ancillary_ok=all((s/f).exists() for f in ("region_context.json","region_v1.geojson","crs_rejections.csv","sarg_samples.csv.gz"))
            ok=ok and ancillary_ok and len(scenario_rows)==int(meta.get("scenario_count",len(scenario_rows))) and route_count==expected_routes
            out["sarg"]={"ok":ok,"primary_key":list(pk),"records":len(records),"patterns":len(patterns),"lookup_index":len(index),
                         "expected_records":expected,"scenarios":len(scenario_rows),"stage_summary":len(stage_rows),
                         "scenario_contexts":len(context_rows),"status_codes":len(status_rows),"fmi_trace":len(fmi_rows),
                         "routes":route_count,"expected_routes":expected_routes,"ancillary_contract_files":ancillary_ok}
            if not ok: errors.append("SARG cardinality/primary-key contract failed")
        except Exception as exc: out["sarg"]={"ok":False,"error":str(exc)}; errors.append(f"SARG:{exc}")
        try:
            c=self.config.crp_root; stats=json.loads((c/"crp_runtime_statistics.json").read_text())
            canonical=["temperature_c","relative_humidity_percent","wind_speed_ms","wind_direction_sin","wind_direction_cos",
                       "terrain_elevation_m","terrain_slope_deg","local_relief_m","building_height_m","magnetic_anomaly_nt","magnetic_gradient_nt_per_m"]
            clusters=stats.get("clusters") or []; feature=list(stats.get("feature_order") or [])
            ok=feature==canonical and len(clusters)>0 and all(len(x.get("centroid") or [])==len(feature) for x in clusters)
            out["crp"]={"ok":ok,"canonical_definition":stats.get("canonical_cluster_definition"),"feature_order":feature,
                        "cluster_count":len(clusters),"catalog_exists":(c/"crp_cluster_catalog.csv").exists(),
                        "assignments_exists":(c/"crp_cell_assignments.csv.gz").exists(),"cluster_map_exists":(c/"crp_cluster_map.tif").exists()}
            if not ok: errors.append("CRP canonical feature/cluster contract failed")
        except Exception as exc: out["crp"]={"ok":False,"error":str(exc)}; errors.append(f"CRP:{exc}")
        try:
            import rasterio
            paths=[self.config.map_root/n for n in ("terrain_obstacles.tif","climate_contexts.tif","magnetic_anomaly.tif")]+[self.config.crp_root/"crp_cluster_map.tif"]
            desc=[]; base=None; aligned=True
            for p in paths:
                with rasterio.open(p) as ds:
                    key=(str(ds.crs),tuple(ds.transform),ds.width,ds.height)
                    if base is None: base=key
                    aligned=aligned and key==base
                    desc.append({"file":p.name,"crs":str(ds.crs),"width":ds.width,"height":ds.height})
            out["boulder_raster_alignment"]={"ok":aligned,"rasters":desc}
            if not aligned: errors.append("Boulder raster alignment failed")
        except Exception as exc: out["boulder_raster_alignment"]={"ok":False,"error":str(exc)}; errors.append(f"Boulder raster:{exc}")
        out["all_ok"]=not errors; out["errors"]=errors
        if strict and errors: raise RuntimeError("Dataset conformance failed: "+" | ".join(errors))
        return out

    @staticmethod
    def _public_symbols(pyfile: Path) -> list[dict[str,str]]:
        try: tree=ast.parse(pyfile.read_text(encoding="utf-8",errors="replace"))
        except Exception: return []
        out=[]
        for node in tree.body:
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and not node.name.startswith("_"):
                out.append({"name":node.name,"kind":"class" if isinstance(node,ast.ClassDef) else "function"})
        return out

    def capability_report(self) -> dict[str,Any]:
        report={}
        for key,spec in self.specs.items():
            files=[]
            if spec.root.is_dir():
                for py in sorted(spec.root.rglob("*.py")):
                    rel=py.relative_to(spec.root)
                    if any(part in {"__pycache__",".pytest_cache","build","dist"} for part in rel.parts): continue
                    symbols=self._public_symbols(py)
                    if symbols: files.append({"file":str(rel),"symbols":symbols})
            report[key]={"root":str(spec.root),"python_capabilities":files,"public_symbol_count":sum(len(x["symbols"]) for x in files)}
        return report

    def invoke(self, dependency: str, relative_py: str, symbol: str, *args: Any, **kwargs: Any) -> Any:
        if dependency not in self.specs: raise KeyError(f"Unknown dependency {dependency!r}")
        root=self.specs[dependency].root; pyfile=(root/relative_py).resolve()
        if root.resolve() not in pyfile.parents: raise ValueError("relative_py escapes dependency root")
        if not pyfile.is_file(): raise FileNotFoundError(pyfile)
        if str(root) not in sys.path: sys.path.insert(0,str(root))
        for sub in (root/"src",root/"python"):
            if sub.is_dir() and str(sub) not in sys.path: sys.path.insert(0,str(sub))
        name=f"_layer3_dep_{dependency}_{abs(hash(str(pyfile)))}"; spec=importlib.util.spec_from_file_location(name,pyfile)
        if spec is None or spec.loader is None: raise ImportError(f"Cannot import {pyfile}")
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); obj=getattr(module,symbol)
        return obj(*args,**kwargs) if callable(obj) else obj

    def write_capabilities(self, path: str | Path) -> None:
        p=Path(path); p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps(self.capability_report(),indent=2,sort_keys=True)+"\n",encoding="utf-8")
