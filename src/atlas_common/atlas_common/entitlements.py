"""Who may do what. Plain code, never a model decision (CLAUDE.md section 1).

Two independent sources must BOTH allow an action:

1. **The Entra app role** on the signed-in user (`Atlas.Engineer`, `Atlas.Approver`,
   `Atlas.ReadOnly`), issued by Entra and delivered by Container Apps built-in auth.
2. **The persona's entitlement** from the seeded `entitlements` container: which risk levels and
   service tiers that role may approve, the largest blast radius it may approve, and whether it may
   execute.

A persona can be *narrowed* for a session ("act as readonly" for the Episode 4 unauthorized beat),
never widened. Every check here is a pure function of its inputs, so the API refuses exactly what
the UI hides, and the tests pin both.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from .models import Entitlement, Person

APP_ROLE_ENGINEER = "Atlas.Engineer"
APP_ROLE_APPROVER = "Atlas.Approver"
APP_ROLE_READONLY = "Atlas.ReadOnly"
APP_ROLES = (APP_ROLE_ENGINEER, APP_ROLE_APPROVER, APP_ROLE_READONLY)

# Persona roles ordered by privilege. Narrowing moves left, never right.
PERSONA_ORDER = ("readonly", "engineer", "sre", "manager", "director")

Action = Literal["chat", "approve", "execute"]


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def persona_rank(role: str) -> int:
    return PERSONA_ORDER.index(role) if role in PERSONA_ORDER else 0


def default_persona_role(app_roles: Iterable[str]) -> str:
    """The persona a signed-in user acts as when no explicit binding exists.

    Approvers act as a manager (may approve Tier 1, high risk), engineers as an SRE, anyone else
    as readonly. A user with no ATLAS app role at all gets readonly: least privilege by default.
    """
    roles = set(app_roles)
    if APP_ROLE_APPROVER in roles:
        return "manager"
    if APP_ROLE_ENGINEER in roles:
        return "sre"
    return "readonly"


def narrow(current: str, requested: str | None) -> str:
    """Apply a session's "act as" request. Only ever lowers privilege."""
    if not requested or requested not in PERSONA_ORDER:
        return current
    return requested if persona_rank(requested) <= persona_rank(current) else current


def pick_persona(people: Iterable[Person], role: str) -> Person | None:
    """Deterministic canonical persona for a role: the lowest user_id holding it."""
    candidates = sorted((p for p in people if p.role == role), key=lambda p: p.user_id)
    return candidates[0] if candidates else None


def can_chat(app_roles: Iterable[str]) -> Decision:
    roles = set(app_roles)
    if roles & set(APP_ROLES):
        return Decision(True, "signed in with an ATLAS role")
    return Decision(False, "no ATLAS app role assigned to this account")


def can_approve(
    app_roles: Iterable[str],
    entitlement: Entitlement | None,
    *,
    risk: str,
    tier: int,
    blast_radius: int,
) -> Decision:
    """May this user approve a plan of this risk, on a service of this tier, with this reach?"""
    if APP_ROLE_APPROVER not in set(app_roles):
        return Decision(False, "the Atlas.Approver app role is required")
    if entitlement is None:
        return Decision(False, "no entitlement found for this persona")
    if risk not in entitlement.may_approve_risk:
        return Decision(False, f"role '{entitlement.role}' may not approve {risk}-risk plans")
    if tier not in entitlement.may_approve_tiers:
        return Decision(False, f"role '{entitlement.role}' may not approve Tier {tier} services")
    if blast_radius > entitlement.max_blast_radius:
        return Decision(
            False,
            f"blast radius {blast_radius} exceeds the {entitlement.max_blast_radius} "
            f"that role '{entitlement.role}' may approve",
        )
    return Decision(True, f"role '{entitlement.role}' may approve this plan")
