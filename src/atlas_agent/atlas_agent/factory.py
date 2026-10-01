"""The single definition of the ATLAS incident agent.

`build_incident_agent` is imported by the web app (Phase 04), the Foundry hosted agent (Phase 05),
the orchestrator (Phase 06) and the tests. One definition, several hosting models - that is the
Episode 1 payoff, and it only holds if nothing else ever constructs this agent.

`triage` wraps a run with the structured-output contract: request `IncidentTriage`, validate it,
retry once with the validation error if it does not conform, and otherwise return a typed
`TriageFailure`. No caller ever receives unvalidated model output (CLAUDE.md section 1).

`IncidentAgentRuntime` owns the shared, expensive pieces - credential, model client, Cosmos and
Search clients - so a per-request agent (needed for per-request instruction versions, CLAUDE.md 9.1)
costs nothing but an object allocation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Self

from agent_framework import Agent, AgentResponse, AgentResponseUpdate, AgentSession, FunctionTool
from agent_framework.exceptions import (
    AgentContentFilterException,
    ChatClientContentFilterException,
    ChatClientException,
)
from agent_framework.foundry import FoundryChatClient
from atlas_common.auth import get_async_credential
from atlas_common.config import Settings
from atlas_common.ops_client import OpsApiClient
from atlas_common.repositories import CosmosStore, KnowledgeSearch
from atlas_common.secrets import read_secret
from atlas_common.telemetry import event
from atlas_common.tool_catalog import ToolCatalog
from azure.core.credentials_async import AsyncTokenCredential
from openai import APIStatusError
from pydantic import ValidationError

from .instructions import AGENT_NAME, InstructionStore, LoadedInstructions
from .schemas import IncidentTriage, TriageFailure
from .session import CosmosSessionStore
from .tools import AtlasTools

# Tighter than the framework defaults (40 iterations, no call or time limit). The canonical triage
# needs about eight tool calls; these bounds turn a runaway loop into a prompt, finite answer
# instead of a stalled demo.
FUNCTION_INVOCATION_LIMITS: dict[str, Any] = {
    "max_iterations": 10,
    "max_function_calls": 24,
    "max_duration_seconds": 90.0,
    "include_detailed_errors": False,
}
TRIAGE_TIMEOUT_S = 150.0


def build_model_client(settings: Settings, credential: AsyncTokenCredential) -> FoundryChatClient:
    """The Foundry chat client, with bounded automatic tool invocation."""
    client = FoundryChatClient(
        project_endpoint=settings.foundry_project_endpoint,
        model=settings.model_main,
        credential=credential,
    )
    client.function_invocation_configuration.update(FUNCTION_INVOCATION_LIMITS)
    return client


def build_incident_agent(
    client: FoundryChatClient,
    instructions: str,
    tools: Sequence[FunctionTool],
    reasoning_effort: str = "",
) -> Agent:
    """Construct the incident agent. Cheap: no I/O, safe to call per request.

    `store=False` keeps conversation history in the session, persisted to Cosmos, rather than in
    Foundry's response store. See `atlas_agent.session` for why.
    """
    return Agent(
        client=client,
        name=AGENT_NAME,
        description="Contoso Cloud Operations incident triage agent.",
        instructions=instructions,
        tools=list(tools),
        default_options=_default_options(reasoning_effort),
    )


def _default_options(reasoning_effort: str) -> Any:
    options: dict[str, Any] = {"store": False}
    if reasoning_effort:
        options["reasoning"] = {"effort": reasoning_effort}
    return options


@dataclass
class TriageResult:
    """The outcome of one triage, with what the demo and the evals need to see alongside it."""

    output: IncidentTriage | TriageFailure
    attempts: int
    tool_calls: list[str] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None


def _tool_calls(response: AgentResponse) -> list[str]:
    calls: list[str] = []
    for message in response.messages:
        for content in message.contents:
            if content.type == "function_call" and content.name:
                calls.append(content.name)
    return calls


def _usage(response: AgentResponse) -> tuple[int | None, int | None]:
    usage = response.usage_details
    if not usage:
        return None, None
    return usage.get("input_token_count"), usage.get("output_token_count")


OnUpdate = Callable[[AgentResponseUpdate], None]

FailureReason = Literal["content_filtered", "model_unavailable", "throttled"]


def model_failure_reason(exc: BaseException) -> FailureReason | None:
    """Classify an exception from a model call, or None if it is not a model-side failure.

    Walks the cause chain because agent-framework-openai 1.14.3 cannot construct its own content
    filter exception for Foundry: the service answers with inner code `ContentFiltered`, the
    framework's `ContentFilterCodes` enum only knows `ResponsibleAIPolicyViolation`, and the
    constructor raises `ValueError` with the real `BadRequestError` as its context. Without this,
    a blocked prompt shows the audience a stack trace instead of a refusal. docs/API-NOTES.md 10.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (AgentContentFilterException, ChatClientContentFilterException)):
            event("guardrail.block", source="framework", kind=type(current).__name__)
            return "content_filtered"
        if isinstance(current, APIStatusError):
            body = current.body if isinstance(current.body, dict) else {}
            if current.code == "content_filter" or body.get("code") == "content_filter":
                event("guardrail.block", source="model", kind="content_filter")
                return "content_filtered"
            if current.status_code == 429:
                return "throttled"
            return "model_unavailable"
        if isinstance(current, ChatClientException):
            return "model_unavailable"
        current = current.__cause__ or current.__context__
    return None


