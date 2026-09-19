"""Typed read repositories over the seeded Cosmos containers.

Written once, here, and called by the ops API (Phase 08) and the agent tools (Phase 03). Every
method is async, bounded by a timeout, and returns domain models.

Time windows are always relative to an explicit `as_of`, never the wall clock. The canonical demo
incident happens at a fixed moment on the reference date, so "changes in the last four hours" has
to mean four hours before that moment, or the same prompt would return different answers depending
on when the demo runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..models import Change, Entitlement, Incident, Person, Service, TelemetryWindow
from .base import CosmosStore, to_model


def _iso(moment: datetime) -> str:
    """The stored timestamp format: UTC, second precision, trailing Z."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class ServiceRepository:
    def __init__(self, store: CosmosStore) -> None:
        self._store = store

    async def get(self, service_id: str) -> Service | None:
        rows = await self._store.query(
            "services",
            "SELECT * FROM c WHERE c.service_id = @id",
            [{"name": "@id", "value": service_id}],
            partition_key=service_id,
        )
        return to_model(Service, rows[0]) if rows else None

    async def list_all(self) -> list[Service]:
        rows = await self._store.query("services", "SELECT * FROM c")
        return sorted((to_model(Service, r) for r in rows), key=lambda s: s.service_id)


class IncidentRepository:
    def __init__(self, store: CosmosStore) -> None:
        self._store = store

    async def get(self, incident_id: str) -> Incident | None:
        rows = await self._store.query(
            "incidents",
            "SELECT * FROM c WHERE c.incident_id = @id",
            [{"name": "@id", "value": incident_id}],
            partition_key=incident_id,
        )
        return to_model(Incident, rows[0]) if rows else None

    async def for_service(self, service_id: str, limit: int = 10) -> list[Incident]:
        """Most recent incidents that affected a service. Cross-partition by design."""
        rows = await self._store.query(
            "incidents",
            "SELECT TOP @limit * FROM c WHERE ARRAY_CONTAINS(c.affected_services, @svc) "
            "ORDER BY c.opened_at DESC",
            [{"name": "@svc", "value": service_id}, {"name": "@limit", "value": limit}],
        )
        return [to_model(Incident, r) for r in rows]


class ChangeRepository:
    def __init__(self, store: CosmosStore) -> None:
        self._store = store

    async def get(self, change_id: str) -> Change | None:
        rows = await self._store.query(
            "changes",
            "SELECT * FROM c WHERE c.change_id = @id",
            [{"name": "@id", "value": change_id}],
        )
        return to_model(Change, rows[0]) if rows else None

    async def recent(
        self, service_id: str, as_of: datetime, since_minutes: int = 240
    ) -> list[Change]:
        """Changes to a service in the window ending at `as_of`. Newest first."""
        rows = await self._store.query(
            "changes",
            "SELECT * FROM c WHERE c.service_id = @svc "
            "AND c.deployed_at >= @start AND c.deployed_at <= @end ORDER BY c.deployed_at DESC",
            [
                {"name": "@svc", "value": service_id},
                {"name": "@start", "value": _iso(as_of - timedelta(minutes=since_minutes))},
                {"name": "@end", "value": _iso(as_of)},
            ],
            partition_key=service_id,
        )
        return [to_model(Change, r) for r in rows]


class TelemetryRepository:
    def __init__(self, store: CosmosStore) -> None:
        self._store = store

    async def windows(
        self, service_id: str, as_of: datetime, window_minutes: int = 60
    ) -> list[TelemetryWindow]:
        """Telemetry windows for a service in the period ending at `as_of`. Oldest first."""
        rows = await self._store.query(
            "telemetry",
            "SELECT * FROM c WHERE c.service_id = @svc "
            "AND c.window_start >= @start AND c.window_start <= @end ORDER BY c.window_start",
            [
                {"name": "@svc", "value": service_id},
                {"name": "@start", "value": _iso(as_of - timedelta(minutes=window_minutes))},
                {"name": "@end", "value": _iso(as_of)},
            ],
            partition_key=service_id,
        )
        return [to_model(TelemetryWindow, r) for r in rows]


class EntitlementRepository:
    """The authorization source of truth for the policy gate. Read by code, never by a model."""

    def __init__(self, store: CosmosStore) -> None:
        self._store = store

    async def for_role(self, role: str) -> Entitlement | None:
        rows = await self._store.query(
            "entitlements",
            "SELECT * FROM c WHERE c.role = @role",
            [{"name": "@role", "value": role}],
            partition_key=role,
        )
        return to_model(Entitlement, rows[0]) if rows else None

    async def person(self, user_id: str) -> Person | None:
        rows = await self._store.query(
            "people",
            "SELECT * FROM c WHERE c.user_id = @id",
            [{"name": "@id", "value": user_id}],
            partition_key=user_id,
        )
        return to_model(Person, rows[0]) if rows else None

    async def person_by_email(self, email: str) -> Person | None:
        rows = await self._store.query(
            "people",
            "SELECT * FROM c WHERE LOWER(c.email) = @email",
            [{"name": "@email", "value": email.lower()}],
        )
        return to_model(Person, rows[0]) if rows else None

    async def people(self) -> list[Person]:
        rows = await self._store.query("people", "SELECT * FROM c")
        return [to_model(Person, r) for r in rows]
