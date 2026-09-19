"""Create or update the Foundry prompt agent `atlas-triage-prompt`. Idempotent and version-aware.

    uv run python -m scripts.create_prompt_agent            # create a version only if changed
    uv run python -m scripts.create_prompt_agent --ask "..."  # then call it once through Responses
    uv run python -m scripts.create_prompt_agent --check    # exit non-zero if missing or drifted

Episode 1 hosting shape 2: the same v1 instructions, but the agent lives server-side in Foundry and
has no code of ours behind it - only the knowledge index, through the project's `aisearch`
connection. The operational tools (services, changes, telemetry, blast radius) reach it through the
toolbox in Phase 05, attached server-side (CLAUDE.md 7.5).

Idempotency: the definition is fingerprinted (model, instructions, tool config, RAI policy) into
the version's `metadata.definition_sha`. A re-run whose fingerprint matches the latest version does
nothing, so `create_version` never piles up identical versions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from atlas_agent.instructions import packaged_text
from atlas_common.auth import get_credential
from atlas_common.config import Settings
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AISearchIndexResource,
    AzureAISearchQueryType,
    AzureAISearchTool,
    AzureAISearchToolResource,
    PromptAgentDefinition,
    RaiConfig,
)
from azure.core.exceptions import ResourceNotFoundError

from scripts.demo_agent_local import _console_utf8

PROMPT_AGENT_NAME = "atlas-triage-prompt"
SEARCH_CONNECTION = "aisearch"
ARM = "https://management.azure.com"
INSTRUCTIONS_VERSION = "v1"

# The v1 instructions name six tools. Until the Phase 05 toolbox is attached, this agent has one.
# Saying so in the instructions turns a missing tool into an honest "I cannot check that" instead of
# an invented service record.
PROMPT_AGENT_ADDENDUM = """

## 9. This deployment

You are running as a Foundry prompt agent. Your only tool is the Azure AI Search knowledge
index (runbooks, past incidents and postmortems). You cannot look up services, changes, telemetry
or blast radius in this deployment. When a step in section 2 needs one of those, say which check
you could not perform and lower your confidence; never fill the gap from memory. Answer in prose,
and cite runbook and incident ids from the retrieved documents.
"""


def rai_policy_id(settings: Settings) -> str:
    """The guardrail's full ARM resource ID. A bare name is rejected by the agent API."""
    return (
        f"/subscriptions/{os.environ['AZURE_SUBSCRIPTION_ID']}"
        f"/resourceGroups/{os.environ['AZURE_RESOURCE_GROUP']}"
        f"/providers/Microsoft.CognitiveServices/accounts/{settings.foundry_account_name}"
        f"/raiPolicies/{os.environ.get('ATLAS_RAI_POLICY_NAME', 'atlas-strict')}"
    )


