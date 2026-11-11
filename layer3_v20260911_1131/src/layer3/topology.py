from __future__ import annotations

import copy
from collections import defaultdict
from typing import Any, Mapping

from .config import Layer3Config
from .models import EdgeSpec, SharedTransDomainState
from .utils import clamp


class TopologyManager:
    """Dynamic G(t) with hysteresis/dwell, safety cuts and sparse Top-B budget."""

    def __init__(self, config: Layer3Config):
        self.config = config
        self.edges: dict[str, EdgeSpec] = {e.key: copy.deepcopy(e) for e in config.network.edges}
        self._candidate_streak: dict[str, int] = defaultdict(int)
        self._candidate_direction: dict[str, int] = defaultdict(int)

    def relevance(self, edge: EdgeSpec, std: SharedTransDomainState, weights: Mapping[str, float]) -> float:
        src = std.representations.get(edge.source)
        dst = std.representations.get(edge.target)
        if src is None or dst is None:
            return 0.0
        trust = (float(weights.get(edge.source, 0.0)) + float(weights.get(edge.target, 0.0))) / 2.0
        uncertainty = 1.0 - (src.normalized_uncertainty + dst.normalized_uncertainty) / 2.0
        quality = (src.quality + dst.quality) / 2.0
        risk = clamp(float(std.risks.get(edge.key, edge.risk_weight)), 0.0, 1.0)
        score = 0.40 * trust + 0.25 * uncertainty + 0.20 * quality + 0.10 * risk + 0.05 * edge.criticality
        return clamp(score, 0.0, 1.0)

    def active_graph(self, std: SharedTransDomainState, weights: Mapping[str, float], safety_gates: Mapping[str, bool] | None = None, unhealthy_nodes: set[str] | None = None) -> dict[str, Any]:
        safety_gates = dict(safety_gates or {})
        unhealthy_nodes = set(unhealthy_nodes or set())
        records: dict[str, dict[str, Any]] = {}
        per_source: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for key, edge in self.edges.items():
            rel = self.relevance(edge, std, weights)
            safety = bool(safety_gates.get(key, True))
            containment = edge.source not in unhealthy_nodes and edge.target not in unhealthy_nodes
            effective_pre_budget = bool(edge.active and safety and containment)
            records[key] = {
                "source": edge.source, "target": edge.target,
                "coupling_types": list(edge.coupling_types),
                "active_meso": bool(edge.active), "safety_gate": int(safety),
                "failure_containment_gate": int(containment),
                "active_effective": int(effective_pre_budget),
                "mandatory": edge.mandatory, "relevance": rel,
                "delay_ticks": edge.delay_ticks, "tolerance": edge.tolerance,
                "top_b_selected": True,
            }
            if effective_pre_budget and not edge.mandatory:
                per_source[edge.source].append((key, rel))

        B = max(0, int(self.config.network.top_b_per_node))
        if B >= 0:
            for source, items in per_source.items():
                keep = {k for k, _ in sorted(items, key=lambda kv: kv[1], reverse=True)[:B]}
                for key, _ in items:
                    if key not in keep:
                        records[key]["active_effective"] = 0
                        records[key]["top_b_selected"] = False
                        records[key]["budget_cut"] = True

        return {
            "nodes": sorted(std.representations), "edges": records,
            "active_edges": [k for k, v in records.items() if v.get("active_effective")],
            "top_b_per_node": B, "unhealthy_nodes": sorted(unhealthy_nodes),
            "formula": "G(t), TopB_i, hysteresis/dwell; Layer3-v3",
        }

    def generate_candidate(self, persistence: Mapping[str, float], graph: Mapping[str, Any]) -> dict[str, Any]:
        changes: dict[str, bool] = {}
        for key, edge in self.edges.items():
            rel = float(graph.get("edges", {}).get(key, {}).get("relevance", 0.0))
            p = float(persistence.get(key, 0.0))
            desired = edge.active
            if edge.mandatory:
                desired = True
            elif edge.active and rel <= edge.off_threshold and p < self.config.network.topology_persistence_threshold:
                desired = False
            elif (not edge.active) and (rel >= edge.on_threshold or p >= self.config.network.topology_persistence_threshold):
                desired = True
            direction = 1 if desired else -1
            if desired != edge.active:
                if self._candidate_direction[key] == direction:
                    self._candidate_streak[key] += 1
                else:
                    self._candidate_direction[key] = direction
                    self._candidate_streak[key] = 1
                if self._candidate_streak[key] >= max(1, edge.dwell_windows):
                    changes[key] = desired
            else:
                self._candidate_streak[key] = 0
                self._candidate_direction[key] = 0
        return {"changes": changes, "commit": False, "reason": "meso_candidate_only", "formula": "3.2-8/3.3-30"}

    def commit(self, changes: Mapping[str, bool]) -> None:
        for key, value in changes.items():
            if key in self.edges:
                if self.edges[key].mandatory and not value:
                    continue
                self.edges[key].active = bool(value)
