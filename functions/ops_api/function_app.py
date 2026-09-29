"""func-atlas-api: the ATLAS operations API, as Azure Functions (Python v2 programming model).

A thin adapter. Each route parses the request, calls the matching handler in `opsapi.core`, and
turns the `Result` into an HTTP response. The behaviour - validation, idempotency, the approval
token check, the error envelope - is all in `core`, where it is unit-tested without a host.

Authentication is NOT done here. Every function is `AuthLevel.ANONYMOUS` because App Service
authentication rejects any request without a valid Entra token for `api://atlas-ops-api` before
this code runs (infra/modules/compute-functions.bicep, `authsettingsV2`). A function key would be a
second, weaker credential for the same door.

The contract is `apim/openapi/atlas-ops-api.yaml`, written by hand; `tests/test_ops_api.py` asserts
these routes match it exactly.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

import azure.functions as func
from atlas_common.auth import get_async_credential
from atlas_common.config import DEFAULT_OPS_NOW
from atlas_common.repositories import CosmosStore
from atlas_common.telemetry import configure_telemetry
from opsapi import core

log = logging.getLogger("atlas.opsapi")
configure_telemetry("atlas-ops-api")

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


class LiveDeps:
    """Real dependencies, created once per worker process and reused across invocations."""

    def __init__(self) -> None:
        self._credential = get_async_credential()
        # Settings needs the Foundry values only for the agent; the API needs Cosmos and the clock.
        self.store = CosmosStore(_settings(), self._credential)
        self.ops_now = datetime.fromisoformat(
            os.environ.get("ATLAS_OPS_NOW", DEFAULT_OPS_NOW).replace("Z", "+00:00"))
        self._queue = os.environ.get("ATLAS_COMMANDS_QUEUE", "remediation-commands")
        self._fqdn = os.environ.get("ATLAS_SERVICEBUS_FQDN", "")

    async def enqueue(self, *, message_id: str, session_id: str, body: dict[str, Any],
                      correlation: str) -> None:
        from azure.servicebus import ServiceBusMessage
        from azure.servicebus.aio import ServiceBusClient

        async with ServiceBusClient(self._fqdn, credential=self._credential) as client:
            sender = client.get_queue_sender(self._queue)
            async with sender:
                await sender.send_messages(ServiceBusMessage(
                    json.dumps(body), message_id=message_id, session_id=session_id,
                    correlation_id=correlation, content_type="application/json",
                    application_properties={"traceparent": f"00-{correlation}-0000000000000000-01"},
                ))


def _settings() -> Any:
    """Just enough Settings for CosmosStore, without demanding the agent's Foundry variables."""
    from atlas_common.config import Settings

    return Settings(
        cosmos_endpoint=os.environ["ATLAS_COSMOS_ENDPOINT"],
        cosmos_database=os.environ.get("ATLAS_COSMOS_DATABASE", "atlas"),
        search_endpoint=os.environ.get("ATLAS_SEARCH_ENDPOINT", ""),
        foundry_project_endpoint=os.environ.get("FOUNDRY_PROJECT_ENDPOINT", ""),
        foundry_account_name=os.environ.get("ATLAS_FOUNDRY_ACCOUNT_NAME", ""),
    )


_deps: LiveDeps | None = None


def deps() -> LiveDeps:
    global _deps
    if _deps is None:
        _deps = LiveDeps()
    return _deps


def respond(result: core.Result) -> func.HttpResponse:
    body = "" if result.body is None else json.dumps(result.body, default=str)
    return func.HttpResponse(body, status_code=result.status, headers=result.headers,
                             mimetype="application/json")


def caller(req: func.HttpRequest) -> str:
    """Who called, as App Service authentication reports it. For the audit trail only."""
    return (req.headers.get("x-ms-client-principal-name")
            or req.headers.get("x-ms-client-principal-id") or "")


def int_param(req: func.HttpRequest, name: str, default: int) -> int | None:
    raw = req.params.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return None


def json_body(req: func.HttpRequest) -> Any:
    try:
        return req.get_json()
    except ValueError:
        return None


# ------------------------------------------------------------------------ routes --


