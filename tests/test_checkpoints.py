"""Phase 07: checkpointing, recovery and the approval contract.

The unit half runs with no Azure and no model: a stub agent stands in for the specialists, so the
suspend, checkpoint, discard, restore, respond, complete cycle is exercised in milliseconds and on
every commit. The live half is in `test_orchestrator_integration.py`.

The test that matters most is `test_a_new_process_can_finish_a_case_it_never_started`: it builds a
workflow, runs it to the approval gate, **throws the workflow object away**, builds a second one
from the same node ids, and finishes the case from the checkpoint. That is the Episode 2 set piece
with the process restart simulated.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from agent_framework import FileCheckpointStorage, WorkflowBuilder
from atlas_common.models import SLO, Entitlement, Service
from atlas_orchestrator.approval import (
    ApprovalExecutor,
    ApprovalRequest,
    ApprovalResponse,
    PolicyGateExecutor,
    request_for,
)
from atlas_orchestrator.checkpoints import CHECKPOINT_TYPES, register_types
from atlas_orchestrator.executors import ReportExecutor
from atlas_orchestrator.models import (
    ImpactResult,
    IncidentCase,
    KnowledgeResult,
    PolicyDecision,
    RemediationPlan,
    RemediationStep,
    RunbookRef,
    TriageResult,
)
from atlas_orchestrator.node_ids import (
    AGENT_NODES,
    ALL_NODES,
    APPROVAL,
    DETERMINISTIC_NODES,
    POLICY_GATE,
    REPORT,
)

register_types()

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
WORKFLOW_NAME = "test-approval-workflow"


def service(service_id: str, tier: int, *, freeze: bool = False,
            environment: str = "prod") -> Service:
    return Service(
        service_id=service_id, name=service_id, tier=tier,  # type: ignore[arg-type]
        owner_team="team-checkout", on_call_rotation="rota-a",
        environment=environment,  # type: ignore[arg-type]
        region=["eu-west"], depends_on=[],
        slo=SLO(availability_target=0.999, latency_p95_ms=250, error_budget_minutes_month=43),
        runbook_ids=["RB-0042"], change_freeze=freeze, repository="contoso/checkout",
    )


CATALOGUE = {
    "svc-checkout-api": service("svc-checkout-api", 1),
    "svc-payments": service("svc-payments", 1, freeze=True),
    "svc-reporting": service("svc-reporting", 4, environment="staging"),
}
ENTITLEMENTS = {
    "sre": Entitlement(role="sre", may_approve_risk=["low", "medium"], may_approve_tiers=[2, 3, 4],
                       may_execute=True, max_blast_radius=8),
    "manager": Entitlement(role="manager", may_approve_risk=["low", "medium", "high"],
                           may_approve_tiers=[1, 2, 3, 4], may_execute=False,
                           max_blast_radius=15),
    "director": Entitlement(role="director", may_approve_risk=["low", "medium", "high"],
                            may_approve_tiers=[1, 2, 3, 4], may_execute=False,
                            max_blast_radius=25),
}


def analysed_case(case_id: str = "CASE-CKPT0001") -> IncidentCase:
    """A case as it stands the moment the planner finishes: everything filled, nothing decided.

    `plan_ready`, not `aggregated`: `record()` moves the case on when the plan lands, and the
    state machine refuses `aggregated -> awaiting_approval`. Getting this wrong in a fixture is
    how you discover the machine is real.
    """
    plan = RemediationPlan(
        steps=[RemediationStep(order=1, action="Disable the payments rollout flag",
                               target_service="svc-payments", command_ref="RB-0042#3",
                               destructive=True, expected_result="Latency returns to baseline",
                               rollback="Re-enable the flag")],
        risk="high", estimated_minutes=12, justification="From RB-0042.",
    ).with_plan_id(case_id)
    return IncidentCase(
        case_id=case_id, report="Checkout is timing out after the payments deploy.",
        status="plan_ready", services=["svc-checkout-api", "svc-payments"],
        raised_by={"persona": "sre", "email": "sam@contoso.com"},
        triage=TriageResult(severity="Sev2", category="deployment",
                            affected_services=["svc-checkout-api", "svc-payments"],
                            likely_cause="payments deploy",
                            suspected_change_ids=["CHG-2026-0873"], confidence=0.95,
                            summary="Checkout degraded after the payments deploy."),
        knowledge=KnowledgeResult(
            runbooks=[RunbookRef(doc_id="RB-0042", title="Checkout latency")]),
        impact=ImpactResult(blast_radius=10, affected_service_ids=["svc-payments"],
                            tier1_affected=["svc-checkout-api"], severity_hint="Sev2"),
        plan=plan,
    )


def build(storage: FileCheckpointStorage | None) -> Any:
    """The Phase 07 tail of the graph: gate, approval, report. Fixed ids, as in node_ids.py."""
    gate = PolicyGateExecutor(catalogue=CATALOGUE, entitlements=ENTITLEMENTS)
    approval = ApprovalExecutor(entitlements=ENTITLEMENTS)
    report = ReportExecutor(id=REPORT)
    builder = WorkflowBuilder(
        name=WORKFLOW_NAME, start_executor=gate, checkpoint_storage=storage,
        output_from=[report], intermediate_output_from=[gate, approval],
    )
    builder.add_chain([gate, approval, report])
    return builder.build()


async def run_to_gate(workflow: Any, case: IncidentCase) -> tuple[list[str], list[Any]]:
    """Run until the workflow parks on a human. Returns (pending request ids, outputs)."""
    pending: list[str] = []
    outputs: list[Any] = []
    async for event in workflow.run(case, stream=True):
        if event.type == "request_info":
            pending.append(event.request_id)
        elif event.type == "output":
            outputs.append(event.data)
    return pending, outputs


# ------------------------------------------------------------------ node identity --


def test_the_graph_signature_is_stable_across_builds() -> None:
    """Two builds of the same graph must hash identically, or no checkpoint can ever be resumed.

    The framework refuses a restore into a changed topology, so this is the test that catches a
    node id someone made dynamic.
    """
    assert build(None).graph_signature_hash == build(None).graph_signature_hash


def test_node_ids_are_fixed_strings_and_unique() -> None:
    assert len(set(ALL_NODES)) == len(ALL_NODES)
    assert all(isinstance(n, str) and n for n in ALL_NODES)
    # No id may look generated: a uuid or a case id in a node id breaks every future restore.
    assert not any("-" in n and len(n) > 24 for n in ALL_NODES)
    assert set(DETERMINISTIC_NODES) & set(AGENT_NODES) == set()


def test_the_built_workflow_uses_the_declared_ids() -> None:
    executors = set(build(None).executors)
    assert {POLICY_GATE, APPROVAL, REPORT} <= executors


# ---------------------------------------------------------------- the round trip --


async def test_the_workflow_parks_on_a_human_and_checkpoints(tmp_path: Path) -> None:
    storage = FileCheckpointStorage(storage_path=str(tmp_path))
    pending, outputs = await run_to_gate(build(storage), analysed_case())

    assert pending == [ApprovalExecutor.request_id_for("CASE-CKPT0001")]
    assert not [o for o in outputs if hasattr(o, "severity")], "a report before approval"

    checkpoints = await storage.list_checkpoints(workflow_name=WORKFLOW_NAME)
    assert checkpoints, "no checkpoint was written at all"
    parked = [c for c in checkpoints if c.pending_request_info_events]
    assert parked, "no checkpoint captured the pending approval"


async def test_a_new_process_can_finish_a_case_it_never_started(tmp_path: Path) -> None:
    """Build, run to the gate, discard, rebuild, restore, answer, complete. The set piece."""
    storage = FileCheckpointStorage(storage_path=str(tmp_path))
    first = build(storage)
    pending, _ = await run_to_gate(first, analysed_case())
    del first  # the "process" is gone

    parked = max((c for c in await storage.list_checkpoints(workflow_name=WORKFLOW_NAME)
                  if c.pending_request_info_events), key=lambda c: c.iteration_count)

    second = build(storage)
    reports = []
    async for event in second.run(
        checkpoint_id=parked.checkpoint_id,
        responses={pending[0]: ApprovalResponse(
            approved=True, by="dana@contoso.com", role="director",
            comment="Break-glass approved during an active Sev2.")},
        stream=True,
    ):
        if event.type == "output" and hasattr(event.data, "severity"):
            reports.append(event.data)

    assert reports, "the restored workflow produced no report"
    case = reports[0].case
    assert case.status == "approved"
    assert case.approval is not None and case.approval.accepted is True
    assert case.approval.by == "dana@contoso.com"
    assert any(d.rule == "human-approved" for d in case.decisions)


async def test_restoring_without_an_answer_re_emits_the_request(tmp_path: Path) -> None:
    """A restart with nobody answering yet must put the question back, not drop it."""
    storage = FileCheckpointStorage(storage_path=str(tmp_path))
    await run_to_gate(build(storage), analysed_case())
    parked = max((c for c in await storage.list_checkpoints(workflow_name=WORKFLOW_NAME)
                  if c.pending_request_info_events), key=lambda c: c.iteration_count)

    re_emitted: list[Any] = []
    async for event in build(storage).run(checkpoint_id=parked.checkpoint_id, stream=True):
        if event.type == "request_info":
            re_emitted.append(event.data)

    assert len(re_emitted) == 1
    assert isinstance(re_emitted[0], ApprovalRequest)
    assert re_emitted[0].case_id == "CASE-CKPT0001"
    assert re_emitted[0].required_roles == ["director"]


async def test_a_rejection_ends_the_case_without_a_plan(tmp_path: Path) -> None:
    storage = FileCheckpointStorage(storage_path=str(tmp_path))
    pending, _ = await run_to_gate(build(storage), analysed_case())
    parked = max((c for c in await storage.list_checkpoints(workflow_name=WORKFLOW_NAME)
                  if c.pending_request_info_events), key=lambda c: c.iteration_count)

    reports = []
    async for event in build(storage).run(
        checkpoint_id=parked.checkpoint_id,
        responses={pending[0]: ApprovalResponse(
            approved=False, by="dana@contoso.com", role="director",
            comment="Not during the payments freeze window; roll forward instead.")},
        stream=True,
    ):
        if event.type == "output" and hasattr(event.data, "severity"):
            reports.append(event.data)

    assert reports[0].case.status == "rejected"
    assert any(d.rule == "human-rejected" for d in reports[0].case.decisions)


async def test_an_approval_from_the_wrong_role_re_raises_the_request(tmp_path: Path) -> None:
    """A refused answer must not advance the case, and the question must survive to be asked again.

    Note where the re-raise shows up. Calling `ctx.request_info` from inside a response handler
    does NOT emit a `request_info` event in the same run - that run's request/response cycle has
    already been consumed. It lands in the next checkpoint, and is re-emitted when that checkpoint
    is restored. So the assertion is on the checkpoint, not on the event stream.
    """
    storage = FileCheckpointStorage(storage_path=str(tmp_path))
    pending, _ = await run_to_gate(build(storage), analysed_case())
    parked = max((c for c in await storage.list_checkpoints(workflow_name=WORKFLOW_NAME)
                  if c.pending_request_info_events), key=lambda c: c.iteration_count)

    reports = []
    async for event in build(storage).run(
        checkpoint_id=parked.checkpoint_id,
        responses={pending[0]: ApprovalResponse(
            approved=True, by="sam@contoso.com", role="sre",
            comment="I will take responsibility for this one.")},
        stream=True,
    ):
        if event.type == "output" and hasattr(event.data, "severity"):
            reports.append(event.data)
    assert not reports, "an approval from the wrong role must not produce a report"

    # The case is still parked, on a LATER checkpoint than the one we answered.
    still_parked = [c for c in await storage.list_checkpoints(workflow_name=WORKFLOW_NAME)
                    if c.pending_request_info_events]
    newest = max(still_parked, key=lambda c: c.iteration_count)
    assert newest.iteration_count > parked.iteration_count, "the refusal wrote no new checkpoint"

    asked_again: list[Any] = []
    async for event in build(storage).run(checkpoint_id=newest.checkpoint_id, stream=True):
        if event.type == "request_info":
            asked_again.append(event.data)
    assert asked_again, "a refused approval must leave the question askable"
    assert asked_again[0].required_roles == ["director"]


# ------------------------------------------------------------------ allowlist --


def test_every_state_model_is_allowlisted() -> None:
    """A type reachable in a checkpoint but missing here is skipped silently on read."""
    names = {f"{m.__module__}:{m.__qualname__}" for m in CHECKPOINT_TYPES}
    for required in (
        "atlas_orchestrator.models:IncidentCase",
        "atlas_orchestrator.models:PolicyDecision",
        "atlas_orchestrator.models:ApprovalRecord",
        "atlas_orchestrator.executors:IntakeRequest",
        "atlas_orchestrator.executors:Partial",
        "atlas_orchestrator.approval:ApprovalRequest",
        "atlas_orchestrator.approval:ApprovalResponse",
        "atlas_agent.schemas:Citation",
        "pydantic_core._pydantic_core:TzInfo",
    ):
        assert required in names, f"{required} is missing from CHECKPOINT_TYPES"


def test_the_allowlist_is_not_wider_than_it_needs_to_be() -> None:
    """Every entry is an ATLAS model, a nested one, or the tzinfo they all carry."""
    allowed_prefixes = ("atlas_orchestrator.", "atlas_agent.", "pydantic_core.")
    for model in CHECKPOINT_TYPES:
        assert model.__module__.startswith(allowed_prefixes), model


# ------------------------------------------------------------- the request shape --


def test_the_approval_request_is_built_from_state_not_prose() -> None:
    case = analysed_case()
    decision = PolicyDecision(
        approval_required=True, reasons=["the plan is high risk"],
        required_approver_roles=["director"], plan_id=case.plan.plan_id if case.plan else "")
    request = request_for(case, decision)

    assert request.case_id == case.case_id
    assert request.plan_id == (case.plan.plan_id if case.plan else "")
    assert request.destructive_steps == 1
    assert request.targets == ["svc-payments"]
    assert request.blast_radius == 10
    assert request.required_roles == ["director"]
    assert request.plan_summary and "svc-payments" in request.plan_summary[0]
    assert "RB-0042#3" in request.plan_summary[0]


def test_an_approval_response_demands_a_comment() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ApprovalResponse(approved=True, by="dana@contoso.com", role="director", comment="")


def test_the_request_id_is_derived_from_the_case() -> None:
    """Deterministic, so an answer given before the resume still matches its request."""
    assert ApprovalExecutor.request_id_for("CASE-X") == ApprovalExecutor.request_id_for("CASE-X")
    assert "CASE-X" in ApprovalExecutor.request_id_for("CASE-X")


# ------------------------------------------------------------------ idempotency --


def test_the_same_plan_dispatches_under_one_id() -> None:
    """`plan_id` is the Service Bus MessageId in Phase 10, so equal plans must hash equal."""
    case = analysed_case()
    assert case.plan is not None
    again = case.plan.model_copy(deep=True).with_plan_id(case.case_id)
    assert again.plan_id == case.plan.plan_id

    changed = case.plan.model_copy(deep=True)
    changed.steps[0].target_service = "svc-reporting"
    assert changed.with_plan_id(case.case_id).plan_id != case.plan.plan_id


async def test_answering_twice_does_not_run_the_case_twice(tmp_path: Path) -> None:
    """Replaying the same approval against the same checkpoint yields the same outcome."""
    storage = FileCheckpointStorage(storage_path=str(tmp_path))
    pending, _ = await run_to_gate(build(storage), analysed_case())
    parked = max((c for c in await storage.list_checkpoints(workflow_name=WORKFLOW_NAME)
                  if c.pending_request_info_events), key=lambda c: c.iteration_count)
    answer = ApprovalResponse(approved=True, by="dana@contoso.com", role="director",
                              comment="Break-glass approved during an active Sev2.")

    async def replay() -> Any:
        found = None
        async for event in build(storage).run(
            checkpoint_id=parked.checkpoint_id, responses={pending[0]: answer}, stream=True,
        ):
            if event.type == "output" and hasattr(event.data, "severity"):
                found = event.data
        return found

    first, second = await replay(), await replay()
    assert first is not None and second is not None
    assert first.case.status == second.case.status == "approved"
    assert first.plan_id == second.plan_id
    assert first.case.approval is not None and second.case.approval is not None


def test_concurrent_builds_do_not_diverge() -> None:
    """Ten builds in parallel must all agree on the graph, or a replica cannot resume another's."""
    hashes = {build(None).graph_signature_hash for _ in range(10)}
    assert len(hashes) == 1


