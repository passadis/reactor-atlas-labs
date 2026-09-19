"""Phase 05: the hosted-agent adapter (offline) and surface equivalence (live).

Offline: the adapter builds the ONE agent per request and requests the IncidentTriage schema only
on a first turn. Live: the same canonical report through the Foundry-hosted agent and through the
in-process agent gives structurally equivalent output - same severity, overlapping affected
services and suspected change - with prose allowed to differ.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from agent_framework import Message
from atlas_agent.schemas import IncidentTriage
from atlas_hosted_agent.agent import HostedIncidentAgent, is_first_turn


class FakeLoaded:
    version = "v1"
    source = "cosmos"


class FakeInner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(self, messages: Any, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "response"


class FakeRuntime:
    def __init__(self) -> None:
        self.inner = FakeInner()
        self.built = 0

    async def agent(self, version: str | None = None) -> tuple[FakeInner, FakeLoaded]:
        self.built += 1
        return self.inner, FakeLoaded()


def test_first_turn_detection() -> None:
    assert is_first_turn("checkout is slow")
    assert is_first_turn(None)
    assert is_first_turn([Message(role="user", contents=["checkout is slow"])])
    assert not is_first_turn([
        Message(role="user", contents=["checkout is slow"]),
        Message(role="assistant", contents=["{...}"]),
        Message(role="user", contents=["who owns it?"]),
    ])


async def test_adapter_requests_the_triage_schema_only_on_a_first_turn() -> None:
    runtime = FakeRuntime()
    adapter = HostedIncidentAgent(runtime)  # type: ignore[arg-type]
    await adapter.run([Message(role="user", contents=["checkout is slow"])])
    await adapter.run([
        Message(role="user", contents=["checkout is slow"]),
        Message(role="assistant", contents=["{}"]),
        Message(role="user", contents=["who owns it?"]),
    ])
    first, follow_up = runtime.inner.calls
    assert first["options"] == {"response_format": IncidentTriage}
    assert follow_up["options"] is None
    assert runtime.built == 2, "the agent is built per request (instructions are data)"


def test_adapter_satisfies_the_agent_protocol() -> None:
    from agent_framework import SupportsAgentRun

    assert isinstance(HostedIncidentAgent(FakeRuntime()), SupportsAgentRun)  # type: ignore[arg-type]


CANONICAL = (
    "Checkout is timing out for about 20% of EU customers since 14:05, "
    "started right after the payments deploy."
)


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.skipif(not os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
                    and not os.path.exists(".env.local"), reason="no provisioned environment")
async def test_hosted_and_in_process_agents_are_structurally_equivalent() -> None:
    from atlas_agent.factory import IncidentAgentRuntime, triage
    from atlas_common.auth import get_credential
    from atlas_common.config import Settings, load_local_env
    from azure.ai.projects import AIProjectClient

    load_local_env()
    settings = Settings.from_env()
    project = AIProjectClient(endpoint=settings.foundry_project_endpoint,
                              credential=get_credential())
    hosted = project.get_openai_client(agent_name="atlas-incident-agent").responses.create(
        input=CANONICAL, timeout=240
    )
    hosted_triage = IncidentTriage.model_validate(json.loads(hosted.output_text))

    async with IncidentAgentRuntime(settings) as runtime:
        agent, _ = await runtime.agent()
        local = await triage(agent, CANONICAL, agent.create_session())
    assert isinstance(local.output, IncidentTriage)

    assert hosted_triage.severity == local.output.severity == "Sev2"
    assert set(hosted_triage.affected_services) & set(local.output.affected_services)
    assert "CHG-2026-0873" in hosted_triage.suspected_change_ids
    assert "CHG-2026-0873" in local.output.suspected_change_ids
    assert {c.doc_id for c in hosted_triage.citations} & {c.doc_id for c in local.output.citations}
