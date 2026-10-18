"""Is this command authorised? Proven from Cosmos, never from the message.

The queue message carries an operation id, a case id and a plan id - and nothing that grants
anything. A command is executable only if ALL of these hold:

1. the operation exists (the ops API wrote it when it accepted the submission);
2. an approval token for exactly this case and plan was spent BY THIS operation - the ops API spends
   it with an ETag-guarded replace, so there is at most one;
3. the case exists and still holds this plan.

A message put straight onto the queue - by someone with Service Bus send rights but no approval -
fails 1 or 2 and is refused with an audit record and no side effects. Single use is inherited: a
token can only ever be spent by one operation, so a second command reusing it has no record.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from atlas_common.approval_tokens import APPROVALS
from atlas_common.remediation import RemediationCommand

CASES = "cases"

RefusalCode = Literal["no_operation", "no_spent_token", "no_case", "plan_changed", "no_plan"]


@dataclass
class Authorisation:
    ok: bool
    code: RefusalCode | Literal["ok"]
    reason: str
    operation: dict[str, Any] = field(default_factory=dict)
    case: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)


async def _one(store: Any, container: str, query: str, params: list[dict[str, Any]],
               partition: str) -> dict[str, Any] | None:
    rows = await store.query(container, query, params, partition_key=partition)
    return dict(rows[0]) if rows else None


async def authorise(store: Any, command: RemediationCommand) -> Authorisation:
    operation = await _one(
        store, APPROVALS, "SELECT * FROM c WHERE c.id = @id",
        [{"name": "@id", "value": f"operation:{command.operation_id}"}], command.case_id)
    if operation is None or operation.get("plan_id") != command.plan_id:
        return Authorisation(False, "no_operation",
                             "no accepted submission exists for this operation and plan")

    token = await _one(
        store, APPROVALS,
        "SELECT * FROM c WHERE c.kind = 'approval-token' AND c.used_by_operation = @op",
        [{"name": "@op", "value": command.operation_id}], command.case_id)
    if (token is None or not token.get("used") or token.get("plan_id") != command.plan_id
            or token.get("case_id") != command.case_id):
        return Authorisation(False, "no_spent_token",
                             "no approval token was spent for this operation", operation)

    case = await _one(store, CASES, "SELECT * FROM c WHERE c.id = @id",
                      [{"name": "@id", "value": command.case_id}], command.case_id)
    if case is None:
        return Authorisation(False, "no_case", "the case does not exist", operation)
    plan = ((case.get("state") or {}).get("plan") or {})
    if plan.get("plan_id") != command.plan_id:
        return Authorisation(False, "plan_changed",
                             "the case no longer holds the approved plan", operation, case)
    steps = sorted(plan.get("steps") or [], key=lambda s: int(s.get("order", 0)))
    if not steps:
        return Authorisation(False, "no_plan", "the approved plan has no steps", operation, case)
    return Authorisation(True, "ok",
                         f"approved by {token.get('approved_by')} ({token.get('approver_role')})",
                         operation, case, steps)


async def audit_refusal(store: Any, command: RemediationCommand, check: Authorisation, *,
                        message_id: str, delivery_count: int) -> None:
    """Every refused command leaves a record next to the case's approvals."""
    await store.upsert(APPROVALS, {
        "id": f"worker-refusal:{command.operation_id}:{secrets.token_hex(4)}",
        "kind": "worker-refusal", "case_id": command.case_id,
        "operation_id": command.operation_id, "plan_id": command.plan_id,
        "code": check.code, "reason": check.reason, "message_id": message_id,
        "delivery_count": delivery_count, "at": datetime.now(UTC).isoformat(),
    })


__all__ = ["Authorisation", "audit_refusal", "authorise"]
