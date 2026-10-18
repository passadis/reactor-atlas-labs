"""Phase 08: the operations API's behaviour, and its contract.

No Azure. A fake Cosmos implements the three behaviours the API's correctness actually rests on -
create-if-absent (the idempotency claim), ETag-conditional replace (single-use tokens), and
partitioned reads - so the concurrency and replay rules are tested, not assumed.

The contract test parses `apim/openapi/atlas-ops-api.yaml` and `functions/ops_api/function_app.py`
and asserts they describe exactly the same routes. The spec is hand-written; this is what stops the
hand drifting from the code.
"""

from __future__ import annotations

import asyncio
import copy
import re
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from azure.core.exceptions import ResourceModifiedError
from azure.cosmos.exceptions import CosmosResourceExistsError, CosmosResourceNotFoundError

from atlas_common import approval_tokens

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "functions" / "ops_api"))

from opsapi import core  # noqa: E402

SPEC = ROOT / "apim" / "openapi" / "atlas-ops-api.yaml"
APP = ROOT / "functions" / "ops_api" / "function_app.py"
NOW = datetime(2026, 9, 1, 14, 20, tzinfo=UTC)


# --------------------------------------------------------------------- the fake store --


class FakeContainer:
    def __init__(self, store: FakeStore, name: str) -> None:
        self.store, self.name = store, name

    @property
    def docs(self) -> dict[str, dict[str, Any]]:
        return self.store.data.setdefault(self.name, {})

    async def create_item(self, body: dict[str, Any]) -> dict[str, Any]:
        if body["id"] in self.docs:
            raise CosmosResourceExistsError(message="exists")
        self.docs[body["id"]] = {**copy.deepcopy(body), "_etag": uuid.uuid4().hex}
        return self.docs[body["id"]]

    async def read_item(self, item: str, partition_key: str) -> dict[str, Any]:
        if item not in self.docs:
            raise CosmosResourceNotFoundError(message="missing")
        return copy.deepcopy(self.docs[item])

    async def replace_item(self, item: str, body: dict[str, Any], etag: str | None = None,
                           match_condition: Any = None) -> dict[str, Any]:
        current = self.docs.get(item)
        if current is None or (etag is not None and current["_etag"] != etag):
            raise ResourceModifiedError(message="etag mismatch")
        self.docs[item] = {**copy.deepcopy(body), "_etag": uuid.uuid4().hex}
        return self.docs[item]


