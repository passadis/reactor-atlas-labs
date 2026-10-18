"""The Phase 02 repositories, served by the operations API instead of by Cosmos.

Same method names, same arguments, same return types as `cosmos_repos`. That is the entire trick
behind "switching to HTTP changed exactly one config value" (PHASE-08 section C): the agent tools
hold a repository and never learn which kind it is.

Two differences are real and worth knowing:

- `as_of` is accepted and ignored. The operations API measures relative windows back from its own
  operations clock (`ATLAS_OPS_NOW`, the same value everything else uses), so a caller cannot ask
  for a different "now". That is on purpose: the clock is a property of the deployment, not of a
  request.
- An unreachable API raises `RepositoryUnavailableError`, exactly as a dead Cosmos does, so the
  tools' degraded-mode message ("I could not reach the service catalogue") is unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import Change, Incident, Service, TelemetryWindow
from ..ops_client import OpsApiClient, OpsApiError, OpsApiUnavailableError
from .base import RepositoryUnavailableError


async def _or_none(call: Any) -> dict[str, Any] | None:
    """A 404 is an answer (None), not a failure. Anything unreachable is a failure."""
    try:
        return dict(await call)
    except OpsApiUnavailableError as exc:
        raise RepositoryUnavailableError("operations API", str(exc)) from exc
    except OpsApiError as exc:
        if exc.status == 404:
            return None
        raise RepositoryUnavailableError("operations API", str(exc)) from exc


async def _must(call: Any) -> dict[str, Any]:
    result = await _or_none(call)
    if result is None:
        raise RepositoryUnavailableError("operations API", "no record was returned")
    return result


class HttpServiceRepository:
    def __init__(self, ops: OpsApiClient) -> None:
        self._ops = ops

    async def get(self, service_id: str) -> Service | None:
        body = await _or_none(self._ops.get_service(service_id))
        return Service.model_validate(body) if body else None

    async def list_all(self) -> list[Service]:
        body = await _must(self._ops.list_services())
        return sorted((Service.model_validate(s) for s in body.get("services", [])),
                      key=lambda s: s.service_id)


class HttpIncidentRepository:
    def __init__(self, ops: OpsApiClient) -> None:
        self._ops = ops

    async def get(self, incident_id: str) -> Incident | None:
        body = await _or_none(self._ops.get_incident(incident_id))
        return Incident.model_validate(body) if body else None


class HttpChangeRepository:
    def __init__(self, ops: OpsApiClient) -> None:
        self._ops = ops

    async def recent(self, service_id: str, as_of: datetime,
                     since_minutes: int = 240) -> list[Change]:
        body = await _must(self._ops.list_changes(service_id, since_minutes))
        return [Change.model_validate(c) for c in body.get("changes", [])]


class HttpTelemetryRepository:
    def __init__(self, ops: OpsApiClient) -> None:
        self._ops = ops

    async def windows(self, service_id: str, as_of: datetime,
                      window_minutes: int = 60) -> list[TelemetryWindow]:
        body = await _or_none(self._ops.get_service_health(service_id, window_minutes))
        if body is None:
            return []
        return [TelemetryWindow.model_validate({**w, "service_id": service_id})
                for w in body.get("windows", [])]


__all__ = [
    "HttpChangeRepository",
    "HttpIncidentRepository",
    "HttpServiceRepository",
    "HttpTelemetryRepository",
]
