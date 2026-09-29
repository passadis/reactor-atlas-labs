"""Approval tokens: proof that a human approved THIS plan, spendable exactly once.

The orchestrator mints one after a human approves a case. The operations API spends it when the
remediation is submitted. Nothing else can create one, and a model that invents one gets a typed
403 and an audit record - which is an Episode 4 evaluation case.

**Unsigned, on purpose.** The lab pack describes a token whose *signature* is checked. A signature
needs a signing key, and CLAUDE.md section 1 permits exactly one secret in the whole system (the
APIM subscription key). So the token is instead an opaque 256-bit random value, and Cosmos holds
only its SHA-256 alongside the case, plan, approver and expiry. There is nothing to forge: a value
is valid only if its hash is on record, and 2^256 guesses is not an attack surface. Recorded as a
deviation in docs/API-NOTES.md section 15.

**Single use** is an ETag-conditional replace. Two concurrent submissions with the same token both
read `used: false`, but only one replace can match the ETag; the other gets 412 and is refused.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from .repositories import CosmosStore

log = logging.getLogger("atlas.approvals")

APPROVALS = "approvals"
TOKEN_TTL = timedelta(minutes=30)
# A recognisable prefix, not a secret: it lets a scanner spot a leaked token in a log.
TOKEN_PREFIX = "atlas_apv_"  # noqa: S105

RefusalCode = Literal[
    "missing", "unknown", "expired", "used", "case", "plan", "role", "unavailable",
]


def _now() -> datetime:
    return datetime.now(UTC)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MintedToken:
    token: str
    token_id: str
    expires_at: datetime


@dataclass(frozen=True)
class TokenCheck:
    ok: bool
    code: RefusalCode | Literal["ok"]
    reason: str
    record: dict[str, Any] | None = None


async def mint(
    store: CosmosStore, *, case_id: str, plan_id: str, approved_by: str, approver_role: str,
    ttl: timedelta = TOKEN_TTL, now: datetime | None = None,
) -> MintedToken:
    """Mint a single-use token for one approved plan. Only the orchestrator calls this."""
    moment = now or _now()
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    digest = token_hash(token)
    expires = moment + ttl
    await store.upsert(APPROVALS, {
        "id": f"token:{digest}", "case_id": case_id, "kind": "approval-token",
        "token_hash": digest, "plan_id": plan_id,
        "approved_by": approved_by, "approver_role": approver_role,
        "issued_at": moment.isoformat(), "expires_at": expires.isoformat(),
        "used": False, "used_at": None, "used_by_operation": None,
    })
    log.info("approval token minted for %s plan %s by %s (%s)", case_id, plan_id,
             approved_by, approver_role)
    return MintedToken(token=token, token_id=digest[:12], expires_at=expires)


async def check(
    store: CosmosStore, token: str | None, *, case_id: str, plan_id: str,
    now: datetime | None = None,
) -> TokenCheck:
    """Validate a token against its record. Does not spend it; `consume` does that."""
    if not token or not token.startswith(TOKEN_PREFIX):
        return TokenCheck(False, "missing", "no approval token was presented")
    digest = token_hash(token)
    try:
        rows = await store.query(
            APPROVALS, "SELECT * FROM c WHERE c.id = @id",
            [{"name": "@id", "value": f"token:{digest}"}], partition_key=case_id,
        )
    except Exception:
        log.exception("approval token lookup failed")
        return TokenCheck(False, "unavailable", "the approval store did not answer")
    if not rows:
        # Either forged, or minted for a different case (the partition is the case).
        return TokenCheck(False, "unknown", "this token was never issued for this case")
    record = rows[0]
    moment = now or _now()
    # Explicit, even though the case is also the partition key. An authorization check must not
    # depend on a storage layout: a query without a partition key, or a future re-partitioning,
    # would otherwise let a token approved for one case spend another's plan.
    if record.get("case_id") != case_id:
        return TokenCheck(False, "case", "the token was issued for a different case", record)
    if record.get("plan_id") != plan_id:
        return TokenCheck(False, "plan", "the token was issued for a different plan", record)
    if record.get("used"):
        return TokenCheck(False, "used", "the token has already been spent", record)
    if datetime.fromisoformat(str(record["expires_at"])) < moment:
        return TokenCheck(False, "expired", "the token has expired", record)
    return TokenCheck(True, "ok", f"approved by {record.get('approved_by')} "
                                  f"({record.get('approver_role')})", record)


async def consume(store: CosmosStore, record: dict[str, Any], *, operation_id: str) -> bool:
    """Spend a checked token, exactly once. False means someone else spent it first."""
    from azure.core import MatchConditions
    from azure.core.exceptions import ResourceModifiedError

    updated = {k: v for k, v in record.items() if not k.startswith("_")}
    updated.update({"used": True, "used_at": _now().isoformat(),
                    "used_by_operation": operation_id})
    try:
        await store.container(APPROVALS).replace_item(
            item=record["id"], body=updated,
            etag=record["_etag"], match_condition=MatchConditions.IfNotModified,
        )
    except ResourceModifiedError:
        log.warning("approval token for %s lost a race and was not spent twice",
                    record.get("case_id"))
        return False
    return True


async def audit_refusal(store: CosmosStore, *, case_id: str, plan_id: str, code: str,
                        reason: str, correlation_id: str, caller: str = "") -> None:
    """Every refused submission leaves a record. Best effort: never mask the 403 itself."""
    try:
        await store.upsert(APPROVALS, {
            "id": f"refusal:{correlation_id}:{secrets.token_hex(4)}", "case_id": case_id,
            "kind": "approval-refusal", "plan_id": plan_id, "code": code, "reason": reason,
            "correlation_id": correlation_id, "caller": caller, "at": _now().isoformat(),
        })
    except Exception:
        log.exception("could not audit a refused approval for %s", case_id)


__all__ = [
    "APPROVALS",
    "TOKEN_PREFIX",
    "TOKEN_TTL",
    "MintedToken",
    "TokenCheck",
    "audit_refusal",
    "check",
    "consume",
    "mint",
    "token_hash",
]