def assert_rai_policy_exists(policy_id: str) -> None:
    """Fail closed. Per the Foundry docs, an agent that names a nonexistent policy can still be
    created and report active while applying no filtering at all."""
    token = get_credential().get_token(f"{ARM}/.default").token
    request = urllib.request.Request(  # noqa: S310 - fixed https ARM host, never user input
        f"{ARM}{policy_id}?api-version=2024-10-01",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            if response.status != 200:
                raise RuntimeError(f"RAI policy lookup returned {response.status}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"RAI policy {policy_id} not found ({exc.code}); refusing to "
                           "create an agent whose guardrail would fail open") from exc


def build_definition(settings: Settings, connection_id: str) -> PromptAgentDefinition:
    now = settings.ops_now.strftime("%Y-%m-%d %H:%M UTC")
    instructions = packaged_text(INSTRUCTIONS_VERSION).replace("{now}", now) + PROMPT_AGENT_ADDENDUM
    return PromptAgentDefinition(
        model=settings.model_main,
        instructions=instructions,
        tools=[
            AzureAISearchTool(
                azure_ai_search=AzureAISearchToolResource(
                    indexes=[
                        AISearchIndexResource(
                            project_connection_id=connection_id,
                            index_name=settings.knowledge_index,
                            # Hybrid needs the index's integrated vectorizer (search_index.py),
                            # which embeds the query as the Search service's own identity.
                            query_type=AzureAISearchQueryType.VECTOR_SEMANTIC_HYBRID,
                            top_k=5,
                        )
                    ]
                )
            )
        ],
        rai_config=RaiConfig(rai_policy_name=rai_policy_id(settings)),
    )


def fingerprint(definition: PromptAgentDefinition) -> str:
    canonical = json.dumps(definition.as_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def latest_version(project: AIProjectClient) -> Any | None:
    try:
        for version in project.agents.list_versions(PROMPT_AGENT_NAME, limit=1, order="desc"):
            return version
    except ResourceNotFoundError:
        return None
    return None


def ensure(project: AIProjectClient, settings: Settings) -> tuple[Any, bool]:
    """Return (latest version, created). Creates a version only when the fingerprint changed."""
    connection_id = project.connections.get(SEARCH_CONNECTION).id
    definition = build_definition(settings, connection_id)
    sha = fingerprint(definition)
    current = latest_version(project)
    if current is not None and (current.metadata or {}).get("definition_sha") == sha:
        return current, False
    assert_rai_policy_exists(rai_policy_id(settings))
    created = project.agents.create_version(
        agent_name=PROMPT_AGENT_NAME,
        definition=definition,
        description="ATLAS incident triage - Foundry prompt agent (Episode 1, hosting shape 2).",
        metadata={
            "definition_sha": sha,
            "instructions_version": INSTRUCTIONS_VERSION,
            "managed_by": "scripts/create_prompt_agent.py",
        },
    )
    return created, True


def ask(project: AIProjectClient, question: str) -> str:
    openai = project.get_openai_client()
    response = openai.responses.create(
        input=question,
        extra_body={"agent_reference": {"name": PROMPT_AGENT_NAME, "type": "agent_reference"}},
        timeout=120,
    )
    # Citation annotations from the Azure AI Search tool. Observed 2026-09-19: they carry the
    # Search endpoint, not a per-document link, so the doc id reaches the model via the title.
    urls: list[str] = []
    for item in response.output:
        for part in getattr(item, "content", None) or []:
            for note in getattr(part, "annotations", None) or []:
                url = getattr(note, "url", None)
                if url and url not in urls:
                    urls.append(url)
    cited = "".join(f"\n  cited: {u}" for u in urls)
    return response.output_text + (f"\n{cited}" if cited else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ask", metavar="QUESTION")
    parser.add_argument("--check", action="store_true", help="verify only, never create")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    project = AIProjectClient(
        endpoint=settings.foundry_project_endpoint, credential=get_credential()
    )
    if args.check:
        connection_id = project.connections.get(SEARCH_CONNECTION).id
        expected = fingerprint(build_definition(settings, connection_id))
        current = latest_version(project)
        if current is None:
            print(f"FAIL {PROMPT_AGENT_NAME} does not exist")
            return 1
        actual = (current.metadata or {}).get("definition_sha")
        if actual != expected:
            print(f"FAIL {PROMPT_AGENT_NAME} v{current.version} drifted ({actual} != {expected})")
            return 1
        print(f"PASS {PROMPT_AGENT_NAME} v{current.version} matches definition {expected}")
        return 0

    version, created = ensure(project, settings)
    verb = "created" if created else "unchanged,"
    print(f"{PROMPT_AGENT_NAME} {verb} version {version.version} "
          f"(definition {version.metadata.get('definition_sha')})")
    if args.ask:
        print(f"\nQ  {args.ask}\n")
        print(ask(project, args.ask))
    return 0


if __name__ == "__main__":
    _console_utf8()
    sys.exit(main())
