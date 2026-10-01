"""After the human: open the ticket, dispatch the approved plan, and follow it to the end.

Three pieces, all deterministic code - no model decides anything here:

- **`Dispatcher`** runs once a case is `approved`. It opens the ticket (idempotent on the case id),
  mints the single-use approval token, and submits the command through the gateway. The ops API
  checks and spends the token and puts the command on Service Bus, so the path is
  orchestrator -> APIM -> Function -> Service Bus -> worker, and the model never touches it.
- **`ApprovalWorkflow`** is the bridge to the Logic App `approve-remediation`: it starts a run when
  a case parks, and completes the run's webhook wait once the orchestrator has decided. The Logic
  App collects and notifies; the orchestrator decides. Say that on stage.
- **`RemediationListener`** consumes `remediation-results` (the case's final word) and the
  `orchestrator-progress` subscription on `remediation-events` (live steps), and writes both onto
  the case, where the UI reads them.

Every external call here is best-effort with a typed outcome: a failed dispatch leaves the case
`approved` with `remediation.status = dispatch_failed` and a retry endpoint, never a stack trace.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from atlas_common.ops_client import OpsApiClient, OpsApiError
from atlas_common.remediation import (
    EVENTS_TOPIC,
    ORCHESTRATOR_SUBSCRIPTION,
    RESULTS_QUEUE,
    ExecutionRecord,
    ProgressEvent,
    service_bus_namespace,
)

from atlas_common import approval_tokens

from .models import IncidentCase
from .store import CaseRepository

log = logging.getLogger("atlas.orchestrator.remediation")

DISPATCHER = "dispatch"
CALLBACK_TIMEOUT_S = 15.0


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Dispatcher:
    def __init__(self, cases: CaseRepository, store: Any, ops: OpsApiClient | None) -> None:
        self._cases, self._store, self._ops = cases, store, ops

    async def dispatch(self, case: IncidentCase) -> dict[str, Any]:
        """Ticket, token, submit. Idempotent: a case already dispatched is returned as it is."""
        doc = await self._cases.get(case.case_id) or {}
        existing = doc.get("remediation") or {}
        if existing.get("operation_id"):
            return dict(existing)
        if case.status != "approved" or case.plan is None or not case.plan.steps:
            return {"status": "not_dispatched", "detail": f"case is {case.status}"}
        if self._ops is None:
            return await self._record(case, {
                "status": "dispatch_failed",
                "detail": "the agent tools are not routed through the gateway (ATLAS_OPS_API_URL)"})

        approval = case.approval
        try:
            ticket, ticket_replayed = await self._ops.create_ticket(
                case_id=case.case_id, severity=case.triage.severity if case.triage else "Sev3",
                summary=(case.report[:480] if len(case.report) >= 8 else case.report.ljust(8)),
                service_ids=case.services)
            await self._cases.set_fields(case.case_id, {"ticket": {
                "ticket_id": ticket.get("ticket_id"), "status": ticket.get("status"),
                "replayed": ticket_replayed, "at": _now()}})

            minted = await approval_tokens.mint(
                self._store, case_id=case.case_id, plan_id=case.plan.plan_id,
                approved_by=approval.by if approval else "unknown",
                approver_role=approval.role if approval else "unknown")
            operation, replayed = await self._ops.submit_remediation(
                case_id=case.case_id, plan_id=case.plan.plan_id,
                approval_token=minted.token)
        except OpsApiError as exc:
            log.warning("dispatch of %s failed: %s", case.case_id, exc)
            return await self._record(case, {
                "status": "dispatch_failed", "detail": exc.message,
                "correlation_id": exc.correlation_id})

        case.transition("executing", by=DISPATCHER, rule="approved-plan-dispatched",
                        detail=f"operation {operation.get('operation_id')}, ticket "
                               f"{ticket.get('ticket_id')}")
        record: dict[str, Any] = {"status": "queued",
                                  "operation_id": operation.get("operation_id"),
                  "plan_id": case.plan.plan_id, "steps_total": len(case.plan.steps),
                  "steps": [], "replayed": replayed, "dispatched_at": _now(),
                  "ticket_id": ticket.get("ticket_id")}
        await self._cases.save(case)
        await self._cases.set_fields(case.case_id, {"remediation": record})
        log.info("case %s dispatched as %s (ticket %s)", case.case_id,
                 record["operation_id"], record["ticket_id"])
        return record

    async def _record(self, case: IncidentCase, record: dict[str, Any]) -> dict[str, Any]:
        record = {**record, "at": _now(), "plan_id": case.plan.plan_id if case.plan else ""}
        await self._cases.set_fields(case.case_id, {"remediation": record})
        return record


class ApprovalWorkflow:
    """Start the Logic App approval run, and complete its webhook wait after the decision."""

    def __init__(self, cases: CaseRepository, store: Any,
                 workflows: OpsApiClient | None, console_url: str) -> None:
        self._cases, self._store = cases, store
        self._workflows, self._console = workflows, console_url.rstrip("/")

    async def start(self, case: IncidentCase) -> dict[str, Any]:
        if self._workflows is None or case.policy is None or case.plan is None:
            return {"status": "not_started", "detail": "no approval workflow configured"}
        body = {
            "case_id": case.case_id, "plan_id": case.plan.plan_id,
            "required_roles": case.policy.required_approver_roles,
            "expires_at": case.policy.expires_at.isoformat() if case.policy.expires_at else None,
            "timeout": _iso_duration(case.policy.expires_at),
            "summary": case.report[:300],
            "console_url": f"{self._console}/approvals?case={case.case_id}",
        }
        try:
            response = await self._workflows.request("POST", "/approvals", json=body)
            record = {"status": "started", "run_id": response.headers.get(
                "x-ms-workflow-run-id", ""), "at": _now()}
        except OpsApiError as exc:
            # The console still works without the workflow: it is notification, not authority.
            record = {"status": "start_failed", "detail": exc.message, "at": _now()}
        await self._cases.set_fields(case.case_id, {"approval_workflow": record})
        return record

    async def complete(self, case_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        """Post the decision to the run's webhook callback, if one registered. Best effort."""
        rows = await self._store.query(
            approval_tokens.APPROVALS, "SELECT * FROM c WHERE c.id = @id",
            [{"name": "@id", "value": f"callback:{case_id}"}], partition_key=case_id)
        if not rows or not rows[0].get("callback_url") or rows[0].get("status") == "completed":
            return {"status": "no_callback"}
        callback = dict(rows[0])
        try:
            async with httpx.AsyncClient(timeout=CALLBACK_TIMEOUT_S) as http:
                response = await http.post(str(callback["callback_url"]), json=decision)
            # 404/409: the run's wait already ended (it timed out and asked for the expiry that
            # produced this very decision). Nothing is waiting any more; that is not a failure.
            status = ("completed" if response.status_code < 300 else
                      "already_closed" if response.status_code in (404, 409) else "rejected")
            outcome = {"status": status, "http_status": response.status_code}
        except httpx.HTTPError as exc:
            outcome = {"status": "failed", "detail": type(exc).__name__}
        callback.update({**outcome, "decision": decision, "completed_at": _now()})
        await self._store.upsert(approval_tokens.APPROVALS, callback)
        return outcome


    async def complete_eventually(self, case_id: str, decision: dict[str, Any], *,
                                  attempts: int = 7, every_s: float = 5.0) -> dict[str, Any]:
        """`complete`, retried while the run has not registered its callback yet.

        A decision can beat the registration: the run subscribes ~1.5 s after the case parks, and
        an approval right away (the end-to-end verifier, or a fast human) found nothing to call -
        leaving the run waiting out its full window. Retrying for ~30 s closes that race.
        """
        outcome: dict[str, Any] = {"status": "no_callback"}
        for attempt in range(attempts):
            outcome = await self.complete(case_id, decision)
            if outcome.get("status") != "no_callback":
                log.info("approval run for %s: callback %s (attempt %d)", case_id,
                         outcome.get("status"), attempt + 1)
                return outcome
            doc = await self._cases.get(case_id) or {}
            started = (doc.get("approval_workflow") or {}).get("status")
            # Give up early only on a start that is KNOWN not to have happened. "No record yet" is
            # not that: the start call runs in the background too, and an instant decision can
            # arrive before it has written its marker (API-NOTES 17.6).
            if started in ("start_failed", "not_started"):
                log.info("approval run for %s was never started (%s); nothing to complete",
                         case_id, started)
                return outcome
            if attempt < attempts - 1:
                await asyncio.sleep(every_s)
        log.warning("approval run for %s never registered a callback", case_id)
        return outcome


