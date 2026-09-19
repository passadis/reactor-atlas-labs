"""Create or update the Foundry Toolbox `atlas-tools`, and the connection agents use to reach it.

    uv run python -m scripts.upsert_toolbox           # idempotent: new version only on change
    uv run python -m scripts.upsert_toolbox --check   # exit non-zero if missing or drifted

A Toolbox is a named, versioned bundle of tools behind ONE MCP endpoint. Episode 1 says "tools are
curated once and shared"; Episode 3 adds the APIM-fronted ops MCP server and the Logic Apps tool
to this same toolbox (Phases 09-10) without touching any agent. Today it holds the knowledge index.

Two things this script owns:

1. **The toolbox** (`project.toolboxes.create_version`). Its definition is fingerprinted into the
   version metadata; a re-run with the same definition creates nothing. A new version is promoted
   to the toolbox's default, so consumers on the default endpoint pick it up with no redeploy.
2. **The `atlas-tools-mcp` project connection** (category RemoteTool, user Entra token, audience
   https://ai.azure.com) pointing at the toolbox's default MCP endpoint. A prompt agent reaches a
   toolbox only through such a connection (docs/API-NOTES.md 12). Created with `azd ai connection`
   when missing.

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
)
from azure.core.exceptions import ResourceNotFoundError

TOOLBOX = "atlas-tools"
CONNECTION = "atlas-tools-mcp"
SEARCH_CONNECTION = "aisearch"


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
            description="ATLAS shared tools: knowledge index now; ops MCP and Logic Apps later.",
            tools=tools,
            metadata={"definition_sha": sha, "managed_by": "scripts/upsert_toolbox.py"},
        )
        version, created = str(made.version), True
    if str(project.toolboxes.get(TOOLBOX).default_version) != version:
        project.toolboxes.update(TOOLBOX, default_version=version)
    return version, created


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
