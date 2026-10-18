"""The four specialists (plus the handoff front desk), built from one shared runtime.

Narrow is the point. Each specialist gets its own instructions, its own tool subset and its own
output model, and nothing else:

| Agent            | Tools                                              | Output           |
|------------------|----------------------------------------------------|------------------|
| triage_agent     | lookup_service, get_recent_changes, get_telemetry    | TriageResult     |
| knowledge_agent  | search_knowledge, get_incident                       | KnowledgeResult  |
| impact_agent     | compute_blast_radius, get_telemetry, lookup_service  | ImpactResult     |
| planner_agent    | search_knowledge, lookup_service (read-only)         | RemediationPlan  |
| front_desk_agent | none - it hands off (Phase 06 section 3)             | prose            |
| coordinator_agent| none - it routes a whole case through the handoff mesh| prose            |

A tool a specialist does not hold is a tool it cannot misuse: the planner has no way to execute
anything, because no executing tool exists anywhere in ATLAS.

Specialist instructions ship with the package rather than in Cosmos. The Cosmos-backed v0/v1
switch (CLAUDE.md 9.1) exists for the Episode 1 agent, whose instructions are the demo subject;
these are read once per process and a change to them is a deploy, which is honest about what they
are - part of the workflow's shape.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Any

from agent_framework import Agent
from atlas_agent.factory import FUNCTION_INVOCATION_LIMITS, IncidentAgentRuntime
from pydantic import BaseModel

from .models import ImpactResult, KnowledgeResult, RemediationPlan, TriageResult

TRIAGE = "triage_agent"
KNOWLEDGE = "knowledge_agent"
IMPACT = "impact_agent"
PLANNER = "planner_agent"
FRONT_DESK = "front_desk_agent"
COORDINATOR = "coordinator_agent"


@dataclass(frozen=True)
class Specialist:
    name: str
    tools: tuple[str, ...]
    output: type[BaseModel] | None
    description: str


SPECIALISTS: dict[str, Specialist] = {
    TRIAGE: Specialist(
        TRIAGE, ("lookup_service", "get_recent_changes", "get_telemetry"), TriageResult,
        "Classifies severity, category, affected services and the suspected change.",
    ),
    KNOWLEDGE: Specialist(
        KNOWLEDGE, ("search_knowledge", "get_incident"), KnowledgeResult,
        "Finds the applicable runbook and similar past incidents, and flags stale procedures.",
    ),
    IMPACT: Specialist(
        IMPACT, ("compute_blast_radius", "get_telemetry", "lookup_service"), ImpactResult,
        "Measures blast radius, Tier 1 exposure, customer impact and SLO burn.",
    ),
    PLANNER: Specialist(
        PLANNER, ("search_knowledge", "lookup_service"), RemediationPlan,
        "Drafts an ordered remediation plan from runbook steps. Cannot execute anything.",
    ),
    FRONT_DESK: Specialist(
        FRONT_DESK, (), None,
        "Answers follow-up questions on an analysed case, handing off to the specialists.",
    ),
    COORDINATOR: Specialist(
        COORDINATOR, (), None,
        "Routes a new incident through every specialist in turn, then summarises.",
    ),
}


def instructions_text(name: str) -> str:
    return (resources.files("atlas_orchestrator") / "instructions" / f"{name}.md").read_text(
        encoding="utf-8"
    )


class SpecialistFactory:
    """Builds the specialists on the shared `IncidentAgentRuntime` clients.

    One model client, one Cosmos store, one search client for the whole workflow; a specialist is
    only an instruction set plus a tool subset, so building one costs an object allocation.
    """

    def __init__(self, runtime: IncidentAgentRuntime) -> None:
        self._runtime = runtime
        self._now = runtime.settings.ops_now.strftime("%Y-%m-%d %H:%M UTC")

    @property
    def runtime(self) -> IncidentAgentRuntime:
        return self._runtime

    def build(self, name: str, *, extra_tools: Sequence[Any] | None = None,
              extra_instructions: str = "", for_handoff: bool = False) -> Agent:
        """One specialist. `for_handoff` is required by `HandoffBuilder` - see docs/API-NOTES.md."""
        spec = SPECIALISTS[name]
        options: dict[str, Any] = {"store": False}
        if self._runtime.settings.reasoning_effort:
            options["reasoning"] = {"effort": self._runtime.settings.reasoning_effort}
        agent = Agent(
            client=self._runtime.client,
            name=spec.name,
            description=spec.description,
            instructions=instructions_text(spec.name).replace("{now}", self._now)
            + extra_instructions,
            tools=self._tools(spec, extra_tools),
            default_options=options,
            # HandoffBuilder refuses to build unless every participant persists its call history
            # per service call: a handoff short-circuits a tool call, and without this the local
            # history diverges from the service's.
            require_per_service_call_history_persistence=for_handoff,
        )
        return agent

    def _tools(self, spec: Specialist, extra: Sequence[Any] | None) -> list[Any] | None:
        tools: list[Any] = list(self._runtime.tools.as_agent_tools(spec.tools))
        tools += list(extra or [])
        return tools or None

    def build_all(self) -> dict[str, Agent]:
        return {name: self.build(name) for name in SPECIALISTS}


def response_format(name: str) -> type[BaseModel] | None:
    return SPECIALISTS[name].output


__all__ = [
    "COORDINATOR",
    "FRONT_DESK",
    "FUNCTION_INVOCATION_LIMITS",
    "IMPACT",
    "KNOWLEDGE",
    "PLANNER",
    "SPECIALISTS",
    "TRIAGE",
    "Specialist",
    "SpecialistFactory",
    "instructions_text",
    "response_format",
]
