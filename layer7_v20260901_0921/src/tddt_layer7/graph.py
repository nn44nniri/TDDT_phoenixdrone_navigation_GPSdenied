from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable


class TarjanSCC:
    @staticmethod
    def compute(vertices: Iterable[str], edges: Iterable[tuple[str, str]]) -> list[list[str]]:
        graph = defaultdict(list)
        V = set(vertices)
        for a, b in edges:
            graph[a].append(b); V.add(a); V.add(b)
        index = 0; stack = []; on = set(); idx = {}; low = {}; out = []
        def strong(v):
            nonlocal index
            idx[v] = low[v] = index; index += 1; stack.append(v); on.add(v)
            for w in graph[v]:
                if w not in idx:
                    strong(w); low[v] = min(low[v], low[w])
                elif w in on:
                    low[v] = min(low[v], idx[w])
            if low[v] == idx[v]:
                comp = []
                while True:
                    w = stack.pop(); on.remove(w); comp.append(w)
                    if w == v: break
                out.append(comp)
        for v in sorted(V):
            if v not in idx: strong(v)
        return out

    @staticmethod
    def selected_cycles(vertices, edges, budget: int = 8) -> list[list[str]]:
        """At most one deterministic simple cycle per non-trivial SCC."""
        edge_set = set(map(tuple, edges)); cycles = []
        for comp in TarjanSCC.compute(vertices, edges):
            if len(cycles) >= budget: break
            if len(comp) == 1:
                v = comp[0]
                if (v, v) in edge_set: cycles.append([v, v])
                continue
            C = set(comp); start = sorted(C)[0]; path = []; seen = set()
            def dfs(v):
                path.append(v); seen.add(v)
                for a, b in sorted(edge_set):
                    if a != v or b not in C: continue
                    if b == start and len(path) >= 2: return path + [start]
                    if b not in seen:
                        z = dfs(b)
                        if z: return z
                path.pop(); seen.discard(v); return None
            cyc = dfs(start)
            if cyc: cycles.append(cyc)
        return cycles[:budget]


@dataclass(slots=True)
class TopologyState:
    on_ticks: dict[str, int]
    off_ticks: dict[str, int]
    chatter_lock_remaining: int = 0


class TopologyManager:
    def __init__(self, max_edges: int = 64, on_threshold: float = 1.0,
                 off_threshold: float = 0.25, dwell_ticks: int = 2):
        self.max_edges = max_edges; self.on_threshold = on_threshold
        self.off_threshold = off_threshold; self.dwell_ticks = max(1, dwell_ticks)
        self.state = TopologyState({}, {}, 0)

    @staticmethod
    def key(edge): return f"{edge[0]}->{edge[1]}"

    def lock_for_dwell(self) -> None:
        self.state.chatter_lock_remaining = max(self.state.chatter_lock_remaining, self.dwell_ticks)

    def candidate(self, current: list[tuple[str, str]], scores: dict[str, float],
                  all_edges: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
        if self.state.chatter_lock_remaining > 0:
            self.state.chatter_lock_remaining -= 1
            return sorted(set(map(tuple, current)))
        cur = set(tuple(x) for x in current); candidates = set(cur); universe = set(all_edges or cur)
        for k in scores:
            if '->' in k:
                a, b = k.split('->', 1); universe.add((a, b))
        for e in sorted(universe):
            k = self.key(e); active = e in cur
            if active:
                self.state.on_ticks[k] = self.state.on_ticks.get(k, 0) + 1; self.state.off_ticks[k] = 0
                if scores.get(k, 0.0) <= self.off_threshold and self.state.on_ticks[k] >= self.dwell_ticks:
                    candidates.discard(e)
            else:
                self.state.off_ticks[k] = self.state.off_ticks.get(k, 0) + 1; self.state.on_ticks[k] = 0
                if scores.get(k, 0.0) >= self.on_threshold and self.state.off_ticks[k] >= self.dwell_ticks:
                    candidates.add(e)
        ranked = sorted(candidates, key=lambda e: (-scores.get(self.key(e), 0.0), self.key(e)))
        return sorted(ranked[:self.max_edges])

    @staticmethod
    def chattering_rate(old: list[tuple[str, str]], new: list[tuple[str, str]]) -> float:
        a, b = set(map(tuple, old)), set(map(tuple, new))
        return len(a.symmetric_difference(b)) / max(1, len(a))


def _cycle_edges(ckey: str) -> list[tuple[str, str]]:
    nodes = [x for x in ckey.split('->') if x]
    return list(zip(nodes, nodes[1:]))


def bounded_cycle_lci(edges, lci_by_cycle: dict[str, float], budget: int = 8) -> dict[str, float]:
    """Validate supplied cycle keys against graph/SCC structure and enforce a hard budget."""
    edge_set = set(map(tuple, edges)); valid = []
    for key, val in lci_by_cycle.items():
        ce = _cycle_edges(key)
        if not ce or ce[-1][1] != ce[0][0]:
            continue
        if all(e in edge_set for e in ce):
            valid.append((key, float(val)))
    valid.sort(key=lambda kv: (-abs(kv[1]), kv[0]))
    return dict(valid[:max(0, budget)])


def cycle_attribution(edges, lci_by_cycle: dict[str, float], threshold: float = 0.0,
                      budget: int = 8) -> dict[str, float]:
    out = defaultdict(float); edge_set = set(map(tuple, edges))
    for ckey, lci in bounded_cycle_lci(edges, lci_by_cycle, budget).items():
        if float(lci) <= threshold: continue
        for a, b in _cycle_edges(ckey):
            if (a, b) in edge_set: out[f"{a}->{b}"] += float(lci)
    return dict(out)
