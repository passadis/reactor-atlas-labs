"""Service catalogue (CMDB) models."""

from pydantic import BaseModel, ConfigDict, Field

from .types import Environment, Tier


class SLO(BaseModel):
    """Service level objective. Drives the impact agent's error-budget maths."""

    model_config = ConfigDict(extra="forbid")

    availability_target: float = Field(ge=0.0, le=1.0)
    latency_p95_ms: int = Field(gt=0)
    error_budget_minutes_month: int = Field(ge=0)


class Service(BaseModel):
    """One entry in the Contoso service catalogue."""

    model_config = ConfigDict(extra="forbid")

    service_id: str
    name: str
    tier: Tier
    owner_team: str
    on_call_rotation: str
    environment: Environment
    region: list[str]
    depends_on: list[str]
    slo: SLO
    runbook_ids: list[str]
    # Three services are frozen. The policy gate BLOCKS destructive plans against them, which is
    # what makes the Episode 2 refusal path real rather than staged.
    change_freeze: bool
    repository: str


class BlastRadius(BaseModel):
    """Result of a deterministic dependency walk.

    Computed by `atlas_common.graph.blast_radius`, never by a model. This distinction is the
    Episode 2 thesis in one object: the agent explains the number, code produces it.
    """

    model_config = ConfigDict(extra="forbid")

    service_id: str
    depth: int
    # Services that depend on this one, transitively. The direction that matters for impact.
    affected_service_ids: list[str]
    affected_count: int
    tier1_affected: list[str]
    max_depth_reached: int
    paths: list[list[str]]
