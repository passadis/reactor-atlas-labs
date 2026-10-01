"""The policy gate and the approval suspension, as workflow nodes.

This is the Episode 2 set piece. `PolicyGateExecutor` decides, in plain code, whether a plan may
run. If it needs a human, `ApprovalExecutor` calls `ctx.request_info(...)` and the workflow
**suspends** - a checkpoint already exists at that superstep boundary, so the process can be killed
here and resumed later from a different replica, or a different day.

Neither executor calls a model. The planner's `requires_approval` is an opinion; these two nodes
hold the authority, and they read the catalogue, the entitlement table and the clock to use it.
"""

# NOTE: no `from __future__ import annotations` in this module, deliberately.
#
# `@response_handler` validates its `ctx` parameter with a raw `inspect.signature`, which under
# PEP 563 hands it the annotation as a *string* and it refuses:
#
#     ValueError: Response handler parameter 'ctx' must be annotated as WorkflowContext,
#     WorkflowContext[T], or WorkflowContext[T, U], got WorkflowContext[IncidentCase, NodeProgress]
#
# Passing explicit types to the decorator does not help - it validates `ctx` either way. `@handler`
# resolves the same annotation without complaint, so the inconsistency is in the framework, not
# here (docs/API-NOTES.md 14.2). Python 3.13 evaluates `X | None` natively, so nothing in this
# module needs the future import.

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_framework import Executor, WorkflowContext, handler, response_handler
from atlas_common.models import Entitlement, Service
from pydantic import BaseModel, ConfigDict, Field

from .executors import NodeProgress
from .models import ApprovalRecord, IncidentCase, PolicyDecision
from .node_ids import APPROVAL, POLICY_GATE
from .policy import EXPIRY_ROLE, approval_is_valid, evaluate, implicated_services

log = logging.getLogger("atlas.orchestrator")


