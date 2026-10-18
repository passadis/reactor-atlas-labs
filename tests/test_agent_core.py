"""Phase 03 unit tests: the agent's contracts, with no Azure and no model.

Everything that is plain code is tested here: instruction fallback, session persistence, typed tool
failures, and the structured-output retry policy. What the model does with those contracts is tested
live in test_agent_integration.py and test_guardrails.py.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from agent_framework import AgentSession
from agent_framework.exceptions import AgentContentFilterException, ChatClientException
from atlas_agent.factory import triage
from atlas_agent.instructions import KNOWN_VERSIONS, InstructionStore, packaged_text
from atlas_agent.schemas import IncidentTriage, TriageFailure
from atlas_agent.session import InMemorySessionStore
from atlas_agent.tools import TOOL_NAMES, AtlasTools, ToolFailure
from atlas_common.config import Settings
from atlas_common.repositories import RepositoryUnavailableError
from pydantic import BaseModel, ValidationError

from atlas_agent import factory

SETTINGS = Settings(
    cosmos_endpoint="https://unit.documents.azure.com:443/",
    cosmos_database="atlas",
    search_endpoint="https://unit.search.windows.net",
    foundry_project_endpoint="https://unit.services.ai.azure.com/api/projects/unit",
    foundry_account_name="unit",
    ops_now=datetime(2026, 9, 1, 14, 20, tzinfo=UTC),
)

TRIAGE = IncidentTriage(
    severity="Sev2",
    category="latency",
    affected_services=["svc-checkout-api", "svc-payments"],
    likely_cause="CHG-2026-0873 on svc-payments",
    confidence=0.9,
    suspected_change_ids=["CHG-2026-0873"],
    summary="Checkout degraded after the payments deploy.",
    citations=[],
    needs_human=True,
    reasoning_notes="Sev2 row: p95 865 ms vs 250 ms SLO.",
)

# ------------------------------------------------------------------------------ fakes --


class UnreachableStore:
    """A Cosmos store whose every call fails the way a dead backend does."""

    async def query(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RepositoryUnavailableError("operations database", "ServiceRequestError")

    async def upsert(self, *_args: Any, **_kwargs: Any) -> None:
        raise RepositoryUnavailableError("operations database", "ServiceRequestError")


class HangingStore:
    async def query(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        await asyncio.sleep(30)
        return []


class UnreachableSearch:
    async def search(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RepositoryUnavailableError("knowledge index", "ServiceRequestError")


class _Strict(BaseModel):
    x: int


def _validation_error() -> ValidationError:
    try:
        _Strict.model_validate({"x": "not a number"})
    except ValidationError as exc:
        return exc
    raise AssertionError("unreachable")


@dataclass
class FakeResponse:
    outcome: IncidentTriage | ValidationError | None
    messages: list[Any] = field(default_factory=list)
    usage_details: dict[str, int] = field(
        default_factory=lambda: {"input_token_count": 100, "output_token_count": 10}
    )

    @property
    def value(self) -> IncidentTriage | None:
        if isinstance(self.outcome, ValidationError):
            raise self.outcome
        return self.outcome


class FakeAgent:
    """Plays back scripted outcomes; records the prompts it was sent."""

    def __init__(self, *outcomes: Any, delay: float = 0.0) -> None:
        self._outcomes = list(outcomes)
        self.prompts: list[str] = []
        self._delay = delay

    async def run(self, prompt: str, **_kwargs: Any) -> FakeResponse:
        self.prompts.append(prompt)
        if self._delay:
            await asyncio.sleep(self._delay)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception) and not isinstance(outcome, ValidationError):
            raise outcome
        return FakeResponse(outcome)


# ------------------------------------------------------------------------ instructions --


def test_every_known_version_is_packaged_and_has_the_clock_placeholder() -> None:
    for version in KNOWN_VERSIONS:
        assert packaged_text(version).strip()
    assert "{now}" in packaged_text("v1")


async def test_instructions_fall_back_to_the_packaged_copy_when_cosmos_is_down() -> None:
    store = InstructionStore(UnreachableStore(), default_version="v1")  # type: ignore[arg-type]
    loaded = await store.load()
    assert loaded.version == "v1"
    assert loaded.source == "packaged"
    assert loaded.text == packaged_text("v1")
    assert "2026-09-01 14:20 UTC" in loaded.render(now="2026-09-01 14:20 UTC")


# ----------------------------------------------------------------------------- session --


async def test_session_round_trips_through_its_serialised_form() -> None:
    sessions = InMemorySessionStore()
    session = AgentSession()
    session.state["history"] = {"messages": [{"role": "user", "text": "checkout is slow"}]}
    await sessions.save("s1", session)
    restored = await sessions.load("s1")
    assert restored is not None
    assert restored.session_id == session.session_id
    assert restored.state == session.state
    assert await sessions.expire("s1") is True
    assert await sessions.load("s1") is None


# ------------------------------------------------------------------------------- tools --


def test_tool_surface_is_exactly_the_six_read_only_tools() -> None:
    tools = AtlasTools(SETTINGS, UnreachableStore(), UnreachableSearch())  # type: ignore[arg-type]
    names = {t.name for t in tools.as_agent_tools()}
    assert names == set(TOOL_NAMES)
    forbidden = ("execute", "submit", "rollback", "restart", "approve", "delete", "update")
    assert not [n for n in names if any(word in n for word in forbidden)]


async def test_unreachable_backends_become_typed_failures_not_exceptions() -> None:
    tools = AtlasTools(SETTINGS, UnreachableStore(), UnreachableSearch())  # type: ignore[arg-type]
    results = [
        await tools.lookup_service("svc-payments"),
        await tools.get_incident("INC-2026-0001"),
        await tools.get_recent_changes("svc-payments"),
        await tools.get_telemetry("svc-payments"),
        await tools.search_knowledge("payment timeouts"),
        await tools.compute_blast_radius("svc-payments"),
    ]
    for result in results:
        assert isinstance(result, ToolFailure), result
        assert result.message.startswith("I could not reach the")


async def test_a_hanging_backend_times_out_into_a_typed_failure() -> None:
    tools = AtlasTools(
        SETTINGS, HangingStore(), UnreachableSearch(), timeout_s=0.2  # type: ignore[arg-type]
    )
    result = await tools.lookup_service("svc-payments")
    assert isinstance(result, ToolFailure)
    assert "no response within" in result.message


# ------------------------------------------------------------------------------ triage --


async def test_triage_returns_the_validated_model_first_time() -> None:
    agent = FakeAgent(TRIAGE)
    result = await triage(agent, "checkout slow", AgentSession())  # type: ignore[arg-type]
    assert result.output == TRIAGE
    assert result.attempts == 1
    assert result.input_tokens == 100


async def test_triage_retries_once_with_the_validation_error() -> None:
    agent = FakeAgent(_validation_error(), TRIAGE)
    result = await triage(agent, "checkout slow", AgentSession())  # type: ignore[arg-type]
    assert result.output == TRIAGE
    assert result.attempts == 2
    assert "Validation errors" in agent.prompts[1]
    assert result.input_tokens == 200


async def test_triage_fails_to_a_typed_error_after_two_invalid_outputs() -> None:
    agent = FakeAgent(_validation_error(), _validation_error())
    result = await triage(agent, "checkout slow", AgentSession())  # type: ignore[arg-type]
    assert isinstance(result.output, TriageFailure)
    assert result.output.reason == "invalid_output"
    assert result.output.attempts == 2


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (AgentContentFilterException("blocked"), "content_filtered"),
        (ChatClientException("503"), "model_unavailable"),
    ],
)
async def test_model_side_failures_map_to_typed_reasons(exc: Exception, reason: str) -> None:
    result = await triage(FakeAgent(exc), "x", AgentSession())  # type: ignore[arg-type]
    assert isinstance(result.output, TriageFailure)
    assert result.output.reason == reason


async def test_triage_has_a_hard_time_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(factory, "TRIAGE_TIMEOUT_S", 0.1)
    result = await triage(FakeAgent(TRIAGE, delay=5), "x", AgentSession())  # type: ignore[arg-type]
    assert isinstance(result.output, TriageFailure)
    assert result.output.reason == "timeout"
