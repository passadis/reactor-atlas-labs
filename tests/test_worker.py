"""Phase 10: the remediation worker's executor, against an in-memory Cosmos.

What must hold, whatever the transport does:
- a command without a spent approval token is refused, audited, and changes nothing;
- a finished operation is never executed twice (duplicate delivery);
- a failing step is retried, and on the 5th delivery the case learns it failed and the message is
  dead-lettered;
- a replay after the fix resumes after the last succeeded step.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from atlas_common.remediation import ExecutionRecord, ProgressEvent, RemediationCommand
from atlas_worker.executor import MAX_DELIVERIES, CommandExecutor

from atlas_worker import handlers


class Store:
    """Just enough of CosmosStore: containers of documents keyed by id."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, dict[str, Any]]] = {}

    async def upsert(self, container: str, doc: dict[str, Any]) -> None:
        self.data.setdefault(container, {})[doc["id"]] = dict(doc)

    async def query(self, container: str, query: str, params: list[dict[str, Any]],
                    partition_key: str | None = None) -> list[dict[str, Any]]:
        docs = list(self.data.get(container, {}).values())
        value = params[0]["value"] if params else None
        if "used_by_operation" in query:
            return [d for d in docs if d.get("kind") == "approval-token"
                    and d.get("used_by_operation") == value]
        return [d for d in docs if d.get("id") == value]


CASE, PLAN, OP = "CASE-TEST01", "0123456789abcdef", "OP-TEST01"


def seed(store: Store, *, steps: list[dict[str, Any]], token_spent: bool = True) -> None:
    store.data["cases"] = {CASE: {"id": CASE, "case_id": CASE, "state": {
        "plan": {"plan_id": PLAN, "steps": steps}}}}
    store.data["approvals"] = {f"operation:{OP}": {
        "id": f"operation:{OP}", "case_id": CASE, "kind": "operation", "operation_id": OP,
        "plan_id": PLAN, "status": "queued"}}
    if token_spent:
        store.data["approvals"]["token:x"] = {
            "id": "token:x", "kind": "approval-token", "case_id": CASE, "plan_id": PLAN,
            "used": True, "used_by_operation": OP, "approved_by": "Dana",
            "approver_role": "director"}


def step(order: int, runbook: str = "RB-0042") -> dict[str, Any]:
    return {"order": order, "action": f"step {order}", "target_service": "svc-payments",
            "command_ref": f"{runbook}#{order}", "destructive": False,
            "expected_result": "ok", "rollback": "none"}


@pytest.fixture(autouse=True)
def fast_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "step_seconds", lambda *a, **k: 0.0)


def run(store: Store, delivery: int = 1) -> tuple[Any, list[ProgressEvent], list[ExecutionRecord]]:
    events: list[ProgressEvent] = []
    results: list[ExecutionRecord] = []

    async def publish(e: ProgressEvent) -> None:
        events.append(e)

    async def send(r: ExecutionRecord) -> None:
        results.append(r)

    executor = CommandExecutor(store, publish=publish, send_result=send)
    command = RemediationCommand(operation_id=OP, case_id=CASE, plan_id=PLAN,
                                 submitted_at=datetime.now(UTC))
    outcome = asyncio.run(executor.execute(command, message_id=PLAN, delivery_count=delivery))
    return outcome, events, results


def test_the_happy_path_runs_every_step_in_order() -> None:
    store = Store()
    seed(store, steps=[step(2), step(1), step(3)])
    outcome, events, results = run(store)
    assert outcome.settle == "complete"
    assert [e.step.order for e in events if e.step.status == "succeeded"] == [1, 2, 3]
    assert results[0].status == "succeeded" and len(results[0].steps) == 3
    assert store.data["approvals"][f"operation:{OP}"]["status"] == "succeeded"
    assert store.data["control"]["state:svc-payments"]["outcome"] == "applied"


def test_a_forged_command_is_refused_audited_and_changes_nothing() -> None:
    store = Store()
    seed(store, steps=[step(1)], token_spent=False)
    outcome, events, results = run(store)
    assert outcome.settle == "dead_letter" and outcome.reason == "rejected:no_spent_token"
    assert not events and not results
    assert "control" not in store.data  # no simulated state change
    refusals = [d for d in store.data["approvals"].values() if d.get("kind") == "worker-refusal"]
    assert len(refusals) == 1 and refusals[0]["code"] == "no_spent_token"


def test_a_command_for_an_unknown_operation_is_refused() -> None:
    store = Store()
    seed(store, steps=[step(1)])
    del store.data["approvals"][f"operation:{OP}"]
    outcome, _, _ = run(store)
    assert outcome.reason == "rejected:no_operation"


def test_a_duplicate_delivery_executes_nothing() -> None:
    store = Store()
    seed(store, steps=[step(1)])
    run(store)
    outcome, events, results = run(store, delivery=2)
    assert outcome.settle == "complete" and "duplicate" in outcome.reason
    assert not events and not results


def test_a_failing_step_retries_then_dead_letters_on_the_last_delivery() -> None:
    store = Store()
    seed(store, steps=[step(1), step(2, handlers.FAULTY_RUNBOOK), step(3)])
    for delivery in range(1, MAX_DELIVERIES):
        outcome, _, results = run(store, delivery=delivery)
        assert outcome.settle == "abandon" and not results
    outcome, _, results = run(store, delivery=MAX_DELIVERIES)
    assert outcome.settle == "dead_letter"
    assert results[0].status == "failed" and "step 2" in results[0].detail
    op = store.data["approvals"][f"operation:{OP}"]
    assert op["status"] == "failed"
    # Step 1 ran once, not five times: completed steps are never repeated on redelivery.
    assert [s["order"] for s in op["step_results"] if s["status"] == "succeeded"] == [1]


def test_a_replay_after_the_fix_resumes_after_the_last_good_step() -> None:
    store = Store()
    seed(store, steps=[step(1), step(2, handlers.FAULTY_RUNBOOK), step(3)])
    run(store, delivery=1)  # step 1 succeeds, step 2 fails
    store.data.setdefault("control", {})[handlers.FAULT_DOC] = {
        "id": handlers.FAULT_DOC, "enabled": False}
    store.data["approvals"][f"operation:{OP}"]["status"] = "queued"  # what dlq_replay does
    outcome, events, results = run(store, delivery=1)
    assert outcome.settle == "complete" and results[0].status == "succeeded"
    ran = [e.step.order for e in events if e.step.status == "succeeded"]
    assert ran == [2, 3]


def test_an_unrecognised_runbook_still_gets_a_bounded_simulated_handler() -> None:
    assert handlers.handler_for("RB-4242#1") is handlers.generic
    assert handlers.handler_for("rb-0099#2") is handlers.faulty