class FakeStore:
    """Just enough of CosmosStore. Queries are matched by the `@id`-style parameters they use."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, dict[str, Any]]] = {}

    def container(self, name: str) -> FakeContainer:
        return FakeContainer(self, name)

    async def upsert(self, container: str, document: dict[str, Any], **_: Any) -> None:
        self.data.setdefault(container, {})[document["id"]] = {
            **copy.deepcopy(document), "_etag": uuid.uuid4().hex}

    async def delete(self, container: str, item_id: str, partition_key: str, **_: Any) -> bool:
        return self.data.get(container, {}).pop(item_id, None) is not None

    async def query(self, container: str, sql: str, parameters: list[dict[str, Any]] | None = None,
                    **_: Any) -> list[dict[str, Any]]:
        docs = list(self.data.get(container, {}).values())
        params = {p["name"]: p["value"] for p in (parameters or [])}
        out = []
        for doc in docs:
            if "c.id = @id" in sql and doc.get("id") != params.get("@id"):
                continue
            if "c.ticket_id = @id" in sql and doc.get("ticket_id") != params.get("@id"):
                continue
            if "c.operation_id = @id" in sql and doc.get("operation_id") != params.get("@id"):
                continue
            if "c.kind = 'operation'" in sql and doc.get("kind") != "operation":
                continue
            out.append(copy.deepcopy(doc))
        return out


class FakeDeps:
    def __init__(self) -> None:
        self.store: Any = FakeStore()
        self.ops_now = NOW
        self.sent: list[dict[str, Any]] = []

    async def enqueue(self, *, message_id: str, session_id: str, body: dict[str, Any],
                      correlation: str) -> None:
        self.sent.append({"message_id": message_id, "session_id": session_id, **body})


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def call(deps: FakeDeps, fn: Any, *args: Any) -> core.Result:
    cid = "c0ffee"
    return run(core.guarded(cid, lambda: fn(deps, *args, cid)))


TICKET = {"case_id": "CASE-ABC123", "severity": "Sev2",
          "summary": "Checkout is timing out after the payments deploy.",
          "service_ids": ["svc-checkout-api", "svc-payments"]}


# ------------------------------------------------------------------- the envelope --


def test_correlation_comes_from_traceparent() -> None:
    tid = "4bf92f3577b34da6a3ce929d0e0e4736"
    assert core.correlation_id({"traceparent": f"00-{tid}-00f067aa0ba902b7-01"}) == tid
    assert len(core.correlation_id({})) == 32


def test_every_response_carries_the_correlation_header() -> None:
    result = call(FakeDeps(), core.get_ticket, "TKT-NOPE")
    assert result.headers["x-atlas-correlation-id"] == "c0ffee"


def test_an_internal_failure_is_an_envelope_not_a_stack_trace() -> None:
    async def explode() -> core.Result:
        raise RuntimeError("secret connection detail that must never leak")

    result = run(core.guarded("abc", explode))
    assert result.status == 500
    assert result.body["code"] == "internal"
    assert "secret" not in str(result.body)
    assert result.body["correlation_id"] == "abc"


def test_a_slow_backend_is_a_typed_503() -> None:
    async def hang() -> core.Result:
        await asyncio.sleep(60)
        return core.Result(200, {})

    original = core.STORE_TIMEOUT_S
    core.STORE_TIMEOUT_S = 0.01
    try:
        result = run(core.guarded("abc", hang))
    finally:
        core.STORE_TIMEOUT_S = original
    assert result.status == 503 and result.body["code"] == "unavailable"


@pytest.mark.parametrize(("fn", "arg"), [
    (core.get_service, "payments"),
    (core.get_incident, "2026-0297"),
    (core.get_ticket, "ticket-1"),
    (core.get_operation, "op-1"),
])
def test_malformed_ids_are_rejected_before_any_lookup(fn: Any, arg: str) -> None:
    result = call(FakeDeps(), fn, arg)
    assert result.status == 400 and result.body["code"] == "invalid_request"


def test_list_changes_requires_a_service() -> None:
    result = call(FakeDeps(), core.list_changes, None, 240)
    assert result.status == 400


# ------------------------------------------------------------------- idempotency --


def test_a_ticket_is_created_once_and_replayed_after() -> None:
    deps = FakeDeps()
    first = run(core.guarded("a", lambda: core.create_ticket(deps, TICKET, "CASE-ABC123", "a")))
    again = run(core.guarded("b", lambda: core.create_ticket(deps, TICKET, "CASE-ABC123", "b")))

    assert first.status == 201
    assert again.status == 200
    assert again.headers.get("Idempotency-Replayed") == "true"
    assert again.body["ticket_id"] == first.body["ticket_id"]
    assert len(deps.store.data["tickets"]) == 1, "a replay opened a second ticket"


def test_reusing_a_key_with_a_different_body_is_a_conflict() -> None:
    deps = FakeDeps()
    run(core.guarded("a", lambda: core.create_ticket(deps, TICKET, "CASE-ABC123", "a")))
    other = {**TICKET, "summary": "A completely different ticket body."}
    result = run(core.guarded("b", lambda: core.create_ticket(deps, other, "CASE-ABC123", "b")))
    assert result.status == 409 and result.body["code"] == "conflict"


def test_concurrent_first_requests_create_one_ticket() -> None:
    """The claim is create-if-absent, so exactly one of the racers does the work."""
    deps = FakeDeps()

    async def race() -> list[core.Result]:
        return await asyncio.gather(*[
            core.guarded(f"r{i}", lambda i=i: core.create_ticket(deps, TICKET, "CASE-RACE01",
                                                               f"r{i}"))
            for i in range(8)
        ])

    results = run(race())
    assert len(deps.store.data["tickets"]) == 1
    assert sum(1 for r in results if r.status == 201) == 1


def test_a_ticket_needs_an_idempotency_key() -> None:
    result = run(core.guarded("a", lambda: core.create_ticket(FakeDeps(), TICKET, None, "a")))
    assert result.status == 400


def test_a_ticket_body_is_validated_at_the_boundary() -> None:
    bad = {**TICKET, "severity": "Sev9"}
    result = run(core.guarded("a", lambda: core.create_ticket(FakeDeps(), bad, "CASE-X12345",
                                                              "a")))
    assert result.status == 400 and "severity" in result.body["message"]


def test_unknown_fields_are_refused() -> None:
    bad = {**TICKET, "priority": "urgent"}
    result = run(core.guarded("a", lambda: core.create_ticket(FakeDeps(), bad, "CASE-X12345",
                                                              "a")))
    assert result.status == 400


# ------------------------------------------------------------- approval tokens --


def mint(deps: FakeDeps, *, plan_id: str = "0123456789abcdef", case_id: str = "CASE-ABC123",
         ttl: timedelta = timedelta(minutes=30), now: datetime | None = None) -> str:
    minted = run(approval_tokens.mint(deps.store, case_id=case_id, plan_id=plan_id,
                                      approved_by="dana@contoso.com", approver_role="director",
                                      ttl=ttl, now=now))
    return minted.token


def submit(deps: FakeDeps, token: str, *, key: str = "0123456789abcdef",
           plan_id: str = "0123456789abcdef", case_id: str = "CASE-ABC123") -> core.Result:
    body = {"case_id": case_id, "plan_id": plan_id, "approval_token": token}
    return run(core.guarded("s", lambda: core.submit_remediation(deps, body, key, "s")))


def test_a_valid_token_queues_the_plan_once() -> None:
    deps = FakeDeps()
    result = submit(deps, mint(deps))
    assert result.status == 202
    assert result.body["status"] == "queued"
    assert result.headers["Location"].startswith("/api/remediation/operations/OP-")
    assert len(deps.sent) == 1
    assert deps.sent[0]["message_id"] == "0123456789abcdef", "MessageId must be the plan id"


def test_a_failed_enqueue_fails_closed_and_says_so() -> None:
    """Episode 3 Beat 4: the ops API loses its Service Bus Sender role. Nothing may run, the
    operation must not claim to be queued, and a retry with a fresh token must go through."""
    class NoSendDeps(FakeDeps):
        async def enqueue(self, **_: Any) -> None:
            raise PermissionError("Unauthorized: Send claim required")

    deps = NoSendDeps()
    token = mint(deps)
    failed = submit(deps, token)
    assert failed.status == 503 and failed.body["code"] == "queue_unavailable"
    assert not deps.sent
    ops = [d for d in deps.store.data.get("approvals", {}).values() if d.get("kind") == "operation"]
    assert [o["status"] for o in ops] == ["enqueue_failed"]

    # The gateway retries a 5xx with the SAME request: it must meet the same honest 503, not
    # "the token has already been spent" (what the operator saw on 2026-10-11).
    again = submit(deps, token)
    assert again.status == 503 and again.body["code"] == "queue_unavailable"

    deps.__class__ = FakeDeps  # the role is back: the same token, never used, now goes through
    retried = submit(deps, token)
    assert retried.status == 202 and len(deps.sent) == 1


def test_a_forged_token_is_refused_and_audited() -> None:
    deps = FakeDeps()
    mint(deps)
    forged = approval_tokens.TOKEN_PREFIX + "x" * 43
    result = submit(deps, forged)
    assert result.status == 403 and result.body["code"] == "approval_invalid"
    assert not deps.sent, "a forged token queued a remediation"
    refusals = [d for d in deps.store.data["approvals"].values()
                if d.get("kind") == "approval-refusal"]
    assert refusals and refusals[0]["code"] == "unknown"


def test_a_token_is_single_use() -> None:
    deps = FakeDeps()
    token = mint(deps)
    first = submit(deps, token, key="plan-key-one")
    second = submit(deps, token, key="plan-key-two")
    assert first.status == 202
    assert second.status == 403 and "spent" in second.body["message"]
    assert len(deps.sent) == 1


def test_a_replay_of_the_same_submission_does_not_need_the_token_again() -> None:
    deps = FakeDeps()
    token = mint(deps)
    first = submit(deps, token)
    again = submit(deps, token)
    assert first.status == 202
    assert again.status == 200 and again.headers.get("Idempotency-Replayed") == "true"
    assert again.body["operation_id"] == first.body["operation_id"]
    assert len(deps.sent) == 1


def test_an_expired_token_is_refused() -> None:
    deps = FakeDeps()
    token = mint(deps, ttl=timedelta(minutes=30), now=datetime.now(UTC) - timedelta(hours=2))
    result = submit(deps, token)
    assert result.status == 403 and "expired" in result.body["message"]


def test_a_token_for_another_plan_is_refused() -> None:
    deps = FakeDeps()
    token = mint(deps, plan_id="aaaaaaaaaaaaaaaa")
    result = submit(deps, token, plan_id="bbbbbbbbbbbbbbbb", key="bbbbbbbbbbbbbbbb")
    assert result.status == 403 and "different plan" in result.body["message"]


def test_a_token_for_another_case_is_refused() -> None:
    deps = FakeDeps()
    token = mint(deps, case_id="CASE-OTHER1")
    result = submit(deps, token, case_id="CASE-ABC123")
    assert result.status == 403


def test_a_refused_submission_does_not_lock_the_key() -> None:
    """A 403 must not burn the Idempotency-Key: the retry with a real token has to work."""
    deps = FakeDeps()
    refused = submit(deps, approval_tokens.TOKEN_PREFIX + "y" * 43)
    assert refused.status == 403
    accepted = submit(deps, mint(deps))
    assert accepted.status == 202


def test_only_the_hash_is_stored() -> None:
    deps = FakeDeps()
    token = mint(deps)
    stored = str(deps.store.data["approvals"])
    assert token not in stored
    assert approval_tokens.token_hash(token) in stored


def test_a_token_lost_race_is_refused_not_double_spent() -> None:
    deps = FakeDeps()
    token = mint(deps)
    checked = run(approval_tokens.check(deps.store, token, case_id="CASE-ABC123",
                                        plan_id="0123456789abcdef"))
    assert checked.record is not None
    stale = dict(checked.record)
    assert run(approval_tokens.consume(deps.store, checked.record, operation_id="OP-1"))
    assert not run(approval_tokens.consume(deps.store, stale, operation_id="OP-2"))


# ------------------------------------------------------------------- the contract --


def spec_routes() -> set[tuple[str, str]]:
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    return {(method.upper(), path) for path, item in spec["paths"].items()
            for method in item if method in ("get", "post", "put", "patch", "delete")}


def app_routes() -> set[tuple[str, str]]:
    source = APP.read_text(encoding="utf-8")
    found: set[tuple[str, str]] = set()
    for route, methods in re.findall(
            r'@app\.route\(route="([^"]+)",\s*methods=\[([^\]]+)\]\)', source):
        for method in re.findall(r'"([A-Z]+)"', methods):
            found.add((method, "/" + route))
    return found


def test_the_deployed_routes_match_the_hand_written_spec_exactly() -> None:
    assert app_routes() == spec_routes(), (
        f"only in the app: {sorted(app_routes() - spec_routes())}; "
        f"only in the spec: {sorted(spec_routes() - app_routes())}")


def test_every_operation_says_when_not_to_use_it() -> None:
    """The descriptions are prompt engineering. Negative guidance is part of the contract."""
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if op.get("operationId") == "getHealth":
                continue
            text = op.get("description", "")
            assert "NOT" in text or "never" in text.lower(), (
                f"{method.upper()} {path} gives no guidance on when not to use it")


def test_every_operation_has_an_llm_facing_id_and_summary() -> None:
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    ids = []
    for path, item in spec["paths"].items():
        for method, op in item.items():
            assert op.get("operationId"), f"{method} {path}"
            assert op.get("summary"), f"{method} {path}"
            ids.append(op["operationId"])
    assert len(ids) == len(set(ids))


def test_the_spec_is_valid_openapi() -> None:
    from openapi_spec_validator import validate
    from openapi_spec_validator.readers import read_from_filename

    spec, _ = read_from_filename(str(SPEC))
    validate(spec)


# ------------------------------------------------- chaos switch: outage and latency (Phase 13) --


class _ChaosStore:
    def __init__(self, doc: dict[str, Any] | None) -> None:
        self.doc = doc

    async def query(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        return [self.doc] if self.doc else []


def _chaos(doc: dict[str, Any] | None) -> Any:
    from opsapi.core import ChaosSwitch

    store = _ChaosStore(doc)
    return ChaosSwitch(lambda: store, cache_s=0)


async def test_an_outage_document_turns_the_switch_on() -> None:
    switch = _chaos({"enabled": True})
    assert await switch.active() is True
    assert await switch.delay_s() == 0


async def test_latency_mode_slows_but_never_fails() -> None:
    switch = _chaos({"enabled": True, "mode": "latency", "latency_ms": 4000})
    assert await switch.active() is False
    assert await switch.delay_s() == 4.0


async def test_latency_is_capped_and_expiry_still_applies() -> None:
    assert await _chaos({"enabled": True, "mode": "latency",
                         "latency_ms": 999_000}).delay_s() == 30.0
    expired = _chaos({"enabled": True, "mode": "latency", "latency_ms": 4000,
                      "expires_at": "2000-01-01T00:00:00+00:00"})
    assert await expired.delay_s() == 0 and await expired.active() is False
