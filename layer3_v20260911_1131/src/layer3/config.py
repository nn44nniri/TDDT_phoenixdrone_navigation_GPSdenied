from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import EdgeSpec


@dataclass
class TimeConfig:
    fast_dt_s: float = 1.0
    meso_fast_steps: int = 10
    slow_meso_windows: int = 6
    mission_epoch_utc: str | None = None
    max_future_skew_s: float = 0.05
    freshness_lambda_per_s: dict[str, float] = field(default_factory=lambda: {
        "MESA_NAV": 0.02, "WingMPET": 0.02, "MicroMetTwin": 1.0 / 1800.0,
    })


@dataclass
class AlignmentConfig:
    min_quality: dict[str, float] = field(default_factory=lambda: {"default": 0.2})
    min_validity: dict[str, float] = field(default_factory=lambda: {"default": 0.2})
    min_freshness: dict[str, float] = field(default_factory=lambda: {"default": 0.05})
    uncertainty_epsilon: float = 1e-9
    strict_semantics: bool = True
    strict_units: bool = True
    strict_frames: bool = True
    preserve_unmapped_signals: bool = True


@dataclass
class NetworkConfig:
    uncertainty_epsilon: float = 1e-6
    consistency_regularizer: float = 1e-6
    crp_quality_penalty: float = 1.0
    crp_regularizer: float = 1e-6
    crp_acceptance_probability: float = 0.999
    crp_min_features: int = 6
    crp_min_quality: float = 0.0
    crp_require_spatial_prior_agreement: bool = False
    topology_persistence_threshold: float = 0.5
    coupling_min: float = 0.0
    coupling_max: float = 1.0
    default_weight_min: float = 0.01
    default_weight_max: float = 0.95
    stability_decay: float = 0.65
    cycle_lci_threshold: float = 0.5
    top_b_per_node: int = 3
    chatter_max_changes_per_window: int = 2
    health_min: float = 0.35
    coc_observations: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    coc_constraints: list[dict[str, Any]] = field(default_factory=list)
    hidden_loop_max_cycle_length: int = 8
    edges: list[EdgeSpec] = field(default_factory=list)


@dataclass
class PlannerConfig:
    enabled: bool = True
    min_terrain_clearance_m: float = 90.0
    min_obstacle_clearance_m: float = 20.0
    base_obstacle_buffer_m: float = 15.0
    uncertainty_buffer_gain_m: float = 40.0
    wind_headwind_weight: float = 0.8
    wind_crosswind_weight: float = 0.25
    slope_weight: float = 0.015
    elevation_gain_weight: float = 0.02
    risk_weight: float = 1.25
    lookahead_m: float = 120.0
    cruise_airspeed_m_s: float = 13.0
    arrival_tolerance_m: float = 25.0
    max_cross_track_m: float = 45.0
    max_bank_deg: float = 35.0
    max_flightpath_deg: float = 8.0
    replan_cross_track_m: float = 35.0
    replan_on_meso: bool = True
    max_steps: int = 36
    natural_obstacle_block_class: int = 7
    energy_budget_j: float = 12000.0
    time_budget_s: float = 260.0
    planner_name: str = "A*_DEM_Wind_Wing_MESA"


@dataclass
class PathsConfig:
    # External dependencies are intentionally NOT embedded in this package.
    twins_root: str = "../dependencies/twins_layer2_v20260820_1233"
    climate_root: str = "../dependencies/climate_twin_v20260911_0922"
    tdoc_root: str = "../dependencies/TDOC_ISO_Universal_Unit_Conversion_Standard20260824_0914"
    sarg_package_root: str = "../dependencies/SARG_v20260821_0945"
    sarg_root: str = "../dependencies/SARG_v20260821_0945/generated/SARG"
    boulder_root: str = "../dependencies/boulder_multilayer_map_builder_v20260814_1215"
    map_root: str = "../dependencies/boulder_multilayer_map_builder_v20260814_1215/outputs/region_v1_map"
    crp_root: str = "../dependencies/boulder_multilayer_map_builder_v20260814_1215/outputs/region_v1_map/CRP"
    ss_kstore: str = "runtime/ss_kstore.jsonl"
    setd_kstore: str = "runtime/setd_kstore.jsonl"
    provenance_log: str = "runtime/provenance.jsonl"
    tdoc_health: str = "runtime/tdoc_health.json"
    snapshots_dir: str = "runtime/snapshots"
    versions_dir: str = "runtime/versions"


