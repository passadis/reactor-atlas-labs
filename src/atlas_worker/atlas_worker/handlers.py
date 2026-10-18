"""Step handlers: what a runbook step does to the simulated estate.

A step's `command_ref` is a runbook reference ("RB-0042#3"), never a shell command. The runbook id
picks a handler; anything unrecognised gets the generic handler, which is still a recorded,
bounded, simulated action - there is no path from a plan to arbitrary execution.

Every handler writes the state change it made to Cosmos (`control` container, one document per
service) and takes a realistic 2-5 s so the progress stream is visible on stage.

RB-0099 fails on purpose while the `fault-rb-0099` switch is on (the default): that is the retry,
dead-letter and replay demo. `scripts/dlq_replay.py --fix` turns the switch off before replaying.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

CONTROL = "control"
FAULT_DOC = "fault-rb-0099"
FAULTY_RUNBOOK = "RB-0099"


class TransientStepError(RuntimeError):
    """A failure worth retrying. The message is abandoned and redelivered."""


def runbook_of(command_ref: str) -> str:
    return command_ref.split("#", 1)[0].strip().upper()


def step_seconds(plan_id: str, order: int, *, low: float = 2.0, high: float = 5.0) -> float:
    """Deterministic per plan and step, so the same demo takes the same time every run."""
    digest = int(hashlib.sha256(f"{plan_id}:{order}".encode()).hexdigest()[:8], 16)
    return low + (digest % 1000) / 1000 * (high - low)


async def fault_enabled(store: Any) -> bool:
    rows = await store.query(CONTROL, "SELECT * FROM c WHERE c.id = @id",
                             [{"name": "@id", "value": FAULT_DOC}], partition_key=FAULT_DOC)
    # Absent means ON: a fresh environment demonstrates the failure without any setup.
    return bool(rows[0].get("enabled", True)) if rows else True


async def record_state(store: Any, step: dict[str, Any], *, operation_id: str,
                       outcome: str) -> None:
    service = str(step.get("target_service") or "unknown")
    await store.upsert(CONTROL, {
        "id": f"state:{service}", "kind": "service-state", "service_id": service,
        "last_action": step.get("action"), "command_ref": step.get("command_ref"),
        "operation_id": operation_id, "outcome": outcome,
        "at": datetime.now(UTC).isoformat(),
    })


Handler = Callable[[Any, dict[str, Any], str, str], Awaitable[str]]


async def generic(store: Any, step: dict[str, Any], plan_id: str, operation_id: str) -> str:
    await asyncio.sleep(step_seconds(plan_id, int(step.get("order", 0))))
    await record_state(store, step, operation_id=operation_id, outcome="applied")
    return f"{step.get('action')} applied to {step.get('target_service')}"


async def faulty(store: Any, step: dict[str, Any], plan_id: str, operation_id: str) -> str:
    await asyncio.sleep(step_seconds(plan_id, int(step.get("order", 0)), low=1.0, high=2.0))
    if await fault_enabled(store):
        raise TransientStepError(
            f"{FAULTY_RUNBOOK}: the target did not converge (simulated fault; "
            "scripts/dlq_replay.py --fix clears it)")
    await record_state(store, step, operation_id=operation_id, outcome="applied")
    return f"{step.get('action')} applied to {step.get('target_service')} (fault cleared)"


REGISTRY: dict[str, Handler] = {FAULTY_RUNBOOK: faulty}


def handler_for(command_ref: str) -> Handler:
    return REGISTRY.get(runbook_of(command_ref), generic)


__all__ = ["FAULTY_RUNBOOK", "FAULT_DOC", "REGISTRY", "TransientStepError", "fault_enabled",
           "handler_for", "runbook_of", "step_seconds"]