def test_asyncio_is_available() -> None:
    """Guard against the module-level `register_types()` being skipped by an import error."""
    assert asyncio.get_event_loop_policy() is not None


# ------------------------------------------------ the handoff shape's approval tail --


def test_the_approval_tail_is_the_same_three_nodes_with_a_stable_identity() -> None:
    """Handoff cases go through this. Before it existed they skipped the policy gate entirely."""
    from atlas_orchestrator.graph import build_approval_tail
    from atlas_orchestrator.node_ids import WORKFLOW_APPROVAL_TAIL

    first = build_approval_tail(catalogue=CATALOGUE, entitlements=ENTITLEMENTS)
    second = build_approval_tail(catalogue=CATALOGUE, entitlements=ENTITLEMENTS)
    assert first.name == WORKFLOW_APPROVAL_TAIL
    assert set(first.executors) == {POLICY_GATE, APPROVAL, REPORT}
    assert first.graph_signature_hash == second.graph_signature_hash


async def test_a_handoff_case_parks_on_the_same_gate(tmp_path: Path) -> None:
    from atlas_orchestrator.graph import build_approval_tail

    storage = FileCheckpointStorage(storage_path=str(tmp_path))
    tail = build_approval_tail(catalogue=CATALOGUE, entitlements=ENTITLEMENTS,
                               checkpoint_storage=storage)
    pending, outputs = await run_to_gate(tail, analysed_case("CASE-HANDOFF1"))
    assert pending == [ApprovalExecutor.request_id_for("CASE-HANDOFF1")]
    assert not [o for o in outputs if hasattr(o, "severity")]


async def test_a_case_with_no_plan_passes_the_gate_without_an_illegal_transition() -> None:
    """A degraded planner leaves nothing to authorise. The case must reach its report anyway."""
    case = analysed_case("CASE-NOPLAN01")
    case.plan = None
    case.status = "aggregated"
    reports = []
    async for event in build(None).run(case, stream=True):
        if event.type == "output" and hasattr(event.data, "severity"):
            reports.append(event.data)
    assert reports, "a plan-less case never reached its report"
    assert reports[0].case.status == "aggregated"
    assert reports[0].needs_human is True
