"""Incident, change and telemetry models."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .types import Category, ChangeStatus, Risk, Severity


class Incident(BaseModel):
    """A historical incident. 500 of these form the knowledge base of past events."""

    model_config = ConfigDict(extra="forbid")

    incident_id: str
    opened_at: datetime
    closed_at: datetime | None
    severity: Severity
    category: Category
    title: str
    description: str
    affected_services: list[str]
    root_cause: str | None
    resolution: str | None
    resolved_by: str
    time_to_detect_minutes: int = Field(ge=0)
    time_to_mitigate_minutes: int = Field(ge=0)
    linked_change_ids: list[str]
    postmortem_url: str | None
    tags: list[str]


class Change(BaseModel):
    """A deployment. The canonical demo incident is caused by one of these."""

    model_config = ConfigDict(extra="forbid")

    change_id: str
    service_id: str
    deployed_at: datetime
    author: str
    summary: str
    risk: Risk
    rollback_available: bool
    commit_sha: str
    status: ChangeStatus


class TelemetryWindow(BaseModel):
    """A pre-computed metrics window.

    Pre-computed rather than queried live so the same demo prompt yields the same numbers every
    time. CLAUDE.md section 6.
    """

    model_config = ConfigDict(extra="forbid")

    service_id: str
    window_start: datetime
    window_minutes: int = Field(gt=0)
    request_rate_rpm: float = Field(ge=0)
    error_rate_pct: float = Field(ge=0, le=100)
    p50_ms: float = Field(ge=0)
    p95_ms: float = Field(ge=0)
    p99_ms: float = Field(ge=0)
    saturation_pct: float = Field(ge=0, le=100)
    slo_burn_rate: float = Field(ge=0)
    anomaly: bool