def retry_after_seconds(exc: BaseException) -> int | None:
    """The `Retry-After` a throttled model call asked for, from anywhere in the cause chain."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, APIStatusError) and current.status_code == 429:
            header = current.response.headers.get("retry-after")
            try:
                return max(1, int(float(header))) if header else 10
            except ValueError:
                return 10
        current = current.__cause__ or current.__context__
    return None


async def _run(
    agent: Agent,
    prompt: str,
    session: AgentSession,
    on_update: OnUpdate | None,
    middleware: Sequence[Any] | None = None,
) -> AgentResponse:
    """One structured run, streamed through `on_update` when a caller wants live events."""
    options: Any = {"response_format": IncidentTriage}
    mw: Any = list(middleware) if middleware else None
    if on_update is None:
        return await agent.run(prompt, session=session, options=options, middleware=mw)
    stream = agent.run(prompt, session=session, options=options, stream=True, middleware=mw)
    async for update in stream:
        on_update(update)
    return await stream.get_final_response()


async def triage(
    agent: Agent,
    report: str,
    session: AgentSession,
    on_update: OnUpdate | None = None,
    middleware: Sequence[Any] | None = None,
) -> TriageResult:
    """Run one structured triage: validate, retry once on invalid output, else typed failure.

    Pass `on_update` to receive streamed events (tool calls, results, text deltas) as they happen;
    the validated result is identical either way.
    """
    prompt = report
    tool_calls: list[str] = []
    tokens_in = tokens_out = 0
    for attempt in (1, 2):
        try:
            async with asyncio.timeout(TRIAGE_TIMEOUT_S):
                response = await _run(agent, prompt, session, on_update, middleware)
        except TimeoutError:
            return TriageResult(
                TriageFailure(reason="timeout", detail="triage exceeded its time budget",
                              attempts=attempt),
                attempt, tool_calls,
            )
        except Exception as exc:
            reason = model_failure_reason(exc)
            if reason is None:
                raise
            return TriageResult(
                TriageFailure(reason=reason, detail=type(exc).__name__, attempts=attempt,
                              retry_after=retry_after_seconds(exc)),
                attempt, tool_calls,
            )

        tool_calls += _tool_calls(response)
        used_in, used_out = _usage(response)
        tokens_in += used_in or 0
        tokens_out += used_out or 0
        try:
            # raises ValidationError on schema failure (API-NOTES 3)
            value: IncidentTriage | None = response.value
        except ValidationError as exc:
            if attempt == 2:
                return TriageResult(
                    TriageFailure(reason="invalid_output", detail=str(exc)[:500], attempts=2),
                    2, tool_calls, tokens_in, tokens_out,
                )
            prompt = (
                "Your previous answer did not match the required IncidentTriage schema. "
                f"Validation errors: {exc}. Return a corrected IncidentTriage object only."
            )
            continue
        if value is None:
            if attempt == 2:
                return TriageResult(
                    TriageFailure(reason="invalid_output", detail="empty structured output",
                                  attempts=2),
                    2, tool_calls, tokens_in, tokens_out,
                )
            prompt = "Return the IncidentTriage object for this incident."
            continue
        return TriageResult(value, attempt, tool_calls, tokens_in, tokens_out)

    return TriageResult(  # pragma: no cover - both attempts return above
        TriageFailure(reason="invalid_output", detail="no attempt completed", attempts=2),
        2, tool_calls, tokens_in, tokens_out,
    )


ANSWER_TIMEOUT_S = 90.0

# What the audience reads when a prose turn fails. Never a stack trace (CLAUDE.md section 6).
DEGRADED_MESSAGES: dict[str, str] = {
    "content_filtered": "I can't help with that request. It was blocked by the content safety "
    "policy. I can help triage an incident instead.",
    "model_unavailable": "I could not reach the language model. Please try again in a moment.",
    "timeout": "That took longer than my time budget, so I stopped. Try a narrower question.",
    "throttled": "The model is busy right now. I will be able to answer again shortly.",
}


@dataclass
class ChatTurn:
    """A prose (follow-up) turn. `failure` is set, and `text` is the degraded message, on error."""

    text: str
    tool_calls: list[str] = field(default_factory=list)
    failure: str | None = None
    retry_after: int | None = None


async def answer(
    agent: Agent,
    question: str,
    session: AgentSession,
    on_update: OnUpdate | None = None,
    middleware: Sequence[Any] | None = None,
) -> ChatTurn:
    """One free-text turn in an existing conversation, with the same failure contract as triage."""
    try:
        async with asyncio.timeout(ANSWER_TIMEOUT_S):
            mw: Any = list(middleware) if middleware else None
            if on_update is None:
                response = await agent.run(question, session=session, middleware=mw)
            else:
                stream = agent.run(question, session=session, stream=True, middleware=mw)
                async for update in stream:
                    on_update(update)
                response = await stream.get_final_response()
    except TimeoutError:
        return ChatTurn(DEGRADED_MESSAGES["timeout"], failure="timeout")
    except Exception as exc:
        reason = model_failure_reason(exc)
        if reason is None:
            raise
        return ChatTurn(DEGRADED_MESSAGES[reason], failure=reason,
                        retry_after=retry_after_seconds(exc))
    return ChatTurn(response.text, _tool_calls(response))


class IncidentAgentRuntime:
    """Shared clients for building and running the incident agent. Use as an async context."""

    def __init__(self, settings: Settings, credential: AsyncTokenCredential | None = None) -> None:
        self.settings = settings
        self._owns_credential = credential is None
        self.credential = credential or get_async_credential()
        self.store = CosmosStore(settings, self.credential)
        self.knowledge = KnowledgeSearch(settings, self.credential)
        self.client = build_model_client(settings, self.credential)
        # One value decides whether the tools read Cosmos directly or call the operations API
        # (PHASE-08 section C). Since Phase 09 that is the APIM gateway: a token for the gateway
        # audience plus the subscription key, read from Key Vault by URI on first use.
        self.ops: OpsApiClient | None = None
        if settings.ops_api_url:
            key_uri = settings.apim_key_secret_uri
            self.ops = OpsApiClient(
                settings.ops_api_url, self.credential, scope=settings.ops_api_scope,
                key_provider=(lambda: read_secret(key_uri, self.credential)) if key_uri else None)
        # Phase 10: the gateway's workflows API (Logic Apps), same identity and key as the tools.
        self.workflows: OpsApiClient | None = None
        if settings.workflows_url:
            key_uri = settings.apim_key_secret_uri
            self.workflows = OpsApiClient(
                settings.workflows_url, self.credential, scope=settings.ops_api_scope,
                key_provider=(lambda: read_secret(key_uri, self.credential)) if key_uri else None,
                timeout_s=30.0)
        # Phase 09: one tool description in APIM, every agent surface (atlas_common.tool_catalog).
        self.catalog: ToolCatalog | None = (
            ToolCatalog(settings.tool_catalog_url, self.credential, settings.ops_api_scope)
            if settings.tool_catalog_url else None)
        self.tools = AtlasTools(settings, self.store, self.knowledge, ops=self.ops,
                                catalog=self.catalog)
        self.instructions = InstructionStore(
            self.store, default_version=settings.instructions_version
        )
        self.sessions = CosmosSessionStore(self.store)

    async def agent(self, version: str | None = None) -> tuple[Agent, LoadedInstructions]:
        """A per-request agent bound to the requested (or active) instruction version."""
        loaded = await self.instructions.load(version)
        await self.refresh_tool_catalog()
        now = self.settings.ops_now.strftime("%Y-%m-%d %H:%M UTC")
        agent = build_incident_agent(
            self.client,
            loaded.render(now=now),
            self.tools.as_agent_tools(),
            reasoning_effort=self.settings.reasoning_effort,
        )
        return agent, loaded

    async def refresh_tool_catalog(self) -> None:
        """Pick up tool descriptions changed in APIM. Cheap when fresh; never raises."""
        if self.catalog is not None:
            await self.catalog.refresh_if_stale()

    async def close(self) -> None:
        if self.ops is not None:
            await self.ops.close()
        if self.workflows is not None:
            await self.workflows.close()
        await self.knowledge.close()
        await self.store.close()
        if self._owns_credential:
            await self.credential.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
