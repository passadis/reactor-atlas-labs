"""Deterministic dependency-graph walks over the service catalogue.

This module is the clearest example in ATLAS of the agentic/deterministic split that CLAUDE.md
section 1 insists on. Blast radius is a graph traversal with an exact answer. A language model
asked to "estimate the blast radius" will produce something plausible and unverifiable; this
produces the same answer every time and can be unit tested. The impact agent calls this and then
explains the result. It never computes it.

Pure functions over an in-memory catalogue. No I/O, no Azure, no clock.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping

from .models import BlastRadius, Service


def build_reverse_index(services: Iterable[Service]) -> dict[str, list[str]]:
    """Map each service to the services that depend on it.

    The catalogue stores `depends_on` (who I need). Impact runs the other way: if payments breaks,
    what breaks with it. That is the reverse edge, so it is built once and reused.

    Returned lists are sorted so every downstream result is deterministic.
    """
    reverse: dict[str, set[str]] = {s.service_id: set() for s in services}
    for service in services:
        for dependency in service.depends_on:
            # A dependency that is not itself catalogued would silently vanish from impact
            # analysis, so it is recorded rather than dropped.
            reverse.setdefault(dependency, set()).add(service.service_id)
    return {k: sorted(v) for k, v in sorted(reverse.items())}


def detect_cycles(services: Iterable[Service]) -> list[list[str]]:
    """Return any dependency cycles. An empty list means the catalogue is a DAG.

    The generator asserts this is empty. A cycle would make blast radius non-terminating in the
    naive formulation and, more importantly, would mean the seeded catalogue is not a realistic
    service graph.
    """
    forward = {s.service_id: sorted(s.depends_on) for s in services}
    cycles: list[list[str]] = []
    # 0 = unvisited, 1 = on the current path, 2 = fully explored
    state: dict[str, int] = {}

    def walk(node: str, path: list[str]) -> None:
        if state.get(node, 0) == 1:
            cycles.append([*path[path.index(node) :], node])
            return
        if state.get(node, 0) == 2:
            return
        state[node] = 1
        for nxt in forward.get(node, []):
            walk(nxt, [*path, nxt])
        state[node] = 2

    for service_id in sorted(forward):
        walk(service_id, [service_id])
    return cycles


def dependency_depth(services: Iterable[Service]) -> int:
    """Longest path length in the forward `depends_on` graph.

    DATA-MODEL.md requires depth >= 4 so the canonical incident has something interesting to walk.
    """
    forward = {s.service_id: sorted(s.depends_on) for s in services}
    memo: dict[str, int] = {}

    def depth_of(node: str, seen: frozenset[str]) -> int:
        if node in memo:
            return memo[node]
        if node in seen:  # defensive; a cycle would otherwise recurse forever
            return 0
        best = 0
        for nxt in forward.get(node, []):
            best = max(best, 1 + depth_of(nxt, seen | {node}))
        memo[node] = best
        return best

    return max((depth_of(n, frozenset()) for n in forward), default=0)


def blast_radius(
    service_id: str,
    services: Iterable[Service],
    depth: int = 3,
    reverse_index: Mapping[str, list[str]] | None = None,
) -> BlastRadius:
    """Which services are affected if `service_id` degrades, up to `depth` hops.

    Breadth-first over the reverse dependency edges. `depth` bounds how far consequences are
    followed, because in a real catalogue everything eventually touches everything and an unbounded
    answer is not actionable.

    `paths` records one shortest path to each affected service. That is what lets the impact agent
    say "payments, because checkout depends on it, and ledger because payments does" rather than
    presenting an opaque number.
    """
    catalogue = {s.service_id: s for s in services}
    reverse = (
        dict(reverse_index)
        if reverse_index is not None
        else build_reverse_index(catalogue.values())
    )

    affected: dict[str, list[str]] = {}
    max_depth_reached = 0

    queue: deque[tuple[str, int, list[str]]] = deque([(service_id, 0, [service_id])])
    seen = {service_id}

    while queue:
        current, hops, path = queue.popleft()
        if hops >= depth:
            continue
        for dependent in reverse.get(current, []):
            if dependent in seen:
                continue
            seen.add(dependent)
            affected[dependent] = [*path, dependent]
            max_depth_reached = max(max_depth_reached, hops + 1)
            queue.append((dependent, hops + 1, [*path, dependent]))

    affected_ids = sorted(affected)
    # Tier 1 is revenue-critical. The policy gate escalates on this, so it is surfaced explicitly
    # rather than left for a caller to recompute.
    tier1 = sorted(sid for sid in affected_ids if sid in catalogue and catalogue[sid].tier == 1)

    return BlastRadius(
        service_id=service_id,
        depth=depth,
        affected_service_ids=affected_ids,
        affected_count=len(affected_ids),
        tier1_affected=tier1,
        max_depth_reached=max_depth_reached,
        paths=[affected[sid] for sid in affected_ids],
    )
