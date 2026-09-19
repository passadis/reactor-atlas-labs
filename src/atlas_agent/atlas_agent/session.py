"""Conversation state that survives a process restart.

The agent keeps its history locally in the framework `AgentSession` (`store=False`), rather than in
Foundry's server-side response store. Both were tried against the live deployment; both recall
across turns. Local history was chosen because the transcript then lives in ATLAS's own database -
visible in Cosmos on stage, cleared by `make demo-reset`, independent of how long Foundry retains
responses - and because the hosted agent uses the same setting, so one definition serves both.

`AgentSession.to_dict()` / `from_dict()` is the framework's supported persistence path. The stored
document is treated as opaque trusted state and bound to the session id that created it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from agent_framework import AgentSession
from atlas_common.repositories import CosmosStore

SESSION_CONTAINER = "sessions"
SESSION_TTL_SECONDS = 86_400  # 24 hours, also the container default


class SessionStore(Protocol):
    async def load(self, session_id: str) -> AgentSession | None: ...
    async def save(self, session_id: str, session: AgentSession) -> None: ...
    async def expire(self, session_id: str) -> bool: ...


class InMemorySessionStore:
    """For tests. Round-trips through `to_dict`, so it exercises the same serialisation path."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, object]] = {}

    async def load(self, session_id: str) -> AgentSession | None:
        data = self._data.get(session_id)
        return AgentSession.from_dict(data) if data is not None else None

    async def save(self, session_id: str, session: AgentSession) -> None:
        self._data[session_id] = session.to_dict()

    async def expire(self, session_id: str) -> bool:
        return self._data.pop(session_id, None) is not None


class CosmosSessionStore:
    """Sessions in the Cosmos `sessions` container, partitioned by session id, TTL 24 hours."""

    def __init__(self, store: CosmosStore) -> None:
        self._store = store

    async def load(self, session_id: str) -> AgentSession | None:
        rows = await self._store.query(
            SESSION_CONTAINER,
            "SELECT c.state FROM c WHERE c.id = @id",
            [{"name": "@id", "value": session_id}],
            partition_key=session_id,
        )
        return AgentSession.from_dict(rows[0]["state"]) if rows else None

    async def save(self, session_id: str, session: AgentSession) -> None:
        await self._store.upsert(
            SESSION_CONTAINER,
            {
                "id": session_id,
                "session_id": session_id,
                "state": session.to_dict(),
                "updated_at": datetime.now(UTC).isoformat(),
                "ttl": SESSION_TTL_SECONDS,
            },
        )

    async def expire(self, session_id: str) -> bool:
        return await self._store.delete(SESSION_CONTAINER, session_id, session_id)
