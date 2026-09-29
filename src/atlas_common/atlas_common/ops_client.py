"""The one client for the ATLAS operations API.

Everything that talks to the ops API over HTTP goes through this class: the agent tools
(`atlas_agent.tools` in HTTP mode), the MCP server, and the orchestrator. That is what makes the
Phase 09 switch a single config value - `ATLAS_OPS_API_URL` moves from the function app to the APIM
gateway, and nothing else changes.

Behaviour, all of it deliberate:

- **Identity, not keys.** A managed-identity token for `api://atlas-ops-api/.default`, from the
  same credential everything else uses. No function key, no subscription key.
- **`traceparent` propagated** from the current span, so a tool call made by an agent shows up
  as a child of that agent's span, through APIM, into the function.
- **Retries only where they are safe**: on 429 and 5xx, with jittered exponential backoff, capped.
  A 4xx other than 429 is the caller's mistake and retrying it only repeats the mistake.
- **A circuit breaker** after five consecutive failures, open for thirty seconds. A dead backend
  then costs one fast `OpsApiUnavailableError` instead of every tool call waiting out its
  retries - the difference between "I could not reach the ticketing system" and a hung demo
  (CLAUDE.md section 6).
- **Typed errors.** The API's `{code, message, correlation_id}` envelope becomes `OpsApiError`
  carrying all three, so a caller can show the correlation id rather than a stack trace.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

from .telemetry import inject_context

log = logging.getLogger("atlas.ops_client")

OPS_API_SCOPE = "api://atlas-ops-api/.default"
RETRYABLE = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3
BREAKER_THRESHOLD = 5
BREAKER_OPEN_S = 30.0


class OpsApiError(Exception):
    """The ops API answered with an error envelope."""

    def __init__(self, status: int, code: str, message: str, correlation_id: str = "") -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message, self.correlation_id = (
            status, code, message, correlation_id)


class OpsApiUnavailableError(OpsApiError):
    """The ops API could not be reached, or the circuit breaker is open."""

    def __init__(self, message: str, correlation_id: str = "") -> None:
        super().__init__(503, "unavailable", message, correlation_id)


class CircuitBreaker:
    """Open after N consecutive failures; half-open after a cool-down; closed on a success."""

    def __init__(self, threshold: int = BREAKER_THRESHOLD, open_s: float = BREAKER_OPEN_S) -> None:
        self.threshold, self.open_s = threshold, open_s
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.monotonic() - self.opened_at >= self.open_s:
            return False  # half-open: let one request through to find out
        return True

    def record_success(self) -> None:
        self.failures, self.opened_at = 0, None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            if self.opened_at is None:
                log.warning("ops API circuit opened after %d consecutive failures", self.failures)
            self.opened_at = time.monotonic()


class OpsApiClient:
    """Async client for the ATLAS operations API. One per process; reuse it."""

    def __init__(self, base_url: str, credential: Any | None = None, *,
                 timeout_s: float = 10.0, transport: httpx.AsyncBaseTransport | None = None,
                 breaker: CircuitBreaker | None = None) -> None:
        if not base_url:
            raise ValueError("ATLAS_OPS_API_URL is not set")
        self.base_url = base_url.rstrip("/")
        self._credential = credential
        self._token: tuple[str, float] | None = None
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=timeout_s,
                                       transport=transport)
        self.breaker = breaker or CircuitBreaker()

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> OpsApiClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def _bearer(self) -> dict[str, str]:
        if self._credential is None:
            return {}
        now = time.time()
        if self._token is None or self._token[1] - now < 120:
            token = await self._credential.get_token(OPS_API_SCOPE)
            self._token = (token.token, float(token.expires_on))
        return {"Authorization": f"Bearer {self._token[0]}"}

    async def request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                      json: Any = None, idempotency_key: str | None = None) -> httpx.Response:
        """Send one request with retries and the breaker. Returns the final 2xx response."""
        if self.breaker.is_open:
            raise OpsApiUnavailableError("the operations API is not answering; the circuit is open")

        # `inject_context` RETURNS the carrier rather than filling the one passed in; calling it
        # for its side effect silently drops the traceparent and breaks the trace at this hop.
        headers: dict[str, str] = inject_context()
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        last: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._http.request(
                    method, path, params=params, json=json,
                    headers={**headers, **await self._bearer()})
            except (httpx.TransportError, TimeoutError) as exc:
                last = exc
                self.breaker.record_failure()
                if attempt < MAX_ATTEMPTS and not self.breaker.is_open:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                raise OpsApiUnavailableError(f"could not reach the operations API: "
                                        f"{type(exc).__name__}") from exc

            if response.status_code in RETRYABLE:
                self.breaker.record_failure()
                if attempt < MAX_ATTEMPTS and not self.breaker.is_open:
                    await asyncio.sleep(self._backoff(attempt, response))
                    continue
            elif response.status_code < 500:
                # 2xx and 4xx both prove the service is up. A 4xx is the caller's problem.
                self.breaker.record_success()

            if response.is_success:
                return response
            raise self._error(response)
        raise OpsApiUnavailableError(f"the operations API failed: {last}")

    @staticmethod
    def _backoff(attempt: int, response: httpx.Response | None = None) -> float:
        retry_after = response.headers.get("Retry-After") if response is not None else None
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), 10.0)
        base: float = min(0.4 * 2.0 ** (attempt - 1), 4.0)
        return base + random.uniform(0, 0.3)  # noqa: S311

    @staticmethod
    def _error(response: httpx.Response) -> OpsApiError:
        try:
            body = response.json()
        except ValueError:
            body = {}
        cid = str(body.get("correlation_id") or response.headers.get("x-atlas-correlation-id", ""))
        if response.status_code in RETRYABLE:
            message = str(body.get("message") or "the operations API failed")
            return OpsApiUnavailableError(message, cid)
        return OpsApiError(response.status_code, str(body.get("code") or "error"),
                           str(body.get("message") or response.reason_phrase), cid)

    # -- the operations, one method per operationId in apim/openapi/atlas-ops-api.yaml --------

    async def list_services(self) -> dict[str, Any]:
        return dict((await self.request("GET", "/services")).json())

    async def get_service(self, service_id: str) -> dict[str, Any]:
        return dict((await self.request("GET", f"/services/{service_id}")).json())

    async def get_service_health(self, service_id: str, window_minutes: int = 60) -> dict[str, Any]:
        return dict((await self.request("GET", f"/services/{service_id}/health",
                                        params={"window_minutes": window_minutes})).json())

    async def get_blast_radius(self, service_id: str, depth: int = 3) -> dict[str, Any]:
        return dict((await self.request("GET", f"/services/{service_id}/blast-radius",
                                        params={"depth": depth})).json())

    async def get_incident(self, incident_id: str) -> dict[str, Any]:
        return dict((await self.request("GET", f"/incidents/{incident_id}")).json())

    async def list_changes(self, service_id: str, since_minutes: int = 240) -> dict[str, Any]:
        return dict((await self.request("GET", "/changes", params={
            "service_id": service_id, "since_minutes": since_minutes})).json())

    async def create_ticket(self, *, case_id: str, severity: str, summary: str,
                            service_ids: list[str] | None = None) -> tuple[dict[str, Any], bool]:
        """Returns (ticket, replayed). The case id is the idempotency key: one ticket per case."""
        response = await self.request("POST", "/tickets", idempotency_key=case_id, json={
            "case_id": case_id, "severity": severity, "summary": summary,
            "service_ids": service_ids or []})
        return dict(response.json()), response.headers.get("Idempotency-Replayed") == "true"

    async def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        return dict((await self.request("GET", f"/tickets/{ticket_id}")).json())

    async def submit_remediation(self, *, case_id: str, plan_id: str,
                                 approval_token: str) -> tuple[dict[str, Any], bool]:
        """Returns (operation, replayed). The plan id is the idempotency key."""
        response = await self.request("POST", "/remediation/commands", idempotency_key=plan_id,
                                      json={"case_id": case_id, "plan_id": plan_id,
                                            "approval_token": approval_token})
        return dict(response.json()), response.headers.get("Idempotency-Replayed") == "true"

    async def get_operation(self, operation_id: str) -> dict[str, Any]:
        return dict((await self.request(
            "GET", f"/remediation/operations/{operation_id}")).json())


__all__ = [
    "OPS_API_SCOPE",
    "CircuitBreaker",
    "OpsApiClient",
    "OpsApiError",
    "OpsApiUnavailableError",
]
