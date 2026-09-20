"""The typed state that flows through the workflow, and each specialist's output contract.

Every edge in the graph carries one of these models, never prose and never a dict. That is what
makes the Episode 2 claims inspectable: you can print the state between any two nodes and see
exactly what one agent handed the next.

`IncidentCase` is the case as it accumulates. Phase 07 persists it as a checkpoint and adds the
policy gate and approval; Phase 10 adds dispatch. The `decisions` log is written only by
deterministic code: every entry names the rule that fired and why, so "the system decided" is
always answerable with a line of code rather than a guess about the model.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal

from atlas_agent.schemas import Citation
from atlas_common.models import Severity
from pydantic import BaseModel, ConfigDict, Field

Dimension = Literal["triage", "knowledge", "impact", "plan"]
CaseStatus = Literal[
    "intake", "analysing", "aggregated", "plan_ready", "awaiting_approval", "dispatched", "failed"
]


def _now() -> datetime:
    return datetime.now(UTC)


class Degraded(BaseModel):
    """One analysis dimension is missing. The run continues; the report says so."""

    model_config = ConfigDict(extra="forbid")

    dimension: Dimension
    reason: Literal["timeout", "model_unavailable", "content_filtered", "invalid_output", "error"]
    detail: str = ""
    at: datetime = Field(default_factory=_now)


class Decision(BaseModel):
    """A deterministic decision. Written by executors, never by an agent."""

    model_config = ConfigDict(extra="forbid")

    by: str
    rule: str
    detail: str
    at: datetime = Field(default_factory=_now)


# ------------------------------------------------------------------ specialist outputs --


class TriageResult(BaseModel):
    """`triage_agent`: what is broken, how badly, and what changed just before."""

    model_config = ConfigDict(extra="forbid")

    severity: Severity
    category: Literal[
        "latency", "availability", "data", "security", "capacity", "deployment", "config"
    ]
    affected_services: list[str]
    likely_cause: str
    suspected_change_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    summary: str
    reasoning_notes: str = ""


class RunbookRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: str
    title: str
    review_age_days: int | None = None
    stale: bool = False
    applicable_steps: list[str] = Field(default_factory=list)


class KnowledgeResult(BaseModel):
    """`knowledge_agent`: the documented procedure and what happened last time."""

    model_config = ConfigDict(extra="forbid")

    runbooks: list[RunbookRef] = Field(default_factory=list)
    similar_incidents: list[str] = Field(default_factory=list)
    # Retrieved text is data. If it contains instructions aimed at the agent, say so here and the
    # aggregate executor escalates - the model never acts on it either way.
    embedded_instructions_found: bool = False
    notes: str = ""
    citations: list[Citation] = Field(default_factory=list)


class ImpactResult(BaseModel):
    """`impact_agent`: how far the failure reaches and who feels it."""

    model_config = ConfigDict(extra="forbid")

    # How far the failure could reach: the exact dependency-graph walk, from the tool.
    blast_radius: int = 0
    # What is actually degraded in telemetry - a subset of the blast radius, usually a small one.
    affected_service_ids: list[str] = Field(default_factory=list)
    tier1_affected: list[str] = Field(default_factory=list)
    customer_impact: str = ""
    slo_burn_rate: float | None = None
    severity_hint: Severity = "Sev3"
    notes: str = ""


class RemediationStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: int
    action: str
    target_service: str
    # A runbook step reference (for example "RB-0042#3"), never free-form shell. Phase 10's worker
    # resolves it against the runbook; anything it cannot resolve is refused there, not here.
    command_ref: str
    destructive: bool
    expected_result: str
    rollback: str


class RemediationPlan(BaseModel):
    """`planner_agent`: an ordered plan. It cannot execute, and does not decide approval."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str = ""
    steps: list[RemediationStep] = Field(default_factory=list)
    risk: Literal["low", "medium", "high"] = "high"
    estimated_minutes: int = 0
    # The agent's OPINION. `policy_gate` (Phase 07) is authoritative and may require approval for a
    # plan the agent thought was safe. Never read this field to decide whether to execute.
    requires_approval: bool = True
    justification: str = ""
    citations: list[Citation] = Field(default_factory=list)

    def with_plan_id(self, case_id: str) -> RemediationPlan:
        """Deterministic id: the same case and the same steps always give the same plan_id."""
        payload = json.dumps(
            {"case_id": case_id, "steps": [s.model_dump(mode="json") for s in self.steps]},
            sort_keys=True, separators=(",", ":"),
        )
        return self.model_copy(
            update={"plan_id": hashlib.sha256(payload.encode()).hexdigest()[:16]}
        )


# ---------------------------------------------------------------------- workflow state --


class IncidentCase(BaseModel):
    """The case as it accumulates. One object, typed, inspectable at any point."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    report: str
    status: CaseStatus = "intake"
    services: list[str] = Field(default_factory=list)
    triage: TriageResult | None = None
    knowledge: KnowledgeResult | None = None
    impact: ImpactResult | None = None
    plan: RemediationPlan | None = None
    degraded: list[Degraded] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    mode: str = "graph"
    traceparent: str | None = None
    raised_by: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    def decide(self, by: str, rule: str, detail: str) -> None:
        self.decisions.append(Decision(by=by, rule=rule, detail=detail))
        self.updated_at = _now()

    def degrade(self, dimension: Dimension, reason: str, detail: str) -> None:
        self.degraded.append(Degraded(dimension=dimension, reason=reason, detail=detail))  # type: ignore[arg-type]
        self.updated_at = _now()

    @property
    def missing(self) -> list[str]:
        return sorted({d.dimension for d in self.degraded})


class IncidentReport(BaseModel):
    """`ReportExecutor` output: assembled from state, with no model call."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    headline: str
    severity: Severity
    summary: str
    plan_id: str | None = None
    plan_steps: int = 0
    requires_approval: bool = True
    needs_human: bool = False
    citations: list[Citation] = Field(default_factory=list)
    missing_analysis: list[str] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    case: IncidentCase
