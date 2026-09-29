"""func-atlas-mcp: the ATLAS MCP tool server (Azure Functions MCP extension, Python v2 model).

Six tools, each a `mcp_tool_trigger` function. The contract - name, description, properties - is
defined once in `opsmcp.tools.TOOLS`; this module only registers it, so `tools/list` and the tests
read the same words the model does.

Transport and auth, as the extension does them:

- Streamable HTTP at `/runtime/webhooks/mcp` (the deprecated SSE transport at `/sse` is not used).
- The endpoint requires the `mcp_extension` **system key** (`host.json` leaves
  `webhookAuthorizationLevel` at its default, `System`). That key is never in source: Phase 09
  stores it in Key Vault and API Management presents it, so no agent ever holds it.

Written one function per tool rather than generated in a loop: the Functions indexer needs each
function to be a distinct module-level name, and six explicit registrations read better on stage.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from typing import Any

import azure.functions as func
from atlas_common.auth import get_async_credential
from atlas_common.config import Settings
from atlas_common.models import Person, Service
from atlas_common.ops_client import OpsApiClient
from atlas_common.repositories import CosmosStore, KnowledgeSearch
from atlas_common.repositories.cosmos_repos import EntitlementRepository, ServiceRepository
from atlas_common.telemetry import configure_telemetry
from opsmcp import tools

log = logging.getLogger("atlas.opsmcp")
configure_telemetry("atlas-ops-mcp")

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)
TOOL = {t.name: t for t in tools.TOOLS}


class LiveBackend:
    """Real stores and the real ops API client, created once per worker and reused."""

    def __init__(self) -> None:
        self._credential = get_async_credential()
        self._settings = Settings.from_env()
        self._store = CosmosStore(self._settings, self._credential)
        self._search = KnowledgeSearch(self._settings, self._credential)
        self._ops = OpsApiClient(os.environ.get("ATLAS_OPS_API_URL", ""), self._credential)

    async def services(self) -> list[Service]:
        return await ServiceRepository(self._store).list_all()

    async def people(self) -> list[Person]:
        return await EntitlementRepository(self._store).people()

    async def search(self, query: str, *, services: Sequence[str] | None,
                     doc_types: Sequence[str] | None, top: int) -> dict[str, Any]:
        results = await self._search.search(query, services=services,
                                            doc_types=doc_types, top=top)  # type: ignore[arg-type]
        return {"query": results.query, "citations": results.citations,
                "hits": [h.model_dump(mode="json") for h in results.hits]}

    def ops(self) -> OpsApiClient:
        return self._ops


_backend: LiveBackend | None = None


def backend() -> LiveBackend:
    global _backend
    if _backend is None:
        _backend = LiveBackend()
    return _backend


def _spec(name: str) -> dict[str, str]:
    tool = TOOL[name]
    return {"tool_name": tool.name, "description": tool.description,
            "tool_properties": tool.properties_json()}


@app.mcp_tool_trigger(arg_name="context", **_spec("search_ops_knowledge"))
async def search_ops_knowledge(context: str) -> str:
    return await tools.invoke(backend(), "search_ops_knowledge", context)


@app.mcp_tool_trigger(arg_name="context", **_spec("get_service_topology"))
async def get_service_topology(context: str) -> str:
    return await tools.invoke(backend(), "get_service_topology", context)


@app.mcp_tool_trigger(arg_name="context", **_spec("get_oncall"))
async def get_oncall(context: str) -> str:
    return await tools.invoke(backend(), "get_oncall", context)


@app.mcp_tool_trigger(arg_name="context", **_spec("open_ticket"))
async def open_ticket(context: str) -> str:
    return await tools.invoke(backend(), "open_ticket", context)


@app.mcp_tool_trigger(arg_name="context", **_spec("submit_remediation"))
async def submit_remediation(context: str) -> str:
    return await tools.invoke(backend(), "submit_remediation", context)


@app.mcp_tool_trigger(arg_name="context", **_spec("get_remediation_status"))
async def get_remediation_status(context: str) -> str:
    return await tools.invoke(backend(), "get_remediation_status", context)


@app.route(route="health", methods=["GET"])
async def health(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(json.dumps({"status": "ok", "tools": len(tools.TOOLS)}),
                             mimetype="application/json")
