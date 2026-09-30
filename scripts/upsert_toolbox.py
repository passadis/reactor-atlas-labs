"""Create or update the Foundry Toolbox `atlas-tools`, and the connection agents use to reach it.

    uv run python -m scripts.upsert_toolbox           # idempotent: new version only on change
    uv run python -m scripts.upsert_toolbox --check   # exit non-zero if missing or drifted

A Toolbox is a named, versioned bundle of tools behind ONE MCP endpoint. Episode 1 says "tools are
curated once and shared"; Episode 3 adds the APIM-fronted MCP servers to this same toolbox
(Phase 09) without touching any agent. It holds:

- `atlas_knowledge` - the knowledge index (Azure AI Search, through the project's connection);
- `atlas_ops`       - APIM `atlas-ops-mcp`: the ops API as MCP tools, descriptions governed in APIM;
- `atlas_tools`     - APIM `atlas-tools-mcp`: the Functions MCP server, behind the gateway.

The two MCP servers are reached through project connections that authenticate as the Foundry
project's MANAGED IDENTITY for audience api://atlas-gateway (it holds Atlas.Agent). Chosen over
the agentic identity because the toolbox is shared: the prompt agent and any future consumer reach
the gateway as one governed principal, which is also what the rate limit counts. And chosen over a
key because a connection holds exactly one credential and a key stored there would sit outside Key
Vault (CLAUDE.md section 1).

Three things this script owns:

1. **The toolbox** (`project.toolboxes.create_version`). Its definition is fingerprinted into the
   version metadata; a re-run with the same definition creates nothing. A new version is promoted
   to the toolbox's default, so consumers on the default endpoint pick it up with no redeploy.
2. **The `atlas-tools-mcp` project connection** (category RemoteTool, user Entra token, audience
   https://ai.azure.com) pointing at the toolbox's default MCP endpoint. A prompt agent reaches a
   toolbox only through such a connection (docs/API-NOTES.md 12). Created with `azd ai connection`
   when missing.
3. **The two gateway connections** (`atlas-apim-ops-mcp`, `atlas-apim-tools-mcp`), project
   managed identity, audience api://atlas-gateway.

The prompt agent is attached in scripts/create_prompt_agent.py; the attach is server-side, in the
agent definition (CLAUDE.md 7.5). Passing a toolbox client-side would change nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from typing import Any

from atlas_common.auth import get_credential
from atlas_common.config import Settings
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AISearchIndexResource,
    AzureAISearchQueryType,
    AzureAISearchToolboxTool,
    AzureAISearchToolResource,
    MCPToolboxTool,
)
from azure.core.exceptions import ResourceNotFoundError

TOOLBOX = "atlas-tools"
CONNECTION = "atlas-tools-mcp"
SEARCH_CONNECTION = "aisearch"
GATEWAY_AUDIENCE = "api://atlas-gateway"


def gateway_servers() -> dict[str, tuple[str, str]]:
    """server label -> (project connection name, APIM MCP endpoint). Empty before Phase 09."""
    ops_mcp = os.environ.get("ATLAS_APIM_OPS_MCP_URL", "").strip()
    if not ops_mcp:
        return {}
    gateway = ops_mcp.rsplit("/ops-mcp/mcp", 1)[0]
    return {
        "atlas_ops": ("atlas-apim-ops-mcp", ops_mcp),
        "atlas_tools": ("atlas-apim-tools-mcp", f"{gateway}/tools-mcp/mcp"),
    }


def toolbox_endpoint(settings: Settings) -> str:
    """The consumer endpoint: follows the toolbox's default version."""
    return f"{settings.foundry_project_endpoint.rstrip('/')}/toolboxes/{TOOLBOX}/mcp?api-version=v1"


def build_tools(settings: Settings, search_connection_id: str) -> list[Any]:
    return [
        AzureAISearchToolboxTool(
            name="atlas_knowledge",
            description=(
                "Search Contoso's operational knowledge: runbooks (titles start with RB-), past "
                "incidents (INC-) and postmortems. Cite the id at the start of each title."
            ),
            azure_ai_search=AzureAISearchToolResource(
                indexes=[
                    AISearchIndexResource(
                        project_connection_id=search_connection_id,
                        index_name=settings.knowledge_index,
                        query_type=AzureAISearchQueryType.VECTOR_SEMANTIC_HYBRID,
                        top_k=5,
                    )
                ]
            ),
        )
    ] + [
        MCPToolboxTool(
            server_label=label,
            server_url=url,
            server_description=(
                "ATLAS operations tools through the API Management gateway: service catalogue, "
                "incidents, recent changes, telemetry and blast radius." if label == "atlas_ops"
                else "ATLAS Functions tools through the API Management gateway: on-call, "
                     "topology, runbook search, tickets and remediation."),
            require_approval="never",
            project_connection_id=connection,
        )
        for label, (connection, url) in gateway_servers().items()
    ]