@app.route(route="health", methods=["GET"])
async def health(req: func.HttpRequest) -> func.HttpResponse:
    return respond(core.Result(200, {"status": "ok", "version": "1.0.0"}))


@app.route(route="services", methods=["GET"])
async def list_services(req: func.HttpRequest) -> func.HttpResponse:
    cid = core.correlation_id(req.headers)
    return respond(await core.guarded(cid, lambda: core.list_services(deps(), cid)))


@app.route(route="services/{service_id}", methods=["GET"])
async def get_service(req: func.HttpRequest) -> func.HttpResponse:
    cid = core.correlation_id(req.headers)
    sid = req.route_params["service_id"]
    return respond(await core.guarded(cid, lambda: core.get_service(deps(), sid, cid)))


@app.route(route="services/{service_id}/health", methods=["GET"])
async def get_service_health(req: func.HttpRequest) -> func.HttpResponse:
    cid = core.correlation_id(req.headers)
    sid = req.route_params["service_id"]
    window = int_param(req, "window_minutes", 60)
    if window is None:
        return respond(core.error(400, "invalid_request", "window_minutes must be an integer", cid))
    return respond(await core.guarded(
        cid, lambda: core.get_service_health(deps(), sid, window, cid)))


@app.route(route="services/{service_id}/blast-radius", methods=["GET"])
async def get_blast_radius(req: func.HttpRequest) -> func.HttpResponse:
    cid = core.correlation_id(req.headers)
    sid = req.route_params["service_id"]
    depth = int_param(req, "depth", 3)
    if depth is None:
        return respond(core.error(400, "invalid_request", "depth must be an integer", cid))
    return respond(await core.guarded(cid, lambda: core.get_blast_radius(deps(), sid, depth, cid)))


@app.route(route="incidents/{incident_id}", methods=["GET"])
async def get_incident(req: func.HttpRequest) -> func.HttpResponse:
    cid = core.correlation_id(req.headers)
    iid = req.route_params["incident_id"]
    return respond(await core.guarded(cid, lambda: core.get_incident(deps(), iid, cid)))


@app.route(route="changes", methods=["GET"])
async def list_changes(req: func.HttpRequest) -> func.HttpResponse:
    cid = core.correlation_id(req.headers)
    since = int_param(req, "since_minutes", 240)
    if since is None:
        return respond(core.error(400, "invalid_request", "since_minutes must be an integer", cid))
    sid = req.params.get("service_id")
    return respond(await core.guarded(cid, lambda: core.list_changes(deps(), sid, since, cid)))


@app.route(route="tickets", methods=["POST"])
async def create_ticket(req: func.HttpRequest) -> func.HttpResponse:
    cid = core.correlation_id(req.headers)
    body = json_body(req)
    if not isinstance(body, dict):
        return respond(core.error(400, "invalid_request", "the body must be a JSON object", cid))
    key = req.headers.get("Idempotency-Key")
    return respond(await core.guarded(cid, lambda: core.create_ticket(deps(), body, key, cid)))


@app.route(route="tickets/{ticket_id}", methods=["GET"])
async def get_ticket(req: func.HttpRequest) -> func.HttpResponse:
    cid = core.correlation_id(req.headers)
    tid = req.route_params["ticket_id"]
    return respond(await core.guarded(cid, lambda: core.get_ticket(deps(), tid, cid)))


@app.route(route="remediation/commands", methods=["POST"])
async def submit_remediation(req: func.HttpRequest) -> func.HttpResponse:
    cid = core.correlation_id(req.headers)
    body = json_body(req)
    if not isinstance(body, dict):
        return respond(core.error(400, "invalid_request", "the body must be a JSON object", cid))
    key = req.headers.get("Idempotency-Key")
    who = caller(req)
    return respond(await core.guarded(
        cid, lambda: core.submit_remediation(deps(), body, key, cid, caller=who)))


@app.route(route="remediation/operations/{operation_id}", methods=["GET"])
async def get_operation(req: func.HttpRequest) -> func.HttpResponse:
    cid = core.correlation_id(req.headers)
    oid = req.route_params["operation_id"]
    return respond(await core.guarded(cid, lambda: core.get_operation(deps(), oid, cid)))
