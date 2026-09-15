"""People and entitlements.

`entitlements.json` is the AUTHORIZATION SOURCE OF TRUTH for the deterministic policy gate
(DATA-MODEL.md section 6). The model never decides whether an action is permitted; the policy gate
reads these records and decides in plain code. CLAUDE.md section 1.
"""

from pydantic import BaseModel, ConfigDict, Field

from .types import PersonRole, Risk, Tier


class Person(BaseModel):
    """A Contoso operator. Obviously fictional; the domain is always contoso.example."""

    model_config = ConfigDict(extra="forbid")

    user_id: str
    display_name: str
    team: str
    role: PersonRole
    email: str


class Entitlement(BaseModel):
    """What a role is permitted to approve or execute.

    The `readonly` role exists specifically so Episode 4's `unauthorized` eval category has a
    persona that MUST be refused. Never grant it may_execute.
    """

    model_config = ConfigDict(extra="forbid")

    role: PersonRole
    may_approve_risk: list[Risk]
    may_approve_tiers: list[Tier]
    may_execute: bool
    max_blast_radius: int = Field(ge=0)
