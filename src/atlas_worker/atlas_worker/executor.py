"""Execute one remediation command. Transport-free, so it is testable without Service Bus.

`execute` returns what to do with the message - complete it, abandon it for a retry, or dead-letter
it - and the receiver loop in `worker.py` does exactly that. Everything that must survive a crash is
in Cosmos before the message is settled:

- the operation document (`approvals` container, `operation:<id>`) holds each step's result, so a
  redelivery or a replay resumes after the last succeeded step instead of re-running it;
- an operation already finished is a duplicate delivery: completed, nothing re-executed.

Exactly-once is layered: the ops API's idempotency key on `plan_id`, Service Bus duplicate
detection on `MessageId = plan_id`, and this operation-status check.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from atlas_common.approval_tokens import APPROVALS
from atlas_common.remediation import (
    ExecutionRecord,
    ProgressEvent,
    RemediationCommand,
    StepResult,
)

from .authorise import audit_refusal, authorise
from .handlers import TransientStepError, handler_for

log = logging.getLogger("atlas.worker.executor")

MAX_DELIVERIES = 5  # matches remediation-commands maxDeliveryCount (messaging.bicep)

Settle = Literal["complete", "abandon", "dead_letter"]


@dataclass
class Outcome:
    settle: Settle
    reason: str
    record: ExecutionRecord | None = None


PublishEvent = Callable[[ProgressEvent], Awaitable[None]]
SendResult = Callable[[ExecutionRecord], Awaitable[None]]


def _now() -> datetime:
    return datetime.now(UTC)


class CommandExecutor:
    def __init__(self, store: Any, *, publish: PublishEvent, send_result: SendResult) -> None:
        self._store, self._publish, self._send_result = store, publish, send_result

    async def execute(self, command: RemediationCommand, *, message_id: str,
                      delivery_count: int) -> Outcome:
        check = await authorise(self._store, command)
        if not check.ok:
            await audit_refusal(self._store, command, check, message_id=message_id,
                                delivery_count=delivery_count)
            log.warning("refused %s: %s (%s)", command.operation_id, check.code, check.reason)
            # Dead-lettered, not completed: an unauthorised command is a security event and
            # belongs where the DLQ alert (Phase 11) will see it. Nothing was executed.
            return Outcome("dead_letter", f"rejected:{check.code}")

        operation = check.operation
        if operation.get("status") in ("succeeded", "failed"):
            return Outcome("complete", f"duplicate delivery; operation already "
                                       f"{operation.get('status')}")

        done = {int(s["order"]): s for s in operation.get("step_results") or []
                if s.get("status") == "succeeded"}
        started_at = (datetime.fromisoformat(str(operation["started_at"]))
                      if operation.get("started_at") else _now())
        operation.update({"status": "running", "started_at": started_at.isoformat(),
                          "steps_total": len(check.steps), "updated_at": _now().isoformat(),
                          "detail": f"executing (delivery {delivery_count})"})
        await self._store.upsert(APPROVALS, operation)

        results: list[StepResult] = [StepResult.model_validate(s) for s in done.values()]
        for raw in check.steps:
            order = int(raw.get("order", 0))
            if order in done:
                continue
            step = StepResult(order=order, action=str(raw.get("action", "")),
                              target_service=str(raw.get("target_service", "")),
                              command_ref=str(raw.get("command_ref", "")), status="started",
                              started_at=_now(), attempt=delivery_count)
            await self._emit(command, step, len(check.steps))
            try:
                detail = await handler_for(step.command_ref)(
                    self._store, raw, command.plan_id, command.operation_id)
            except TransientStepError as exc:
                step = step.model_copy(update={"status": "failed", "detail": str(exc),
                                               "finished_at": _now()})
                await self._emit(command, step, len(check.steps))
                return await self._failed_attempt(command, operation, results, step,
                                                  delivery_count)
            step = step.model_copy(update={"status": "succeeded", "detail": detail,
                                           "finished_at": _now()})
            results.append(step)
            await self._emit(command, step, len(check.steps))
            operation["step_results"] = [r.model_dump(mode="json") for r in results]
            operation["steps_completed"] = len(results)
            operation["updated_at"] = _now().isoformat()
            await self._store.upsert(APPROVALS, operation)

        record = ExecutionRecord(
            operation_id=command.operation_id, case_id=command.case_id, plan_id=command.plan_id,
            status="succeeded", steps=results, steps_total=len(check.steps),
            detail=f"all {len(results)} steps applied", started_at=started_at,
            delivery_count=delivery_count)
        await self._finish(operation, record)
        return Outcome("complete", "succeeded", record)

    async def _emit(self, command: RemediationCommand, step: StepResult, total: int) -> None:
        try:
            await self._publish(ProgressEvent(operation_id=command.operation_id,
                                              case_id=command.case_id, plan_id=command.plan_id,
                                              step=step, steps_total=total))
        except Exception:
            # Progress is for people watching; losing one event must never fail the remediation.
            log.warning("progress event for %s step %s not published", command.operation_id,
                        step.order, exc_info=True)

    async def _failed_attempt(self, command: RemediationCommand, operation: dict[str, Any],
                              results: list[StepResult], failed: StepResult,
                              delivery_count: int) -> Outcome:
        operation["step_results"] = [r.model_dump(mode="json") for r in results]
        operation["last_error"] = failed.detail
        operation["updated_at"] = _now().isoformat()
        if delivery_count < MAX_DELIVERIES:
            operation["detail"] = (f"step {failed.order} failed on delivery {delivery_count} of "
                                   f"{MAX_DELIVERIES}; retrying")
            await self._store.upsert(APPROVALS, operation)
            return Outcome("abandon", f"transient failure at step {failed.order}")
        # The last delivery: the case learns it failed now, and the message goes to the DLQ for a
        # human (scripts/dlq_inspect.py, then dlq_replay.py after a fix).
        record = ExecutionRecord(
            operation_id=command.operation_id, case_id=command.case_id, plan_id=command.plan_id,
            status="failed", steps=[*results, failed], steps_total=int(
                operation.get("steps_total") or 0),
            detail=f"step {failed.order} failed after {delivery_count} deliveries: "
                   f"{failed.detail}",
            started_at=datetime.fromisoformat(str(operation["started_at"]))
            if operation.get("started_at") else None, delivery_count=delivery_count)
        await self._finish(operation, record)
        return Outcome("dead_letter", f"step {failed.order} failed {MAX_DELIVERIES} times", record)

    async def _finish(self, operation: dict[str, Any], record: ExecutionRecord) -> None:
        operation.update({"status": record.status, "detail": record.detail,
                          "finished_at": record.finished_at.isoformat(),
                          "updated_at": _now().isoformat(),
                          "step_results": [s.model_dump(mode="json") for s in record.steps]})
        await self._store.upsert(APPROVALS, operation)
        await self._send_result(record)


__all__ = ["MAX_DELIVERIES", "CommandExecutor", "Outcome"]
