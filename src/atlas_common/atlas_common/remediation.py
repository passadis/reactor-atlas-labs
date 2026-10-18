"""The asynchronous remediation contract: what travels over Service Bus, and in which order.

Three messages, one per hop, all typed:

- `RemediationCommand` - the ops API enqueues it on `remediation-commands` once the approval token
  has been checked and spent. It carries NO plan and NO token: the worker reads the plan from the
  case and proves authorisation from the spent-token record, so a message forged straight onto the
  queue has nothing to execute and nothing to show for itself.
- `ProgressEvent` - the worker publishes one to the `remediation-events` topic per step transition.
- `ExecutionRecord` - the worker's final word, on `remediation-results` and in Cosmos.

Service Bus properties are part of the contract too (see the ops API's enqueue and the worker):
`MessageId = plan_id` (duplicate detection, 10 minutes), `SessionId = first target service`
(ordering per service), `CorrelationId = case_id`, application property `traceparent`.
"""

from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

COMMANDS_QUEUE = "remediation-commands"
RESULTS_QUEUE = "remediation-results"
EVENTS_TOPIC = "remediation-events"
ORCHESTRATOR_SUBSCRIPTION = "orchestrator-progress"

StepStatus = Literal["started", "succeeded", "failed", "skipped"]
OperationStatus = Literal["queued", "running", "succeeded", "failed", "rejected"]


def _now() -> datetime:
    return datetime.now(UTC)


class RemediationCommand(BaseModel):
    """The queue message. Deliberately thin: authority lives in Cosmos, not in the message."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    case_id: str
    plan_id: str
    submitted_at: datetime


class StepResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: int
    action: str
    target_service: str
    command_ref: str
    status: StepStatus
    detail: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    attempt: int = 1


class ProgressEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str
    case_id: str
    plan_id: str
    step: StepResult
    steps_total: int
    at: datetime = Field(default_factory=_now)


class ExecutionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str
    case_id: str
    plan_id: str
    status: OperationStatus
    steps: list[StepResult] = Field(default_factory=list)
    steps_total: int = 0
    detail: str = ""
    started_at: datetime | None = None
    finished_at: datetime = Field(default_factory=_now)
    delivery_count: int = 1


def service_bus_namespace() -> str:
    """The fully qualified namespace, from the environment every workload already carries."""
    fqdn = os.environ.get("ATLAS_SERVICEBUS_FQDN", "").strip()
    if not fqdn:
        raise RuntimeError("ATLAS_SERVICEBUS_FQDN is not set")
    return fqdn


def child_traceparent(traceparent: str | None, fallback_trace_id: str = "") -> str:
    """A valid W3C traceparent in the same trace: same trace id, a fresh span id.

    A span id of all zeros is invalid per W3C and silently dropped by some SDKs, which breaks the
    one-waterfall promise at exactly the Service Bus hop.
    """
    trace_id = fallback_trace_id
    if traceparent:
        parts = traceparent.split("-")
        if len(parts) == 4 and len(parts[1]) == 32:
            trace_id = parts[1]
    if len(trace_id) != 32:
        trace_id = secrets.token_hex(16)
    return f"00-{trace_id}-{secrets.token_hex(8)}-01"


__all__ = [
    "COMMANDS_QUEUE",
    "EVENTS_TOPIC",
    "ORCHESTRATOR_SUBSCRIPTION",
    "RESULTS_QUEUE",
    "ExecutionRecord",
    "OperationStatus",
    "ProgressEvent",
    "RemediationCommand",
    "StepResult",
    "StepStatus",
    "child_traceparent",
    "service_bus_namespace",
]
