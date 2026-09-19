"""The ATLAS incident agent as a Foundry hosted agent (Responses protocol, port 8088).

This module does not define an agent. It adapts the one definition in `atlas_agent.factory` to
the Foundry agent server, so "Foundry runs it" and "you run it" (the web app) are the same agent
with the same instructions, tools and output contract - the Episode 1 comparison.

Why an adapter rather than handing `ResponsesHostServer` a fixed `Agent`:

- **Instructions are data** (CLAUDE.md 9.1). A fixed `Agent` bakes in whatever instruction text it
  was built with at startup; switching v0/v1 would mean a new version. The adapter builds the agent
  per request through `IncidentAgentRuntime.agent()`, which reads the active version from Cosmos,
  exactly as the web app does.
- **Same contract per turn.** The server passes the whole conversation on each request. A first
  turn (no assistant message yet) is a triage and requests the `IncidentTriage` JSON schema; a
  follow-up is prose. The web app follows the same rule.
- **History is the platform's.** The server replays history as `messages`, so the adapter runs the
  inner agent without a session - no second copy of the conversation.

Identity: the platform gives every hosted agent its own Entra agent identity. The credential is
injectable (`build_server(credential=...)`); Phase 09 swaps the direct Cosmos/Search repositories
for the APIM-fronted ops API, which is then a one-line change here, not a new agent.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Sequence
from typing import Any

from agent_framework import AgentResponse, AgentResponseUpdate, AgentSession, Message
from atlas_agent.factory import IncidentAgentRuntime
from atlas_agent.instructions import AGENT_NAME
from atlas_agent.schemas import IncidentTriage
from atlas_common.config import Settings
from azure.core.credentials_async import AsyncTokenCredential

log = logging.getLogger("atlas.hosted")

DESCRIPTION = "Contoso Cloud Operations incident triage agent (ATLAS), hosted by Foundry."


def is_first_turn(messages: Any) -> bool:
    """True when the conversation has no assistant reply yet: that turn is a triage."""
    if messages is None or isinstance(messages, str):
        return True
    if isinstance(messages, Message):
        return messages.role != "assistant"
    if isinstance(messages, Sequence):
        return not any(getattr(m, "role", None) == "assistant" for m in messages)
    return True


class HostedIncidentAgent:
    """`SupportsAgentRun` adapter over the one ATLAS agent definition."""

    def __init__(self, runtime: IncidentAgentRuntime) -> None:
        self._runtime = runtime
        self.id = AGENT_NAME
        self.name: str | None = AGENT_NAME
        self.description: str | None = DESCRIPTION

    def create_session(self, *, session_id: str | None = None, **_: Any) -> AgentSession:
        return AgentSession(session_id=session_id)

    def get_session(
        self, service_session_id: Any, *, session_id: str | None = None, **_: Any
    ) -> AgentSession:
        return AgentSession(service_session_id=service_session_id, session_id=session_id)

    async def _prepare(self, messages: Any) -> tuple[Any, Any]:
        agent, loaded = await self._runtime.agent()
        first = is_first_turn(messages)
        log.info("hosted turn: instructions=%s (%s) triage=%s", loaded.version, loaded.source,
                 first)
        options: Any = {"response_format": IncidentTriage} if first else None
        return agent, options

    def run(
        self,
        messages: Any = None,
        *,
        stream: bool = False,
        session: AgentSession | None = None,
        **_: Any,
    ) -> Awaitable[AgentResponse[Any]] | AsyncIterator[AgentResponseUpdate]:
        # `session` is deliberately unused: the server replays history in `messages`.
        if stream:
            return self._stream(messages)
        return self._complete(messages)

    async def _complete(self, messages: Any) -> AgentResponse[Any]:
        agent, options = await self._prepare(messages)
        response: AgentResponse[Any] = await agent.run(messages, options=options)
        return response

    async def _stream(self, messages: Any) -> AsyncIterator[AgentResponseUpdate]:
        agent, options = await self._prepare(messages)
        stream = agent.run(messages, stream=True, options=options)
        async for update in stream:
            yield update


def build_server(credential: AsyncTokenCredential | None = None) -> Any:
    """The Foundry agent server around the adapter. Credential injectable for the Phase 09 swap."""
    from agent_framework.observability import enable_instrumentation
    from agent_framework_foundry_hosting import ResponsesHostServer
    from atlas_common.telemetry import capture_content
    from azure.identity.aio import DefaultAzureCredential

    # The agent server exports to the project's Application Insights on its own (it reads the
    # platform-injected connection string). Only Agent Framework's GenAI spans need switching on.
    enable_instrumentation(enable_sensitive_data=capture_content())
    # The platform-managed agent identity is not a user-assigned identity we name, so this is the
    # documented DefaultAzureCredential path for hosted agents, not atlas_common.auth.
    # See docs/API-NOTES.md 12.
    runtime = IncidentAgentRuntime(Settings.from_env(), credential or DefaultAzureCredential())
    return ResponsesHostServer(HostedIncidentAgent(runtime))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    build_server().run()


if __name__ == "__main__":
    main()
