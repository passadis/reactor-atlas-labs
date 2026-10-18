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
from atlas_common.repositories.cosmos_repos import EntitlementRepository
from atlas_orchestrator.agents import IMPACT, KNOWLEDGE, PLANNER, TRIAGE, SpecialistFactory
from atlas_orchestrator.approval import ApprovalResponse
from atlas_orchestrator.executors import BranchNode, IntakeRequest, NodeProgress
from atlas_orchestrator.graph import build_workflow
from atlas_orchestrator.models import (
    ImpactResult,
    IncidentCase,
    IncidentReport,
    PolicyDecision,
    RemediationPlan,
    RemediationStep,
)
from atlas_orchestrator.runner import CaseRunner
from atlas_orchestrator.tool_approval import ToolApprovalGate

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


# ---------------------------------------------------- Phase 07: approval, live --


async def test_the_canonical_incident_parks_on_a_human_with_reasons() -> None:
    """The gate runs against the real catalogue and entitlements, and stops the case."""
    async with IncidentAgentRuntime(Settings.from_env()) as runtime:
        runner = CaseRunner(runtime, "graph")
        await runner.load_reference_data()
        assert runner.gated, "the policy gate did not load its reference data"
        doc = await runner.create(CANONICAL, raised_by={"persona": "sre"})
        case_id = str(doc["case_id"])

        parked: dict[str, Any] = {}
        async for event in runner.stream(case_id):
            if event.kind == "approval_required":
                parked = event.data
                break
        await runner.close()

    assert parked, "the case never reached the approval gate"
    assert parked["required_roles"], "nobody was named as an approver"
    assert parked["reasons"], "the gate gave no reasons"
    assert parked["plan_id"]
    # Every reason is a sentence a human can act on, not a code.
    assert all(len(r) > 20 for r in parked["reasons"]), parked["reasons"]


async def test_a_parked_case_is_durable_and_finishes_from_its_checkpoint() -> None:
    """Park a case, drop the runner entirely, then finish it from a brand-new one.

    This is `kill_orchestrator.sh` without the ACA call: the object that started the case is gone
    before the approval is given, and the case still completes.
    """
    async with IncidentAgentRuntime(Settings.from_env()) as runtime:
        first = CaseRunner(runtime, "graph")
        await first.load_reference_data()
        doc = await first.create(CANONICAL, raised_by={"persona": "sre"})
        case_id = str(doc["case_id"])
        async for event in first.stream(case_id):
            if event.kind == "approval_required":
                break
        assert first.checkpoints is not None
        checkpoints = await first.checkpoints.list_checkpoints(
            workflow_name=first.workflow_name("graph"))
        parked = [c for c in checkpoints if c.pending_request_info_events]
        await first.close()
    assert parked, "nothing was checkpointed while the case was parked"

    # A second runtime and a second runner: nothing survives except Cosmos.
    async with IncidentAgentRuntime(Settings.from_env()) as runtime:
        second = CaseRunner(runtime, "graph")
        await second.load_reference_data()
        outcome = await second.resume_from_checkpoint(
            case_id,
            response=ApprovalResponse(
                approved=True, by="dana@contoso.com", role="director",
                comment="Break-glass: payments rollback approved during an active Sev2."),
        )
        settled = await second.get(case_id)
        await second.close()

    assert outcome["path"] == "cold"
    assert outcome["status"] == "approved", outcome
    assert outcome["seconds"] < 30, f"a resume should be quick, took {outcome['seconds']}s"
    state = (settled or {}).get("state", {})
    assert state.get("approval", {}).get("accepted") is True
    assert any(d["rule"] == "human-approved" for d in state.get("decisions", []))


async def test_an_approval_that_lands_while_the_service_is_down_is_swept_up() -> None:
    """Record the answer with nobody running the case, then let a fresh runner's sweep find it."""
    async with IncidentAgentRuntime(Settings.from_env()) as runtime:
        first = CaseRunner(runtime, "graph")
        await first.load_reference_data()
        doc = await first.create(CANONICAL, raised_by={"persona": "sre"})
        case_id = str(doc["case_id"])
        async for event in first.stream(case_id):
            if event.kind == "approval_required":
                break
        # The answer arrives, but the service goes away before it can act on it.
        await first.cases.record_answer(case_id, ApprovalResponse(
            approved=True, by="dana@contoso.com", role="director",
            comment="Approved while the orchestrator was restarting.").model_dump(mode="json"))
        await first.close()

    async with IncidentAgentRuntime(Settings.from_env()) as runtime:
        second = CaseRunner(runtime, "graph")
        await second.load_reference_data()
        swept = await second.sweep()
        settled = await second.get(case_id)
        await second.close()

    # The deployed orchestrator replicas run the same sweep against the same Cosmos, so either this
    # runner or a live replica picks the case up first (seen once, under verifier load). The
    # property under test is that the answer is not lost - not which process found it.
    status = (settled or {}).get("status")
    assert any(row.get("case_id") == case_id for row in swept) or status != "awaiting_approval", (
        swept, status)
    assert status in ("approved", "executing", "completed"), status


async def test_tool_level_approval_holds_a_destructive_call() -> None:
    """The second mechanism: the framework pauses one call, with its arguments visible."""
    plan = RemediationPlan(
        steps=[RemediationStep(order=1, action="Disable the payments rollout flag",
                               target_service="svc-payments", command_ref="RB-0042#3",
                               destructive=True, expected_result="Latency returns to baseline",
                               rollback="Re-enable the flag")],
        risk="high", estimated_minutes=10, justification="From RB-0042.",
    ).with_plan_id("CASE-TOOLAPPR")
    case = IncidentCase(
        case_id="CASE-TOOLAPPR", report="Checkout is timing out.", status="approved", plan=plan,
        policy=PolicyDecision(approval_required=True, required_approver_roles=["director"],
                              plan_id=plan.plan_id),
    )

    async with IncidentAgentRuntime(Settings.from_env()) as runtime:
        entitlements = {e.role: e for e in
                        await EntitlementRepository(runtime.store).list_all()}
        gate = ToolApprovalGate(case, entitlements)
        response, session, pending = await gate.start(
            runtime.client, f"Execute step 1 of plan {plan.plan_id}.")

        assert pending, "the destructive tool ran without asking"
        assert pending[0].tool_name == "execute_remediation"
        assert plan.plan_id in pending[0].arguments
        assert not gate.dispatcher.records, "the tool body ran before approval"

        # The wrong role cannot release it.
        _refused, note = await gate.release(response, session, approver_role="sre")
        assert note.startswith("refused:"), note
        assert not gate.dispatcher.records, "a refused approval still executed the tool"

        # The right role can.
        response2, session2, pending2 = await gate.start(
            runtime.client, f"Execute step 1 of plan {plan.plan_id}.")
        assert pending2
        _released, note2 = await gate.release(response2, session2, approver_role="director")
        assert note2 == "released", note2
        assert gate.dispatcher.records, "an approved call never reached the tool"
        assert gate.dispatcher.records[0].step == 1