def _iso_duration(expires_at: datetime | None) -> str:
    """The Logic App's webhook timeout: whatever is left of the approval window, at least 1 min."""
    if expires_at is None:
        return "PT30M"
    seconds = max(60, int((expires_at - datetime.now(UTC)).total_seconds()))
    return f"PT{seconds}S"


class RemediationListener:
    """Consume results and progress. Runs on every replica; whichever receives a message
    applies it to Cosmos, which is where the UI and the other replica read it."""

    def __init__(self, cases: CaseRepository, credential: Any,
                 on_update: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self._cases, self._credential = cases, credential
        self._on_update = on_update
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        try:
            namespace = service_bus_namespace()
        except RuntimeError:
            log.warning("ATLAS_SERVICEBUS_FQDN not set; remediation listener not started")
            return
        self._tasks = [asyncio.create_task(self._loop(namespace, "results")),
                       asyncio.create_task(self._loop(namespace, "events"))]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _loop(self, namespace: str, kind: str) -> None:
        from azure.servicebus.aio import ServiceBusClient

        backoff = 1.0
        while True:
            try:
                async with ServiceBusClient(namespace, credential=self._credential) as client:
                    receiver = (client.get_queue_receiver(RESULTS_QUEUE, max_wait_time=30)
                                if kind == "results" else
                                client.get_subscription_receiver(
                                    EVENTS_TOPIC, ORCHESTRATOR_SUBSCRIPTION, max_wait_time=30))
                    async with receiver:
                        backoff = 1.0
                        while True:
                            for message in await receiver.receive_messages(
                                    max_message_count=20, max_wait_time=10):
                                try:
                                    payload = json.loads(str(message))
                                    if kind == "results":
                                        await self.apply_result(
                                            ExecutionRecord.model_validate(payload))
                                    else:
                                        await self.apply_progress(
                                            ProgressEvent.model_validate(payload))
                                    await receiver.complete_message(message)
                                except Exception:
                                    log.exception("could not apply %s message", kind)
                                    await receiver.abandon_message(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("remediation %s listener failed; retrying in %.0f s", kind, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def apply_progress(self, event: ProgressEvent) -> None:
        doc = await self._cases.get(event.case_id)
        if doc is None:
            return
        record = dict(doc.get("remediation") or {})
        if record.get("operation_id") not in (None, event.operation_id):
            return
        steps = {int(s["order"]): s for s in record.get("steps") or []}
        steps[event.step.order] = event.step.model_dump(mode="json")
        record.update({"operation_id": event.operation_id, "steps": [
            steps[k] for k in sorted(steps)], "steps_total": event.steps_total,
            "updated_at": _now()})
        if record.get("status") in (None, "queued", "replaying"):
            record["status"] = "running"
        if record.get("status") in ("succeeded", "failed", "dead_lettered"):
            return  # a straggler after the result: the result already holds every step
        await self._cases.patch_remediation(event.case_id, record)
        if self._on_update:
            self._on_update(event.case_id, {"kind": "remediation_progress",
                                            "step": event.step.model_dump(mode="json")})

    async def apply_result(self, result: ExecutionRecord) -> None:
        doc = await self._cases.get(result.case_id)
        if doc is None or not doc.get("state"):
            log.info("result for unknown case %s (operation %s) ignored", result.case_id,
                     result.operation_id)
            return
        case = IncidentCase.model_validate(doc["state"])
        record = dict(doc.get("remediation") or {})
        record.update({"operation_id": result.operation_id, "detail": result.detail,
                       "steps": [s.model_dump(mode="json") for s in result.steps],
                       "steps_total": result.steps_total,
                       "finished_at": result.finished_at.isoformat(),
                       "delivery_count": result.delivery_count})
        if result.status == "succeeded":
            record["status"] = "succeeded"
            if case.status == "executing":
                case.transition("completed", by="worker", rule="remediation-succeeded",
                                detail=result.detail)
        else:
            # Dead-lettered is parked for a human, not terminal: the case stays `executing` so a
            # replay after the fix can still complete it (scripts/dlq_replay.py).
            record["status"] = "dead_lettered"
            case.decide("worker", "remediation-dead-lettered", result.detail)
        await self._cases.save(case)
        # Last, unconditional, whole record: the result is the authority on how it ended.
        await self._cases.patch_remediation(result.case_id, record, only_if_open=False)
        if self._on_update:
            self._on_update(result.case_id, {"kind": "remediation_result",
                                             "status": record["status"]})


__all__ = ["ApprovalWorkflow", "Dispatcher", "RemediationListener"]
