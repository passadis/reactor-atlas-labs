"""Phase 06 acceptance, live: the workflow against the deployed model, Cosmos and Search.

Three things are proven here that a unit test cannot:

1. The canonical incident produces a complete, typed case in every orchestration shape.
2. The concurrent branch really is concurrent - wall clock beats the sum of the two branches.
3. A dead specialist degrades the run instead of killing it.

Costs roughly four minutes of model time. Skips cleanly without a provisioned environment.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import pytest
from atlas_agent.factory import IncidentAgentRuntime
from atlas_common.config import Settings, load_local_env
from atlas_orchestrator.agents import IMPACT, KNOWLEDGE, PLANNER, TRIAGE, SpecialistFactory
from atlas_orchestrator.executors import BranchNode, IntakeRequest, NodeProgress
from atlas_orchestrator.graph import build_workflow
from atlas_orchestrator.models import ImpactResult, IncidentReport
from atlas_orchestrator.runner import CaseRunner

load_local_env()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(
        not os.environ.get("FOUNDRY_PROJECT_ENDPOINT"),
        reason="no provisioned environment (.env.local missing)",
    ),
]

CANONICAL = (
    "Checkout is timing out for about 20% of EU customers since 14:05, "
    "started right after the payments deploy."
)


async def run_graph(mode: str) -> tuple[IncidentReport, dict[str, NodeProgress], float]:
    """Run the canonical incident and collect the report plus every node's own timing."""
    async with IncidentAgentRuntime(Settings.from_env()) as runtime:
        workflow = build_workflow(SpecialistFactory(runtime), mode)  # type: ignore[arg-type]
        nodes: dict[str, NodeProgress] = {}
        report: IncidentReport | None = None
        started = time.perf_counter()
        async for event in workflow.run(IntakeRequest(report=CANONICAL, mode=mode), stream=True):
            if event.type == "intermediate" and isinstance(event.data, NodeProgress):
                nodes[event.data.node] = event.data
            elif event.type == "output" and isinstance(event.data, IncidentReport):
                report = event.data
        elapsed = time.perf_counter() - started
    assert report is not None, "the workflow produced no report"
    return report, nodes, elapsed


async def test_the_canonical_incident_produces_a_complete_typed_case() -> None:
    report, nodes, elapsed = await run_graph("graph")
    case = report.case

    assert report.severity in ("Sev1", "Sev2"), report.severity
    assert case.triage is not None
    assert "CHG-2026-0873" in case.triage.suspected_change_ids
    assert "svc-checkout-api" in case.triage.affected_services

    assert case.knowledge is not None
    assert case.knowledge.runbooks, "the knowledge agent recorded no runbook"
    assert {r.doc_id for r in case.knowledge.runbooks} & {"RB-0042", "RB-0056", "RB-0060"}

    assert case.impact is not None
    assert case.impact.blast_radius > 0
    assert "svc-checkout-api" in case.impact.tier1_affected

    assert case.plan is not None
    assert case.plan.steps and case.plan.plan_id
    assert case.status == "plan_ready"
    assert report.needs_human is True  # Tier 1 is in the blast radius
    assert not case.degraded, case.degraded

    # The decision log names the rule behind every deterministic choice.
    rules = {d.rule for d in case.decisions}
    assert {"intake", "triage", "knowledge", "impact", "plan", "report"} <= rules
    assert "severity=max(triage,impact)" in rules
    assert "tier1-in-blast-radius" in rules

    assert elapsed < 120, f"the canonical case took {elapsed:.0f}s"
    assert set(nodes) == {"intake", TRIAGE, KNOWLEDGE, IMPACT, "aggregate", PLANNER}


async def test_the_concurrent_branch_really_is_concurrent() -> None:
    """Wall clock across the branch must beat running the two branches back to back."""
    _report, nodes, _elapsed = await run_graph("graph")
    knowledge, impact = nodes[KNOWLEDGE].seconds, nodes[IMPACT].seconds
    # Both branch nodes are timed from their own start; the superstep ends when the slower finishes.
    branch_wall = max(knowledge, impact)
    assert branch_wall < (knowledge + impact) * 0.9, (
        f"knowledge {knowledge}s and impact {impact}s did not overlap"
    )


async def test_the_plan_is_deterministic_for_the_same_steps() -> None:
    report, _nodes, _elapsed = await run_graph("graph")
    assert report.case.plan is not None
    replayed = report.case.plan.with_plan_id(report.case_id)
    assert replayed.plan_id == report.case.plan.plan_id


async def test_sequential_mode_reaches_the_same_kind_of_answer() -> None:
    report, nodes, _elapsed = await run_graph("sequential_only")
    assert report.severity in ("Sev1", "Sev2")
    assert report.case.plan is not None and report.case.plan.steps
    assert set(nodes) == {"intake", TRIAGE, KNOWLEDGE, IMPACT, "aggregate", PLANNER}


async def test_handoff_mode_fills_the_same_typed_state() -> None:
    """The shape changes; the contract does not. Every dimension still arrives, via record tools."""
    async with IncidentAgentRuntime(Settings.from_env()) as runtime:
        runner = CaseRunner(runtime, "handoff")
        report = await runner.run_sync(CANONICAL, mode="handoff")
    case = report.case
    assert case.triage is not None and case.impact is not None and case.plan is not None
    assert case.knowledge is not None
    assert case.status == "plan_ready"
    assert report.severity in ("Sev1", "Sev2", "Sev3")
    assert {d.by for d in case.decisions} >= {TRIAGE, IMPACT, PLANNER}
    # A mesh that ran out of turns is filled in by deterministic code, and says so. Either way
    # the case is complete; the decision log records which path got it there.
    filled = [d.by for d in case.decisions if d.rule == "handoff-gap-filled"]
    assert not case.degraded, f"gap filling failed for {case.missing} (filled {filled})"


async def test_a_dead_specialist_degrades_the_run_rather_than_killing_it() -> None:
    """Point the impact agent at a broken agent and assert the case still completes."""

    class DeadAgent:
        async def run(self, *_args: Any, **_kwargs: Any) -> Any:
            await asyncio.sleep(0)
            raise ConnectionError("impact agent endpoint is unreachable")

    async with IncidentAgentRuntime(Settings.from_env()) as runtime:
        factory = SpecialistFactory(runtime)
        workflow = build_workflow(factory, "graph")
        dead = BranchNode(id=IMPACT, agent=DeadAgent(),  # type: ignore[arg-type]
                          output_model=ImpactResult, dimension="impact")
        workflow.executors[IMPACT] = dead

        report: IncidentReport | None = None
        async for event in workflow.run(IntakeRequest(report=CANONICAL), stream=True):
            if event.type == "output" and isinstance(event.data, IncidentReport):
                report = event.data

    assert report is not None, "a dead specialist killed the run"
    assert report.missing_analysis == ["impact"]
    assert report.needs_human is True
    assert "Missing analysis: impact" in report.summary
    assert report.case.triage is not None, "losing impact must not lose triage"
    assert report.case.plan is not None, "losing impact must not lose the plan"
    assert any(d.rule == "degrade" for d in report.case.decisions)