@dataclass
class Layer3Config:
    package_root: Path
    time: TimeConfig = field(default_factory=TimeConfig)
    alignment: AlignmentConfig = field(default_factory=AlignmentConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    policy: dict[str, Any] = field(default_factory=dict)
    objectives: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)

    EXPECTED_DEPENDENCY_BASENAMES = {
        "twins": "twins_layer2_v20260820_1233",
        "climate": "climate_twin_v20260911_0922",
        "tdoc": "TDOC_ISO_Universal_Unit_Conversion_Standard20260824_0914",
        "sarg": "SARG_v20260821_0945",
        "boulder": "boulder_multilayer_map_builder_v20260814_1215",
    }

    @staticmethod
    def _default_edges() -> list[EdgeSpec]:
        # Only the navigation-state feed required by the configured flight model is
        # structurally mandatory. The remaining relations are dynamic and therefore
        # eligible for hysteresis/dwell and Top-B sparsification.
        return [
            EdgeSpec("MicroMetTwin", "WingMPET", ["Data", "Model"], 0.85, 5.0, False,
                     criticality=0.9, coupling_type_scales={"Data":0.85,"Model":1.0}),
            EdgeSpec("MicroMetTwin", "MESA_NAV", ["Data", "Model"], 0.55, 5.0, False,
                     criticality=0.8, coupling_type_scales={"Data":0.75,"Model":0.60}),
            EdgeSpec("MESA_NAV", "WingMPET", ["State"], 0.90, 4.0, True,
                     criticality=1.0, coupling_type_scales={"State":1.0}),
            EdgeSpec("WingMPET", "MESA_NAV", ["Model", "Error"], 0.70, 1.0, False,
                     criticality=0.9, coupling_type_scales={"Model":0.8,"Error":1.0}),
            EdgeSpec("MESA_NAV", "MicroMetTwin", ["Data"], 0.65, 2.0, False,
                     criticality=0.7, coupling_type_scales={"Data":1.0}),
        ]

    @staticmethod
    def _default_coc_observations() -> dict[str, list[dict[str, Any]]]:
        return {
            "MESA_NAV":[{"key":"ground_velocity_ned_m_s","dimensions":["Vg_N","Vg_E","Vg_D"]}],
            "WingMPET":[{"key":"air_velocity_ned_m_s","dimensions":["Va_N","Va_E","Va_D"]}],
            "MicroMetTwin":[{"key":"wind_ned_m_s","dimensions":["W_N","W_E","W_D"]}],
        }

    @staticmethod
    def _default_coc_constraints() -> list[dict[str, Any]]:
        return [{"name":"velocity_triangle_ned","type":"builtin","lambda":1.0,"hard":True,
                 "edges":["MicroMetTwin->WingMPET","MicroMetTwin->MESA_NAV","MESA_NAV->WingMPET"]}]

    @classmethod
    def default(cls, package_root: str | Path) -> "Layer3Config":
        cfg = cls(package_root=Path(package_root).resolve())
        cfg.network.edges = cls._default_edges()
        cfg.network.coc_observations = cls._default_coc_observations()
        cfg.network.coc_constraints = cls._default_coc_constraints()
        cfg.objectives = {
            "fast": [
                {"id":"distance_to_goal","metric":"distance_to_goal_m","relation":"le","target":25.0,"unit":"m","weight":1.2,"achievement_mode":"progress_to_target"},
                {"id":"cross_track","metric":"cross_track_error_m","relation":"le","target":45.0,"unit":"m","weight":1.0},
                {"id":"terrain_clearance","metric":"terrain_clearance_m","relation":"ge","target":90.0,"unit":"m","weight":1.3},
                {"id":"obstacle_clearance","metric":"obstacle_clearance_m","relation":"ge","target":20.0,"unit":"m","weight":1.3},
                {"id":"wing_feasibility","metric":"wing_feasible","relation":"bool","target":1.0,"unit":"1","weight":1.5},
                {"id":"risk","metric":"risk","relation":"le","target":0.8,"unit":"1","weight":1.0}
            ],
            "meso": [
                {"id":"mean_cross_track","metric":"mean_cross_track_error_m","relation":"le","target":45.0,"unit":"m","weight":1.0},
                {"id":"minimum_clearance","metric":"min_terrain_clearance_m","relation":"ge","target":90.0,"unit":"m","weight":1.3},
                {"id":"feasible_fraction","metric":"feasible_fraction","relation":"ge","target":1.0,"unit":"1","weight":1.5},
                {"id":"mean_risk","metric":"mean_risk","relation":"le","target":0.8,"unit":"1","weight":1.0},
                {"id":"mean_fast_attainment","metric":"mean_fast_achievement_pct","relation":"ge","target":90.0,"unit":"%","weight":1.0}
            ],
            "slow": [
                {"id":"arrival","metric":"distance_to_goal_m","relation":"le","target":25.0,"unit":"m","weight":2.0,"achievement_mode":"progress_to_target"},
                {"id":"safety_fraction","metric":"safety_fraction","relation":"ge","target":0.99,"unit":"1","weight":2.0},
                {"id":"energy_budget","metric":"cumulative_energy_j","relation":"le","target":12000.0,"unit":"J","weight":1.0},
                {"id":"time_budget","metric":"mission_time_s","relation":"le","target":260.0,"unit":"s","weight":1.0},
                {"id":"mean_fast_attainment","metric":"mean_fast_achievement_pct","relation":"ge","target":90.0,"unit":"%","weight":1.0}
            ]
        }
        cfg.policy = {
            "version": "L3-policy-v1",
            "weight_bounds": {
                "MESA_NAV": [0.01, 0.95],
                "WingMPET": [0.01, 0.95],
                "MicroMetTwin": [0.01, 0.95],
            },
            "coupling_bounds": {e.key: [0.0, 1.0] for e in cfg.network.edges},
            "consistency_bounds": {"minimum_margin": -1.0, "maximum_mismatch": 100.0},
            "risk_threshold": 0.8,
            "objective_envelope": {},
        }
        cfg.runtime = {
            "persist_every_snapshot": True,
            "strict_dependency_validation": True,
            "require_contract_normalization": True,
            "require_structural_authorization": True,
            "hold_last_safe_on_validation_failure": True,
            "failure_containment": True,
            "use_boulder_runtime_sampling": True,
            "enable_runtime_couplings": True,
            "enable_counterfactual_ablation": True,
            "allow_dynamic_twin_registration": True,
        }
        return cfg

    @classmethod
    def from_yaml(cls, path: str | Path, *, package_root: str | Path | None = None) -> "Layer3Config":
        p = Path(path).resolve()
        root = Path(package_root).resolve() if package_root else p.parents[1]
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        cfg = cls.default(root)

        t = dict(raw.get("time", {}) or {})
        cfg.time = TimeConfig(**{**cfg.time.__dict__, **t})
        a = dict(raw.get("alignment", {}) or {})
        cfg.alignment = AlignmentConfig(**{**cfg.alignment.__dict__, **a})
        n = dict(raw.get("network", {}) or {})
        edges_raw = n.pop("edges", None)
        cfg.network = NetworkConfig(**{**cfg.network.__dict__, **n})
        if edges_raw is not None:
            cfg.network.edges = [EdgeSpec(**e) for e in edges_raw]
        if not cfg.network.edges:
            cfg.network.edges = cls._default_edges()
        if not cfg.network.coc_observations:
            cfg.network.coc_observations = cls._default_coc_observations()
        if not cfg.network.coc_constraints:
            cfg.network.coc_constraints = cls._default_coc_constraints()
        pl = dict(raw.get("planner", {}) or {})
        cfg.planner = PlannerConfig(**{**cfg.planner.__dict__, **pl})
        paths = dict(raw.get("paths", {}) or {})
        cfg.paths = PathsConfig(**{**cfg.paths.__dict__, **paths})
        cfg.policy.update(raw.get("policy", {}) or {})
        if raw.get("objectives") is not None: cfg.objectives = dict(raw.get("objectives") or {})
        cfg.runtime.update(raw.get("runtime", {}) or {})
        return cfg

    @staticmethod
    def _expand_env_defaults(text: str) -> str:
        # Supports ${VAR:-fallback} in addition to normal $VAR/${VAR}.
        pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]*)\}")
        text = pattern.sub(lambda m: os.environ.get(m.group(1), m.group(2)), text)
        return os.path.expandvars(text)

    def resolve(self, value: str | Path) -> Path:
        text = self._expand_env_defaults(str(value))
        p = Path(text).expanduser()
        return p.resolve() if p.is_absolute() else (self.package_root / p).resolve()

    def threshold(self, mapping: dict[str, float], twin_id: str) -> float:
        return float(mapping.get(twin_id, mapping.get("default", 0.0)))

    @property
    def twins_root(self) -> Path: return self.resolve(self.paths.twins_root)
    @property
    def climate_root(self) -> Path: return self.resolve(self.paths.climate_root)
    @property
    def tdoc_root(self) -> Path: return self.resolve(self.paths.tdoc_root)
    @property
    def sarg_package_root(self) -> Path: return self.resolve(self.paths.sarg_package_root)
    @property
    def sarg_root(self) -> Path: return self.resolve(self.paths.sarg_root)
    @property
    def boulder_root(self) -> Path: return self.resolve(self.paths.boulder_root)
    @property
    def map_root(self) -> Path: return self.resolve(self.paths.map_root)
    @property
    def crp_root(self) -> Path: return self.resolve(self.paths.crp_root)