def fingerprint(tools: list[Any]) -> str:
    canonical = json.dumps([t.as_dict() for t in tools], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def latest_version(project: AIProjectClient) -> Any | None:
    try:
        for version in project.toolboxes.list_versions(TOOLBOX, limit=1, order="desc"):
            return version
    except ResourceNotFoundError:
        return None
    return None


def ensure_toolbox(project: AIProjectClient, settings: Settings) -> tuple[str, bool]:
    search_id = project.connections.get(SEARCH_CONNECTION).id
    tools = build_tools(settings, search_id)
    sha = fingerprint(tools)
    current = latest_version(project)
    if current is not None and (current.metadata or {}).get("definition_sha") == sha:
        version, created = str(current.version), False
    else:
        made = project.toolboxes.create_version(
            name=TOOLBOX,
            description=("ATLAS shared tools: knowledge index, and the ops and Functions MCP "
                         "servers through APIM."),
            tools=tools,
            metadata={"definition_sha": sha, "managed_by": "scripts/upsert_toolbox.py"},
        )
        version, created = str(made.version), True
    if str(project.toolboxes.get(TOOLBOX).default_version) != version:
        project.toolboxes.update(TOOLBOX, default_version=version)
    return version, created


def publish(project: AIProjectClient, settings: Settings, reason: str) -> str:
    """Publish a new toolbox version with the SAME definition and make it the default.

    Why: the toolbox caches the tool list it discovered from each MCP server, with a sliding expiry
    (docs/API-NOTES.md 16.10). A change made in APIM - a tool description - reaches the prompt
    agent only when that cache lapses, anywhere from about a minute to never while something keeps
    asking. A new version rediscovers immediately (measured 9 s). The definition is unchanged, so
    this is a refresh, not a change of what the toolbox holds.
    """
    search_id = project.connections.get(SEARCH_CONNECTION).id
    tools = build_tools(settings, search_id)
    made = project.toolboxes.create_version(
        name=TOOLBOX,
        description=("ATLAS shared tools: knowledge index, and the ops and Functions MCP "
                     "servers through APIM."),
        tools=tools,
        metadata={"definition_sha": fingerprint(tools), "managed_by": "scripts/upsert_toolbox.py",
                  "published_for": reason[:120]},
    )
    project.toolboxes.update(TOOLBOX, default_version=str(made.version))
    return str(made.version)


def ensure_connection(project: AIProjectClient, settings: Settings) -> str:
    try:
        return str(project.connections.get(CONNECTION).id)
    except ResourceNotFoundError:
        pass
    azd = shutil.which("azd") or "azd"
    subprocess.run(
        [azd, "ai", "connection", "create", CONNECTION, "-C", "agents",
         "--kind", "remote-tool", "--target", toolbox_endpoint(settings),
         "--auth-type", "user-entra-token", "--audience", "https://ai.azure.com", "--no-prompt"],
        check=True, capture_output=True, text=True, env={**os.environ, "MSYS_NO_PATHCONV": "1"},
    )
    return str(project.connections.get(CONNECTION).id)


def ensure_gateway_connections(project: AIProjectClient) -> list[str]:
    """One RemoteTool connection per APIM MCP server, as the project's managed identity."""
    made: list[str] = []
    azd = shutil.which("azd") or "azd"
    for connection, url in gateway_servers().values():
        try:
            project.connections.get(connection)
            continue
        except ResourceNotFoundError:
            pass
        subprocess.run(
            [azd, "ai", "connection", "create", connection, "-C", "agents",
             "--kind", "remote-tool", "--target", url,
             "--auth-type", "project-managed-identity", "--audience", GATEWAY_AUDIENCE,
             "--no-prompt"],
            check=True, capture_output=True, text=True,
            env={**os.environ, "MSYS_NO_PATHCONV": "1"},
        )
        made.append(connection)
    return made


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    project = AIProjectClient(endpoint=settings.foundry_project_endpoint,
                              credential=get_credential())
    if args.check:
        current = latest_version(project)
        search_id = project.connections.get(SEARCH_CONNECTION).id
        expected = fingerprint(build_tools(settings, search_id))
        ok = current is not None and (current.metadata or {}).get("definition_sha") == expected
        try:
            project.connections.get(CONNECTION)
            conn_ok = True
        except ResourceNotFoundError:
            conn_ok = False
        print(f"{'PASS' if ok else 'FAIL'} toolbox {TOOLBOX} "
              f"v{getattr(current, 'version', '-')} matches {expected}")
        print(f"{'PASS' if conn_ok else 'FAIL'} connection {CONNECTION}")
        return 0 if ok and conn_ok else 1
    made = ensure_gateway_connections(project)
    if made:
        print(f"gateway connections created: {', '.join(made)}")
    version, created = ensure_toolbox(project, settings)
    connection_id = ensure_connection(project, settings)
    print(f"toolbox {TOOLBOX}: {'created' if created else 'unchanged,'} version {version} "
          f"(default)\n  endpoint {toolbox_endpoint(settings)}\n  connection {CONNECTION}: "
          f"{connection_id.rsplit('/', 1)[-1]}")
    return 0


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        getattr(s, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
