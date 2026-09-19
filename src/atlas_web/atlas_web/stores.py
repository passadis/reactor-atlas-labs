"""Turns and cases, in Cosmos.

**Turns** live in the `sessions` container next to the conversation they belong to. The web app runs
two replicas: `POST /api/chat` and the `GET .../stream` that follows can land on different ones, so
a pending turn cannot live in process memory. A turn is claimed exactly once (`pending` ->
`streaming`); a browser `EventSource` auto-reconnect finds it claimed and is told the turn is over,
instead of silently running the agent a second time.

**Cases** are what a triage produces: the validated `IncidentTriage`, the tool timeline, the
citations, and the trace id. Phase 06/07 extend the same document with typed workflow state, a
plan and an approval. `make demo-reset` clears the container.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from atlas_common.repositories import CosmosStore
from atlas_common.repositories.base import with_timeout
from azure.core import MatchConditions
from azure.core.exceptions import ResourceModifiedError

SESSIONS = "sessions"
CASES = "cases"
TURN_TTL_S = 3600


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TurnStore:
    def __init__(self, store: CosmosStore) -> None:
        self._store = store

    async def create(self, session_id: str, **fields: Any) -> dict[str, Any]:
        turn = {
            "id": f"turn-{uuid.uuid4().hex}",
            "session_id": session_id,
            "kind": "turn",
            "status": "pending",
            "created_at": _now(),
            "ttl": TURN_TTL_S,
            **fields,
        }
        await self._store.upsert(SESSIONS, turn)
        return turn

    async def get(self, turn_id: str) -> dict[str, Any] | None:
        rows = await self._store.query(
            SESSIONS, "SELECT * FROM c WHERE c.id = @id", [{"name": "@id", "value": turn_id}]
        )
        return rows[0] if rows else None

    async def claim(self, turn: dict[str, Any]) -> bool:
        """Move pending -> streaming exactly once, across replicas. False if someone else won."""
        if turn.get("status") != "pending":
            return False
        body = {k: v for k, v in turn.items() if not k.startswith("_")}
        body.update(status="streaming", updated_at=_now())
        container = self._store.container(SESSIONS)

        async def replace() -> bool:
            try:
                await container.replace_item(
                    turn["id"],
                    body,
                    etag=turn["_etag"],
                    match_condition=MatchConditions.IfNotModified,
                )
            except ResourceModifiedError:
                return False
            return True

        won = await with_timeout(replace(), "Cosmos sessions")
        if won:
            turn.update(body)
        return won

    async def cancel_requested(self, turn_id: str, session_id: str) -> bool:
        rows = await self._store.query(
            SESSIONS,
            "SELECT VALUE c.cancel_requested FROM c WHERE c.id = @id",
            [{"name": "@id", "value": turn_id}],
            partition_key=session_id,
        )
        return bool(rows and rows[0])

    async def count_completed(self, session_id: str) -> int:
        rows = await self._store.query(
            SESSIONS,
            "SELECT VALUE COUNT(1) FROM c WHERE c.kind = 'turn' AND c.status = 'done'",
            partition_key=session_id,
        )
        return int(cast(Any, rows[0])) if rows else 0

    async def update(self, turn: dict[str, Any], **fields: Any) -> dict[str, Any]:
        turn.update(fields, updated_at=_now())
        await self._store.upsert(SESSIONS, {k: v for k, v in turn.items() if not k.startswith("_")})
        return turn

    async def shape_state(self, session_id: str, shape: str) -> dict[str, Any]:
        """Per-session state for a hosting shape, e.g. the prompt agent's previous response id."""
        rows = await self._store.query(
            SESSIONS,
            "SELECT * FROM c WHERE c.id = @id",
            [{"name": "@id", "value": f"shape-{shape}-{session_id}"}],
            partition_key=session_id,
        )
        return rows[0] if rows else {}

    async def save_shape_state(self, session_id: str, shape: str, **fields: Any) -> None:
        await self._store.upsert(
            SESSIONS,
            {
                "id": f"shape-{shape}-{session_id}",
                "session_id": session_id,
                "kind": "shape-state",
                "ttl": 86_400,
                **fields,
            },
        )


class CaseStore:
    def __init__(self, store: CosmosStore) -> None:
        self._store = store

    @staticmethod
    def new_id() -> str:
        return f"CASE-{uuid.uuid4().hex[:8].upper()}"

    async def save(self, case: dict[str, Any]) -> None:
        case.setdefault("created_at", _now())
        case["updated_at"] = _now()
        await self._store.upsert(CASES, case)

    async def get(self, case_id: str) -> dict[str, Any] | None:
        rows = await self._store.query(
            CASES,
            "SELECT * FROM c WHERE c.id = @id",
            [{"name": "@id", "value": case_id}],
            partition_key=case_id,
        )
        if not rows:
            return None
        return {k: v for k, v in rows[0].items() if not k.startswith("_")}

    async def list_awaiting_approval(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self._store.query(
            CASES,
            "SELECT TOP @n c.id, c.case_id, c.title, c.severity, c.plan, c.created_at FROM c "
            "WHERE IS_DEFINED(c.plan) AND c.plan.status = 'awaiting_approval' "
            "ORDER BY c.created_at DESC",
            [{"name": "@n", "value": limit}],
        )
        return rows
