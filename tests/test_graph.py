"""Unit tests for the deterministic dependency walks.

Blast radius is code, not model output, so it gets tested like code. These use a tiny hand-built
catalogue rather than the generated one so the expected answers are obvious by inspection.
"""

from __future__ import annotations

import pytest
from atlas_common.graph import blast_radius, build_reverse_index, dependency_depth, detect_cycles
from atlas_common.models import SLO, Service

SLO_FIXTURE = SLO(availability_target=0.999, latency_p95_ms=300, error_budget_minutes_month=43)


def svc(service_id: str, depends_on: list[str], tier: int = 3) -> Service:
    return Service(
        service_id=service_id,
        name=service_id,
        tier=tier,  # type: ignore[arg-type]
        owner_team="team-x",
        on_call_rotation="rot-x",
        environment="prod",
        region=["eastus"],
        depends_on=depends_on,
        slo=SLO_FIXTURE,
        runbook_ids=[],
        change_freeze=False,
        repository=f"contoso/{service_id}",
    )


#   web -> api -> payments -> ledger
#   mobile -> api
# Arrows are "depends on", so ledger breaking affects payments, api, web and mobile.
CHAIN = [
    svc("web", ["api"], tier=1),
    svc("mobile", ["api"]),
    svc("api", ["payments"]),
    svc("payments", ["ledger"]),
    svc("ledger", []),
]


def test_reverse_index_inverts_depends_on() -> None:
    reverse = build_reverse_index(CHAIN)
    assert reverse["ledger"] == ["payments"]
    assert reverse["api"] == ["mobile", "web"]  # sorted, so deterministic
    assert reverse["web"] == []


def test_blast_radius_walks_dependents_not_dependencies() -> None:
    """The direction is the whole point: who breaks when I break."""
    result = blast_radius("ledger", CHAIN, depth=5)
    assert result.affected_service_ids == ["api", "mobile", "payments", "web"]
    assert result.affected_count == 4
    # ledger itself is not in its own blast radius
    assert "ledger" not in result.affected_service_ids


def test_blast_radius_respects_depth_bound() -> None:
    assert blast_radius("ledger", CHAIN, depth=1).affected_service_ids == ["payments"]
    assert blast_radius("ledger", CHAIN, depth=2).affected_service_ids == ["api", "payments"]


def test_blast_radius_reports_tier1() -> None:
    result = blast_radius("ledger", CHAIN, depth=5)
    assert result.tier1_affected == ["web"]


def test_blast_radius_paths_explain_the_number() -> None:
    """Each affected service carries the chain that reaches it, for the agent to narrate."""
    result = blast_radius("ledger", CHAIN, depth=5)
    paths = dict(zip(result.affected_service_ids, result.paths, strict=True))
    assert paths["payments"] == ["ledger", "payments"]
    assert paths["web"] == ["ledger", "payments", "api", "web"]


def test_blast_radius_is_deterministic() -> None:
    a = blast_radius("ledger", CHAIN, depth=5)
    b = blast_radius("ledger", list(reversed(CHAIN)), depth=5)
    assert a.model_dump() == b.model_dump()


def test_diamond_dependency_counts_a_service_once() -> None:
    #      top
    #     /   \
    #  left   right
    #     \   /
    #      base
    diamond = [
        svc("top", ["left", "right"]),
        svc("left", ["base"]),
        svc("right", ["base"]),
        svc("base", []),
    ]
    result = blast_radius("base", diamond, depth=5)
    assert result.affected_service_ids == ["left", "right", "top"]
    assert result.affected_count == 3


def test_leaf_service_has_empty_blast_radius() -> None:
    result = blast_radius("web", CHAIN, depth=5)
    assert result.affected_service_ids == []
    assert result.affected_count == 0
    assert result.max_depth_reached == 0


def test_unknown_service_is_not_an_error() -> None:
    """Tools must return typed emptiness, never raise into the model. CLAUDE.md section 1."""
    result = blast_radius("does-not-exist", CHAIN, depth=3)
    assert result.affected_count == 0


def test_detect_cycles_finds_a_cycle() -> None:
    cyclic = [svc("a", ["b"]), svc("b", ["c"]), svc("c", ["a"])]
    assert detect_cycles(cyclic) != []


def test_detect_cycles_clean_on_a_dag() -> None:
    assert detect_cycles(CHAIN) == []


def test_dependency_depth() -> None:
    # web -> api -> payments -> ledger is three edges
    assert dependency_depth(CHAIN) == 3


@pytest.mark.parametrize("depth", [0, 1, 2, 3, 4, 10])
def test_depth_is_monotonic(depth: int) -> None:
    """Increasing depth can never shrink the blast radius."""
    smaller = blast_radius("ledger", CHAIN, depth=depth)
    larger = blast_radius("ledger", CHAIN, depth=depth + 1)
    assert set(smaller.affected_service_ids) <= set(larger.affected_service_ids)
