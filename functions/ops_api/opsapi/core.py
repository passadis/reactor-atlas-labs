"""The operations API's behaviour, independent of the Functions host.

`function_app.py` is a thin adapter: it turns an HTTP request into a call here and a `Result` back
into an HTTP response. Everything that matters - validation, the error envelope, idempotency, the
approval-token check - lives in this module, so it is unit-tested without a Functions host and the
same code runs in Azure.

Rules this module holds:

- Every response carries a correlation id, taken from the incoming W3C `traceparent` when there is
  one, so an error seen by the model can be found in the trace.
- Errors are always `{code, message, correlation_id}`. Never a stack trace, never an internal
  detail: an exception becomes `internal` with a generic message, and the detail goes to the log.
- Every store call has a tight timeout; a slow backend becomes a typed 503 the caller may retry.
- Both writes are idempotent on `Idempotency-Key`. The first request claims the key with a create
  that fails if it exists, so two concurrent first requests cannot both do the work.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import atlas_common.approval_tokens as approval_tokens
from atlas_common.graph import blast_radius
from atlas_common.models import Severity
from atlas_common.repositories import CosmosStore, RepositoryUnavailableError
from atlas_common.repositories.cosmos_repos import (
    ChangeRepository,
    IncidentRepository,
    ServiceRepository,
    TelemetryRepository,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger("atlas.opsapi")

STORE_TIMEOUT_S = 5.0
TICKETS = "tickets"
IDEMPOTENCY = "idempotency"
APPROVALS = approval_tokens.APPROVALS
CASES = "cases"

ErrorCode = Literal["invalid_request", "not_found", "conflict", "approval_invalid",
                    "unavailable", "internal"]

SERVICE_ID = re.compile(r"^svc-[a-z0-9-]+$")
INCIDENT_ID = re.compile(r"^INC-\d{4}-\d{4}$")
TICKET_ID = re.compile(r"^TKT-[A-Z0-9]+$")
OPERATION_ID = re.compile(r"^OP-[A-Za-z0-9]+$")
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9:_-]{6,128}$")


def _now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------------ the envelope --


@dataclass
class Result:
    """What a handler returns. The adapter turns it into an HTTP response, unchanged."""

    status: int
    body: Any
    headers: dict[str, str] = field(default_factory=dict)


def correlation_id(headers: Mapping[str, str]) -> str:
    """The trace id from `traceparent` if the caller sent one, otherwise a fresh id."""
    lowered = {k.lower(): v for k, v in headers.items()}
    traceparent = lowered.get("traceparent", "")
    parts = traceparent.split("-")
    if len(parts) == 4 and len(parts[1]) == 32:
        return parts[1]
    return uuid.uuid4().hex


def error(status: int, code: ErrorCode, message: str, cid: str) -> Result:
    return Result(status, {"code": code, "message": message, "correlation_id": cid})


def dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


CONTROL = "control"
CHAOS_DOC_ID = "chaos-ops-api"
CHAOS_CACHE_S = 3.0


class ChaosSwitch:
    """The ops API's application-level outage switch (CLAUDE.md 9.3).

    `scripts/chaos/ops_api_down.sh` writes one document to the `control` container; while it is
    enabled and unexpired, every route except /health answers a typed 503. The gateway's retry
    policy, the client's circuit breaker and the agent's degraded mode are then exercised for real -
    with no control-plane write, no stopped Function App and no cold start on the way back.
    The document always carries an expiry, so a forgotten switch turns itself off.

    Two modes (Phase 13 added the second): `outage` (the default) answers 503; `latency` answers
    normally after `latency_ms` - scripts/inject_latency.py, which makes the p95 alert and the
    latency gate fire without breaking anything.

    Read at most every few seconds per worker. A failure to READ the switch is never an outage.
    """

    def __init__(self, store: Callable[[], Any], *, cache_s: float = CHAOS_CACHE_S) -> None:
        self._store, self._cache_s = store, cache_s
        self._state: tuple[float, bool, float] | None = None

    async def active(self) -> bool:
        """True while the switch is an outage."""
        return (await self._read())[0]

    async def delay_s(self) -> float:
        """Seconds to wait before answering while the switch is in latency mode, else 0."""
        return (await self._read())[1]

    async def _read(self) -> tuple[bool, float]:
        now = time.monotonic()
        if self._state is not None and now - self._state[0] < self._cache_s:
            return self._state[1], self._state[2]
        on, delay = False, 0.0
        try:
            rows = await asyncio.wait_for(self._store().query(
                CONTROL, "SELECT * FROM c WHERE c.id = @id",
                [{"name": "@id", "value": CHAOS_DOC_ID}], partition_key=CHAOS_DOC_ID), 2.0)
            doc = rows[0] if rows else {}
            expires = doc.get("expires_at")
            live = bool(doc.get("enabled")) and (
                not expires or datetime.fromisoformat(str(expires)) > datetime.now(UTC))
            if live and doc.get("mode") == "latency":
                delay = min(float(doc.get("latency_ms") or 0) / 1000, 30.0)
            else:
                on = live
        except Exception:
            log.warning("chaos switch unreadable; treating as off", exc_info=True)
        self._state = (now, on, delay)
        return on, delay


async def guarded(cid: str, work: Callable[[], Awaitable[Result]], *,
                  chaos: ChaosSwitch | None = None) -> Result:
    """Run a handler under a timeout, turning every failure into a typed envelope."""
    if chaos is not None and await chaos.active():
        result = error(503, "unavailable", "the operations API is temporarily unavailable", cid)
        result.headers["x-atlas-chaos"] = "on"
        result.headers.setdefault("x-atlas-correlation-id", cid)
        return result
    if chaos is not None and (delay := await chaos.delay_s()):
        await asyncio.sleep(delay)  # latency mode: slow, never wrong
    try:
        async with asyncio.timeout(STORE_TIMEOUT_S * 3):
            result = await work()
    except TimeoutError:
        log.warning("request %s timed out", cid)
        result = error(503, "unavailable", "a backing store did not answer in time", cid)
    except RepositoryUnavailableError as exc:
        log.warning("request %s: store unavailable: %s", cid, exc)
        result = error(503, "unavailable", "a backing store did not answer in time", cid)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()))
        result = error(400, "invalid_request",
                       f"{where or 'body'}: {first.get('msg', 'invalid')}"[:200], cid)
    except Exception:
        # The detail belongs in the log and the trace, never in the response.
        log.exception("request %s failed", cid)
        result = error(500, "internal", "the operations API could not complete the request", cid)
    result.headers.setdefault("x-atlas-correlation-id", cid)
    return result


# ------------------------------------------------------------------------ reads --


class Deps(Protocol):
    """What the handlers need. The adapter provides a live one; tests provide a fake."""

    store: CosmosStore
    ops_now: datetime

    async def enqueue(self, *, message_id: str, session_id: str, body: dict[str, Any],
                      correlation: str) -> None: ...


async def list_services(deps: Deps, cid: str) -> Result:
    services = await ServiceRepository(deps.store).list_all()
    return Result(200, {"services": [dump(s) for s in services]})


async def get_service(deps: Deps, service_id: str, cid: str) -> Result:
    if not SERVICE_ID.match(service_id):
        return error(400, "invalid_request", "service_id must look like svc-name", cid)
    service = await ServiceRepository(deps.store).get(service_id)
    if service is None:
        return error(404, "not_found", f"no service {service_id}", cid)
    return Result(200, dump(service))


async def get_service_health(deps: Deps, service_id: str, window_minutes: int,
                             cid: str) -> Result:
    if not SERVICE_ID.match(service_id):
        return error(400, "invalid_request", "service_id must look like svc-name", cid)
    if not 5 <= window_minutes <= 1440:
        return error(400, "invalid_request", "window_minutes must be between 5 and 1440", cid)
    service = await ServiceRepository(deps.store).get(service_id)
    if service is None:
        return error(404, "not_found", f"no service {service_id}", cid)
    windows = await TelemetryRepository(deps.store).windows(
        service_id, as_of=deps.ops_now, window_minutes=window_minutes)
    anomalies = [w for w in windows if w.anomaly]
    body: dict[str, Any] = {
        "service_id": service_id,
        "window_minutes": window_minutes,
        "measured": bool(windows),
        "slo_p95_ms": service.slo.latency_p95_ms,
        "peak_p95_ms": max((w.p95_ms for w in windows), default=None),
        "peak_error_rate_pct": max((w.error_rate_pct for w in windows), default=None),
        "peak_slo_burn_rate": max((w.slo_burn_rate for w in windows), default=None),
        "anomaly_onset": anomalies[0].window_start.isoformat() if anomalies else None,
        "windows": [dump(w) for w in windows],
    }
    return Result(200, body)


async def get_blast_radius(deps: Deps, service_id: str, depth: int, cid: str) -> Result:
    if not SERVICE_ID.match(service_id):
        return error(400, "invalid_request", "service_id must look like svc-name", cid)
    if not 1 <= depth <= 6:
        return error(400, "invalid_request", "depth must be between 1 and 6", cid)
    services = await ServiceRepository(deps.store).list_all()
    if not any(s.service_id == service_id for s in services):
        return error(404, "not_found", f"no service {service_id}", cid)
    return Result(200, dump(blast_radius(service_id, services, depth=depth)))


async def get_incident(deps: Deps, incident_id: str, cid: str) -> Result:
    if not INCIDENT_ID.match(incident_id):
        return error(400, "invalid_request", "incident_id must look like INC-2026-0297", cid)
    incident = await IncidentRepository(deps.store).get(incident_id)
    if incident is None:
        return error(404, "not_found", f"no incident {incident_id}", cid)
    return Result(200, dump(incident))


async def list_changes(deps: Deps, service_id: str | None, since_minutes: int,
                       cid: str) -> Result:
    if not service_id or not SERVICE_ID.match(service_id):
        return error(400, "invalid_request", "service_id is required and must look like svc-name",
                     cid)
    if not 1 <= since_minutes <= 10080:
        return error(400, "invalid_request", "since_minutes must be between 1 and 10080", cid)
    changes = await ChangeRepository(deps.store).recent(
        service_id, as_of=deps.ops_now, since_minutes=since_minutes)
    return Result(200, {"service_id": service_id, "since_minutes": since_minutes,
                        "changes": [dump(c) for c in changes]})


# ------------------------------------------------------------------ idempotency --


@dataclass
class IdempotencyClaim:
    """Outcome of claiming a key. `replay` carries the stored result when the key was used."""

    claimed: bool
    replay: Result | None = None
    conflict: str = ""


def request_fingerprint(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


async def claim_key(store: CosmosStore, scope: str, key: str,
                    fingerprint: str) -> IdempotencyClaim:
    """Claim an idempotency key, or find what already happened under it.

    The claim is a `create_item`, which Cosmos refuses if the id exists. That is the whole
    concurrency story: of two simultaneous first requests, exactly one create succeeds.
    """
    from azure.cosmos.exceptions import CosmosResourceExistsError

    doc_id = f"{scope}:{key}"
    try:
        await asyncio.wait_for(store.container(IDEMPOTENCY).create_item({
            "id": doc_id, "key": doc_id, "scope": scope, "fingerprint": fingerprint,
            "state": "in_progress", "created_at": _now().isoformat(),
        }), STORE_TIMEOUT_S)
        return IdempotencyClaim(claimed=True)
    except CosmosResourceExistsError:
        pass
    existing = await asyncio.wait_for(
        store.container(IDEMPOTENCY).read_item(item=doc_id, partition_key=doc_id),
        STORE_TIMEOUT_S)
    if existing.get("fingerprint") != fingerprint:
        return IdempotencyClaim(
            claimed=False,
            conflict="this Idempotency-Key was already used with a different request")
    if existing.get("state") != "done":
        return IdempotencyClaim(
            claimed=False, conflict="a request with this Idempotency-Key is still in progress")
    stored = existing.get("result") or {}
    return IdempotencyClaim(claimed=False, replay=Result(
        200, stored.get("body"), {"Idempotency-Replayed": "true"}))


async def settle_key(store: CosmosStore, scope: str, key: str, fingerprint: str,
                     result: Result) -> None:
    doc_id = f"{scope}:{key}"
    await asyncio.wait_for(store.upsert(IDEMPOTENCY, {
        "id": doc_id, "key": doc_id, "scope": scope, "fingerprint": fingerprint,
        "state": "done", "result": {"status": result.status, "body": result.body},
        "settled_at": _now().isoformat(),
    }), STORE_TIMEOUT_S)


async def release_key(store: CosmosStore, scope: str, key: str) -> None:
    """A failed first attempt must not leave the key locked for the next seven days."""
    doc_id = f"{scope}:{key}"
    try:
        await store.delete(IDEMPOTENCY, doc_id, doc_id)
    except Exception:
        log.exception("could not release idempotency key %s", doc_id)


# ------------------------------------------------------------------------ tickets --


class TicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=r"^CASE-[A-Z0-9]+$")
    severity: Severity
    summary: str = Field(min_length=8, max_length=500)
    service_ids: list[str] = Field(default_factory=list, max_length=25)


async def create_ticket(deps: Deps, body: Any, idempotency_key: str | None, cid: str) -> Result:
    if not idempotency_key or not IDEMPOTENCY_KEY.match(idempotency_key):
        return error(400, "invalid_request", "an Idempotency-Key header is required", cid)
    request = TicketRequest.model_validate(body)
    for service_id in request.service_ids:
        if not SERVICE_ID.match(service_id):
            return error(400, "invalid_request", f"service_ids: {service_id} is not a service id",
                         cid)

    fingerprint = request_fingerprint(request.model_dump(mode="json"))
    claim = await claim_key(deps.store, "ticket", idempotency_key, fingerprint)
    if claim.replay is not None:
        return claim.replay
    if not claim.claimed:
        return error(409, "conflict", claim.conflict, cid)

    try:
        now = _now().isoformat()
        ticket = {
            "id": "", "ticket_id": f"TKT-{secrets.token_hex(5).upper()}",
            "case_id": request.case_id, "severity": request.severity,
            "summary": request.summary, "service_ids": request.service_ids,
            "status": "open", "created_at": now, "updated_at": now,
            "idempotency_key": idempotency_key, "correlation_id": cid,
        }
        ticket["id"] = ticket["ticket_id"]
        await asyncio.wait_for(deps.store.upsert(TICKETS, ticket), STORE_TIMEOUT_S)
        public = {k: v for k, v in ticket.items() if k not in ("id", "idempotency_key",
                                                              "correlation_id")}
        result = Result(201, public, {"Location": f"/api/tickets/{ticket['ticket_id']}"})
        await settle_key(deps.store, "ticket", idempotency_key, fingerprint, result)
        return result
    except BaseException:
        await release_key(deps.store, "ticket", idempotency_key)
        raise


async def get_ticket(deps: Deps, ticket_id: str, cid: str) -> Result:
    if not TICKET_ID.match(ticket_id):
        return error(400, "invalid_request", "ticket_id must look like TKT-...", cid)
    rows = await deps.store.query(TICKETS, "SELECT * FROM c WHERE c.ticket_id = @id",
                                  [{"name": "@id", "value": ticket_id}], partition_key=ticket_id)
    if not rows:
        return error(404, "not_found", f"no ticket {ticket_id}", cid)
    return Result(200, {k: v for k, v in rows[0].items()
                        if not k.startswith("_") and k not in ("id", "idempotency_key",
                                                               "correlation_id")})


# -------------------------------------------------------------------- remediation --


class RemediationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=r"^CASE-[A-Z0-9]+$")
    plan_id: str = Field(pattern=r"^[a-f0-9]{16}$")
    approval_token: str = Field(min_length=20, max_length=2048)


async def _plan_session(store: CosmosStore, case_id: str, plan_id: str) -> tuple[str, int]:
    """The Service Bus session for a plan, and its step count, read from the stored case.

    Session = the first target service, so commands against one service run in order. If the case
    cannot be read the case id is a safe fallback: it still serialises this plan's steps.
    """
    try:
        rows = await store.query(CASES, "SELECT * FROM c WHERE c.id = @id",
                                 [{"name": "@id", "value": case_id}], partition_key=case_id)
    except Exception:
        return case_id, 0
    plan = ((rows[0].get("state") or {}).get("plan") or {}) if rows else {}
    if plan.get("plan_id") != plan_id:
        return case_id, 0
    steps = plan.get("steps") or []
    first = steps[0].get("target_service") if steps else None
    return (first or case_id), len(steps)


async def submit_remediation(deps: Deps, body: Any, idempotency_key: str | None,
                             cid: str, caller: str = "") -> Result:
    if not idempotency_key or not IDEMPOTENCY_KEY.match(idempotency_key):
        return error(400, "invalid_request", "an Idempotency-Key header is required", cid)
    command = RemediationCommand.model_validate(body)

    # The fingerprint deliberately excludes the token: a replay after the token was spent must
    # still find the original operation, and the token is single use.
    fingerprint = request_fingerprint({"case_id": command.case_id, "plan_id": command.plan_id})
    claim = await claim_key(deps.store, "remediation", idempotency_key, fingerprint)
    if claim.replay is not None:
        return claim.replay
    if not claim.claimed:
        return error(409, "conflict", claim.conflict, cid)

    try:
        checked = await approval_tokens.check(
            deps.store, command.approval_token, case_id=command.case_id,
            plan_id=command.plan_id, now=_now())
        if not checked.ok or checked.record is None:
            await approval_tokens.audit_refusal(
                deps.store, case_id=command.case_id, plan_id=command.plan_id,
                code=str(checked.code), reason=checked.reason, correlation_id=cid,
                caller=caller)
            await release_key(deps.store, "remediation", idempotency_key)
            return error(403, "approval_invalid",
                         f"the approval token is not valid for this plan: {checked.reason}", cid)

        operation_id = f"OP-{secrets.token_hex(6).upper()}"
        if not await approval_tokens.consume(deps.store, checked.record,
                                             operation_id=operation_id):
            await approval_tokens.audit_refusal(
                deps.store, case_id=command.case_id, plan_id=command.plan_id, code="used",
                reason="lost a race to spend the token", correlation_id=cid, caller=caller)
            await release_key(deps.store, "remediation", idempotency_key)
            return error(403, "approval_invalid", "the approval token has already been spent", cid)

        session_id, steps_total = await _plan_session(deps.store, command.case_id,
                                                      command.plan_id)
        now = _now().isoformat()
        operation = {
            "id": f"operation:{operation_id}", "kind": "operation", "case_id": command.case_id,
            "operation_id": operation_id, "plan_id": command.plan_id, "status": "queued",
            "steps_completed": 0, "steps_total": steps_total,
            "detail": "queued for the remediation worker",
            "approved_by": checked.record.get("approved_by"),
            "approver_role": checked.record.get("approver_role"),
            "submitted_at": now, "updated_at": now, "correlation_id": cid,
        }
        await asyncio.wait_for(deps.store.upsert(APPROVALS, operation), STORE_TIMEOUT_S)
        # MessageId = plan_id: Service Bus duplicate detection makes a resubmitted plan a no-op
        # inside the window, on top of the idempotency key above.
        try:
            await deps.enqueue(message_id=command.plan_id, session_id=session_id,
                               correlation=cid,
                               body={"operation_id": operation_id, "case_id": command.case_id,
                                     "plan_id": command.plan_id, "submitted_at": now})
        except Exception as exc:
            # Nothing reached the queue, so nothing used the token: give it back, record the
            # attempt truthfully, and say what happened. Otherwise the gateway's own retry met
            # "the token has already been spent" and that is all the operator saw
            # (2026-10-11, API-NOTES 23.2). A typed 503: the gateway retries it, then relays it.
            log.warning("enqueue refused for %s: %s", operation_id, exc)
            operation.update(status="enqueue_failed", updated_at=_now().isoformat(),
                             detail=f"never reached the queue: {type(exc).__name__}")
            await asyncio.wait_for(deps.store.upsert(APPROVALS, operation), STORE_TIMEOUT_S)
            await approval_tokens.release(deps.store, checked.record, operation_id=operation_id)
            await release_key(deps.store, "remediation", idempotency_key)
            return error(503, "queue_unavailable",
                         "the remediation queue refused the command; nothing was queued and the "
                         "approval stands - retry once the queue accepts it", cid)
        public = _public_operation(operation)
        result = Result(202, public,
                        {"Location": f"/api/remediation/operations/{operation_id}"})
        await settle_key(deps.store, "remediation", idempotency_key, fingerprint, result)
        return result
    except BaseException:
        await release_key(deps.store, "remediation", idempotency_key)
        raise


def _public_operation(doc: Mapping[str, Any]) -> dict[str, Any]:
    keep = ("operation_id", "case_id", "plan_id", "status", "steps_completed", "steps_total",
            "detail", "submitted_at", "updated_at")
    return {k: doc.get(k) for k in keep}


async def get_operation(deps: Deps, operation_id: str, cid: str) -> Result:
    if not OPERATION_ID.match(operation_id):
        return error(400, "invalid_request", "operation_id must look like OP-...", cid)
    rows = await deps.store.query(
        APPROVALS, "SELECT * FROM c WHERE c.kind = 'operation' AND c.operation_id = @id",
        [{"name": "@id", "value": operation_id}])
    if not rows:
        return error(404, "not_found", f"no operation {operation_id}", cid)
    return Result(200, _public_operation(rows[0]))


__all__ = [
    "Deps",
    "RemediationCommand",
    "Result",
    "TicketRequest",
    "claim_key",
    "correlation_id",
    "create_ticket",
    "error",
    "get_blast_radius",
    "get_incident",
    "get_operation",
    "get_service",
    "get_service_health",
    "get_ticket",
    "guarded",
    "list_changes",
    "list_services",
    "request_fingerprint",
    "submit_remediation",
]
