"""Structured outputs for the incident agent.

`IncidentTriage` is what the agent returns and what every downstream consumer - the web UI, the
orchestrator, the evaluation harness - validates before use. CLAUDE.md section 1: structured
outputs everywhere, never prose handed to another component.

`TriageFailure` is the typed failure. When the model cannot produce a valid `IncidentTriage` after
one corrective retry, callers receive this instead of an exception or unvalidated text.
"""

from __future__ import annotations

from typing import Literal

from atlas_common.models import Category, Severity
from pydantic import BaseModel, ConfigDict, Field


class Citation(BaseModel):
    """A source the agent relied on. `doc_id` is what makes a claim checkable."""

    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(description="Runbook id (RB-xxxx) or incident id (INC-xxxx-xxxx).")
    title: str
    last_reviewed: str | None = Field(
        description="ISO date the source was last reviewed, or null if unknown."
    )
    relevance: float = Field(ge=0.0, le=1.0, description="How directly this source applies.")


class IncidentTriage(BaseModel):
    """The agent's assessment of one reported incident."""

    model_config = ConfigDict(extra="forbid")

    severity: Severity
    category: Category
    affected_services: list[str] = Field(description="Service ids confirmed by lookup_service.")
    likely_cause: str
    confidence: float = Field(ge=0.0, le=1.0)
    suspected_change_ids: list[str] = Field(
        description="Change ids from get_recent_changes that plausibly caused this. May be empty."
    )
    summary: str
    citations: list[Citation]
    needs_human: bool
    reasoning_notes: str


class TriageFailure(BaseModel):
    """Typed failure: returned, never raised, so no caller ever receives unvalidated output."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["triage_failure"] = "triage_failure"
    reason: Literal["invalid_output", "model_unavailable", "content_filtered", "timeout"]
    detail: str
    attempts: int