class ApprovalRequest(BaseModel):
    """What the approval console, the Logic App and the report all render.

    Everything here is derived from state by code. Nothing in it is written by an agent, so an
    approver is never reading the model's account of why it should be allowed to proceed.
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str
    plan_id: str
    plan_summary: list[str] = Field(default_factory=list)
    risk: str = "high"
    blast_radius: int = 0
    reasons: list[str] = Field(default_factory=list)
    required_roles: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    destructive_steps: int = 0
    targets: list[str] = Field(default_factory=list)


class ApprovalResponse(BaseModel):
    """A human's answer. `comment` is mandatory on both paths: a decision without a reason is not
    an audit trail."""

    model_config = ConfigDict(extra="forbid")

    approved: bool
    by: str
    role: str
    comment: str = Field(min_length=3, max_length=1000)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def request_for(case: IncidentCase, decision: PolicyDecision) -> ApprovalRequest:
    """Flatten the case and the policy decision into what a human needs to answer. No model call."""
    plan = case.plan
    steps = plan.steps if plan else []
    return ApprovalRequest(
        case_id=case.case_id,
        plan_id=plan.plan_id if plan else "",
        plan_summary=[f"{s.order}. {s.action} on {s.target_service} "
                      f"({'destructive' if s.destructive else 'safe'}, {s.command_ref})"
                      for s in steps],
        risk=plan.risk if plan else "high",
        blast_radius=case.impact.blast_radius if case.impact else 0,
        reasons=list(decision.reasons),
        required_roles=list(decision.required_approver_roles),
        expires_at=decision.expires_at,
        destructive_steps=sum(1 for s in steps if s.destructive),
        targets=sorted({s.target_service for s in steps}),
    )


class PolicyGateExecutor(Executor):
    """Deterministic authorization. Reads the catalogue and entitlements; never the model.

    Holds no state of its own between runs: the catalogue and entitlements are passed in at build
    time and the decision lands on the case, which is what the checkpoint carries.
    """

    def __init__(
        self,
        *,
        id: str = POLICY_GATE,
        catalogue: dict[str, Service],
        entitlements: dict[str, Entitlement],
        requester_role: str = "sre",
    ) -> None:
        super().__init__(id=id)
        self._catalogue = catalogue
        self._entitlements = entitlements
        self._requester_role = requester_role

    @handler
    async def run(
        self, case: IncidentCase, ctx: WorkflowContext[IncidentCase, NodeProgress | IncidentCase]
    ) -> None:
        requester_role = case.raised_by.get("persona") or self._requester_role
        decision = evaluate(
            case.plan,
            catalogue=self._catalogue,
            entitlements=self._entitlements,
            requester=self._entitlements.get(requester_role),
            impact=case.impact,
            runbooks=case.knowledge.runbooks if case.knowledge else None,
            implicated=implicated_services(
                services=case.services,
                triage_affected=case.triage.affected_services if case.triage else (),
                impact_affected=case.impact.affected_service_ids if case.impact else (),
            ),
        )
        if case.approval_ttl_minutes and decision.expires_at is not None:
            # A case may ask for a SHORTER window, never a longer one (the timeout test).
            shorter = decision.evaluated_at + timedelta(minutes=case.approval_ttl_minutes)
            if shorter < decision.expires_at:
                decision.expires_at = shorter
                decision.reasons.append(
                    f"approval window shortened to {case.approval_ttl_minutes} min for this case")
        case.policy = decision
        for reason in decision.reasons:
            case.decide(self.id, "policy", reason)

        if case.plan is None or not case.plan.steps:
            # Nothing to authorise: the planner degraded or produced no steps. The case moves on
            # to the report with its status unchanged; `needs_human` there says why.
            await ctx.yield_output(NodeProgress(
                node=self.id, case_id=case.case_id, seconds=0.0, ok=True,
                detail="no plan to authorise"))
            await ctx.yield_output(case)
            await ctx.send_message(case)
            return
        if decision.blocked:
            case.transition("rejected", by=self.id, rule="policy-blocked",
                            detail=decision.summary)
        elif decision.auto_approved:
            case.transition("approved", by=self.id, rule="policy-auto-approved",
                            detail=decision.summary)
        else:
            case.transition("awaiting_approval", by=self.id, rule="policy-approval-required",
                            detail=decision.summary)
        await ctx.yield_output(NodeProgress(
            node=self.id, case_id=case.case_id, seconds=0.0, ok=not decision.blocked,
            detail=decision.summary))
        # The case as it now stands, so the runner can persist it even if the next node suspends.
        await ctx.yield_output(case)
        await ctx.send_message(case)


PRUNE_AFTER = timedelta(minutes=10)


class ApprovalExecutor(Executor):
    """Suspends the workflow on a human, and resumes when one answers.

    The suspension is the point: `ctx.request_info` returns without sending anything onward, the
    superstep ends, a checkpoint is written, and the run goes idle. Killing the process here loses
    nothing.
    """

    def __init__(self, *, id: str = APPROVAL,
                 entitlements: dict[str, Entitlement] | None = None) -> None:
        super().__init__(id=id)
        self._entitlements = entitlements or {}
        # Cases parked on a human, by request id. Survives a checkpoint through the hooks below.
        self._pending: dict[str, IncidentCase] = {}

    def _prune(self) -> None:
        """Forget parked cases whose approval window closed more than PRUNE_AFTER ago.

        The workflow object - and so this executor - is reused for every case of its shape on a
        replica, and `_pending` used to grow for good: a case answered on the OTHER replica, resumed
        cold, or deleted by demo-reset stayed here and rode along in every later checkpoint of every
        other case (verify_phase07 found it; API-NOTES 17.10). Nothing can validly answer a request
        whose window closed; the margin leaves room for the expiry path itself.
        """
        cutoff = datetime.now(UTC) - PRUNE_AFTER
        stale = [rid for rid, case in self._pending.items()
                 if case.policy is not None and case.policy.expires_at is not None
                 and case.policy.expires_at < cutoff]
        for rid in stale:
            self._pending.pop(rid, None)
        if stale:
            log.info("approval executor pruned %d expired parked case(s)", len(stale))

    @staticmethod
    def request_id_for(case_id: str) -> str:
        """Deterministic, so an approval posted before the resume still matches its request."""
        return f"approval:{case_id}"

    @handler
    async def run(
        self, case: IncidentCase, ctx: WorkflowContext[IncidentCase, NodeProgress | IncidentCase]
    ) -> None:
        if case.status != "awaiting_approval":
            # Auto-approved or blocked: nothing to ask a human. Straight through.
            await ctx.send_message(case)
            return
        decision = case.policy or PolicyDecision()
        request_id = self.request_id_for(case.case_id)
        self._prune()
        self._pending[request_id] = case
        log.info("case %s suspended for approval by %s", case.case_id,
                 decision.required_approver_roles or ["an approver"])
        await ctx.yield_output(NodeProgress(
            node=self.id, case_id=case.case_id, seconds=0.0, ok=True,
            detail=f"suspended for {' or '.join(decision.required_approver_roles) or 'approval'}"))
        await ctx.yield_output(case)
        await ctx.request_info(request_for(case, decision), ApprovalResponse,
                               request_id=request_id)

    @response_handler
    async def answered(
        self, original_request: ApprovalRequest, response: ApprovalResponse,
        ctx: WorkflowContext[IncidentCase, NodeProgress | IncidentCase],
    ) -> None:
        """A human answered. Re-check validity here as well as at the API: this is the last gate."""
        request_id = self.request_id_for(original_request.case_id)
        case = self._pending.pop(request_id, None)
        if case is None:
            log.warning("approval for %s arrived with no parked case", original_request.case_id)
            return
        decision = case.policy or PolicyDecision()
        current_plan_id = case.plan.plan_id if case.plan else ""
        if not response.approved and response.role == EXPIRY_ROLE:
            await self._expire(case, decision, original_request, response, request_id, ctx)
            return
        ok, reason, code = approval_is_valid(
            decision, approver_role=response.role,
            approver_entitlement=self._entitlements.get(response.role),
            current_plan_id=current_plan_id,
        )
        record = ApprovalRecord(
            approved=response.approved, by=response.by, role=response.role,
            comment=response.comment, at=response.at, plan_id=original_request.plan_id,
            accepted=ok, refusal_code="" if ok else code, refusal_reason="" if ok else reason,
        )
        case.approval = record

        if not ok:
            # An invalid approval is not a rejection: the case stays parked and the request is
            # raised again, so a wrong answer cannot advance a plan by accident.
            case.decide(self.id, "approval-refused", f"{code}: {reason}")
            self._pending[request_id] = case
            log.warning("approval for %s refused (%s): %s", case.case_id, code, reason)
            await ctx.request_info(request_for(case, decision), ApprovalResponse,
                                   request_id=request_id)
            return

        if response.approved:
            case.transition("approved", by=self.id, rule="human-approved",
                            detail=f"{response.by} ({response.role}): {response.comment}")
        else:
            case.transition("rejected", by=self.id, rule="human-rejected",
                            detail=f"{response.by} ({response.role}): {response.comment}")
        await ctx.yield_output(NodeProgress(
            node=self.id, case_id=case.case_id, seconds=0.0, ok=True,
            detail=f"{'approved' if response.approved else 'rejected'} by {response.by} "
                   f"({response.role})"))
        await ctx.send_message(case)

    async def _expire(
        self, case: IncidentCase, decision: PolicyDecision, original_request: ApprovalRequest,
        response: ApprovalResponse, request_id: str,
        ctx: WorkflowContext[IncidentCase, NodeProgress | IncidentCase],
    ) -> None:
        """The window closed with no human answer: reject, but only if it really has closed.

        Deterministic on the clock. An `approval-window` rejection that arrives early - a bug, or
        someone replaying the Logic App's timeout call - is refused and the case stays parked.
        """
        now = datetime.now(UTC)
        if decision.expires_at is None or now <= decision.expires_at:
            case.decide(self.id, "approval-refused",
                        f"expired: the window is still open until {decision.expires_at}")
            self._pending[request_id] = case
            await ctx.request_info(request_for(case, decision), ApprovalResponse,
                                   request_id=request_id)
            return
        case.approval = ApprovalRecord(
            approved=False, by=response.by, role=response.role, comment=response.comment,
            at=response.at, plan_id=original_request.plan_id, accepted=True)
        case.transition("rejected", by=self.id, rule="approval-expired",
                        detail=f"no approver answered before {decision.expires_at.isoformat()}; "
                               f"nothing was dispatched")
        await ctx.yield_output(NodeProgress(
            node=self.id, case_id=case.case_id, seconds=0.0, ok=True,
            detail="rejected: the approval window expired"))
        await ctx.send_message(case)

    async def on_checkpoint_save(self) -> dict[str, Any]:
        """Persist the parked cases.

        These hooks are **async**. Override them synchronously and every checkpoint fails with
        `Failed to create checkpoint at iteration N ... this does not fail the workflow run` -
        the run looks healthy and nothing is ever written (docs/API-NOTES.md 14.1).
        """
        self._prune()
        return {"pending": {rid: case.model_dump(mode="json")
                            for rid, case in self._pending.items()}}

    async def on_checkpoint_restore(self, state: dict[str, Any]) -> None:
        self._pending = {
            rid: IncidentCase.model_validate(raw)
            for rid, raw in (state.get("pending") or {}).items()
        }
        self._prune()
        log.info("approval executor restored with %d parked case(s)", len(self._pending))


__all__ = [
    "ApprovalExecutor",
    "ApprovalRecord",
    "ApprovalRequest",
    "ApprovalResponse",
    "PolicyGateExecutor",
    "request_for",
]
