"""The policy gate: whether a plan may run, and who has to say yes.

Pure code. No model call is reachable from this module - it imports no client, no agent and no
runtime, and `tests/test_policy.py` asserts that by construction. This is the line CLAUDE.md
section 1 draws: classification and drafting are agent work, but *authorization is not*, and
nothing here consults the model that wrote the plan.

`RemediationPlan.requires_approval` is the planner's opinion and is ignored. The gate reads the
plan's *steps* against the catalogue, the entitlement and the clock, and its answer wins.

The rules run in order and each one that fires appends to `reasons`, which is what the approval
console and the incident report show. A decision with an empty `reasons` list would be
unexplainable, so `evaluate()` never produces one.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Literal

from atlas_common.models import Entitlement, Service

from .models import ImpactResult, PolicyDecision, RemediationPlan, RunbookRef

# An approval is a statement about a plan at a moment. Thirty minutes later the telemetry has
# moved on, so the approval is stale and the gate asks again (rule 6).
APPROVAL_TTL = timedelta(minutes=30)
# The system's own answer when the window closes unanswered (Phase 10). Only valid once the clock
# says so: before `expires_at` it is refused like any other wrong answer.
EXPIRY_ROLE = "approval-window"
# A runbook nobody has reviewed in a year may still be right, but it may not be actioned without
# a human reading it first (rule 4).
STALE_RUNBOOK_DAYS = 365
# Above this many affected services, a plan is never automatic regardless of tier (rule 5).
AUTO_APPROVE_MAX_BLAST_RADIUS = 2
AUTO_APPROVE_TIERS = frozenset({3, 4})
# Who signs a break-glass change against a frozen service (rule 1b). Named here, not chosen by
# the usual "cheapest sufficient approver" search: breaking a freeze is always the senior call.
BREAK_GLASS_ROLE = "director"


def _tier_of(service_id: str, catalogue: dict[str, Service]) -> int:
    """An unknown service is treated as Tier 1. Not knowing is not a reason to be permissive."""
    service = catalogue.get(service_id)
    return int(service.tier) if service else 1


_NO_ROLLBACK = frozenset({"", "none", "n/a", "na", "-", "not applicable", "no rollback"})


def _has_rollback(rollback: str) -> bool:
    """A rollback the step can actually be undone with, not a placeholder."""
    return rollback.strip().strip(".").lower() not in _NO_ROLLBACK


def _approver_roles(risk: str, tiers: set[int], blast_radius: int,
                    entitlements: dict[str, Entitlement]) -> list[str]:
    """Which seeded roles could approve this, from the entitlement table - never from the model."""
    able = [
        role for role, ent in entitlements.items()
        if risk in ent.may_approve_risk
        and tiers <= set(ent.may_approve_tiers)
        and blast_radius <= ent.max_blast_radius
    ]
    # Least privilege first, so the UI names the cheapest sufficient approver.
    return sorted(able, key=lambda r: (entitlements[r].max_blast_radius, r))


def implicated_services(
    services: Collection[str] = (),
    triage_affected: Collection[str] = (),
    impact_affected: Collection[str] = (),
) -> set[str]:
    """Which services this incident is actually about. Used by rule 1 to tell the two freezes apart.

    Deterministic set union over what intake resolved and what the two analysis dimensions
    observed. Never the planner's opinion: a plan cannot argue its way into break-glass by naming
    a service the incident never touched.
    """
    return {*services, *triage_affected, *impact_affected}


def evaluate(
    plan: RemediationPlan | None,
    *,
    catalogue: dict[str, Service],
    entitlements: dict[str, Entitlement],
    requester: Entitlement | None,
    impact: ImpactResult | None = None,
    runbooks: list[RunbookRef] | None = None,
    implicated: Collection[str] = (),
    now: datetime | None = None,
) -> PolicyDecision:
    """Apply the gate. Deterministic in its inputs: same plan and catalogue, same answer."""
    moment = now or datetime.now(UTC)
    decision = PolicyDecision(plan_id=plan.plan_id if plan else "", evaluated_at=moment)

    if plan is None or not plan.steps:
        decision.reasons.append("there is no plan to approve")
        decision.approval_required = False
        return decision

    targets = sorted({step.target_service for step in plan.steps})
    tiers = {_tier_of(t, catalogue) for t in targets}
    destructive = [s for s in plan.steps if s.destructive]
    blast_radius = impact.blast_radius if impact else 0

    # -- Rule 1: change freeze, in two halves --------------------------------------------------
    #
    # A freeze stops *planned* change. It is not a reason to leave a service broken, and the
    # canonical incident is caused by a deploy to a frozen service - so remediating it is
    # unavoidable. The two cases are genuinely different and the gate treats them differently:
    #
    #   1a. frozen AND NOT implicated in this incident -> blocked outright. A plan reaching for a
    #       frozen service the incident has nothing to do with is the plan being wrong.
    #   1b. frozen AND implicated -> break glass: allowed, but only a director signs it, and the
    #       reason says so in as many words. This is how a real change freeze works.
    #
    # `implicated` comes from intake and the analysis dimensions, never from the plan, so a plan
    # cannot talk its way into break-glass by naming a service the incident never touched.
    frozen_targets = {t for t in targets
                      if destructive and catalogue.get(t) and catalogue[t].change_freeze}
    frozen_unrelated = sorted(frozen_targets - set(implicated))
    break_glass = sorted(frozen_targets & set(implicated))
    if frozen_unrelated:
        decision.blocked = True
        decision.reasons.append(
            f"change freeze: {', '.join(frozen_unrelated)} is frozen and is not implicated in "
            f"this incident; a destructive step against it is refused")

    # -- Rule 2: the requester must have some authority over remediation -----------------------
    #
    # "May not execute" is not the test, and using it was a bug caught by verification against the
    # deployed system: `manager` and `director` are seeded `may_execute=False` because they
    # *authorise* rather than act - separation of duties - so a case raised by a manager was
    # blocked outright and could never be remediated by anyone.
    #
    # The rule protects against a persona with no authority at all driving a destructive change.
    # A persona that may approve has authority even though it does not execute; `readonly`, which
    # may do neither, is exactly what this refuses (and what Episode 4's unauthorized beat uses).
    if requester is None:
        decision.blocked = True
        decision.reasons.append("no entitlement found for the requesting persona")
    elif not requester.may_execute and not requester.may_approve_risk:
        decision.blocked = True
        decision.reasons.append(
            f"role '{requester.role}' may neither execute nor approve remediation")

    if decision.blocked:
        decision.approval_required = False
        decision.auto_approved = False
        return decision

    # -- Rule 3: what forces a human ------------------------------------------------------------
    if break_glass:
        decision.reasons.append(
            f"break glass: {', '.join(break_glass)} is under change freeze but is implicated in "
            f"this incident; a {BREAK_GLASS_ROLE} must approve")
    if plan.risk == "high":
        decision.reasons.append("the plan is high risk")
    tier_12 = sorted({t for t in targets if _tier_of(t, catalogue) in (1, 2)})
    if tier_12:
        decision.reasons.append(
            f"targets a Tier 1 or Tier 2 service: {', '.join(tier_12)}")
    if requester is not None and blast_radius > requester.max_blast_radius:
        decision.reasons.append(
            f"blast radius {blast_radius} exceeds the {requester.max_blast_radius} that role "
            f"'{requester.role}' may act on unsupervised")
    # A destructive step forces a human UNLESS the plan sits wholly inside rule 5's narrow door:
    # low risk, every target a known non-production Tier 3/4 service, and every destructive step
    # carrying a real rollback. That door never opens onto production (rule 5 checks it again), so
    # "no destructive production action is ever auto-approved" holds by construction. Decided
    # 2026-10-01: a reversible change to a staging service is what auto-approval is for.
    reversible_non_prod = bool(destructive) and plan.risk == "low" and all(
        s.target_service in catalogue
        and catalogue[s.target_service].environment != "prod"
        and catalogue[s.target_service].tier in AUTO_APPROVE_TIERS
        and _has_rollback(s.rollback)
        for s in destructive)
    if destructive and not reversible_non_prod:
        decision.reasons.append(
            f"{len(destructive)} of {len(plan.steps)} steps are destructive "
            f"({', '.join(sorted({s.target_service for s in destructive}))})")

    # -- Rule 4: stale knowledge is not actioned silently ---------------------------------------
    stale = sorted({r.doc_id for r in (runbooks or [])
                    if r.stale or (r.review_age_days or 0) > STALE_RUNBOOK_DAYS})
    if stale:
        decision.reasons.append(
            f"cited runbook {', '.join(stale)} has not been reviewed in over 12 months")

    # -- Rule 5: auto-approval is a narrow door -------------------------------------------------
    environments = {catalogue[t].environment for t in targets if t in catalogue}
    auto_eligible = (
        not decision.reasons
        and plan.risk == "low"
        and tiers <= AUTO_APPROVE_TIERS
        and (not destructive or reversible_non_prod)
        and blast_radius <= AUTO_APPROVE_MAX_BLAST_RADIUS
        and environments
        and "prod" not in environments
        and all(t in catalogue for t in targets)
    )
    if auto_eligible:
        decision.auto_approved = True
        decision.approval_required = False
        changes = (f"{len(destructive)} reversible destructive step(s), each with a rollback"
                   if destructive else "no destructive step")
        decision.reasons.append(
            f"auto-approved: low risk, Tier {sorted(tiers)}, {changes}, blast radius "
            f"{blast_radius}, environment {sorted(environments)}")
        return decision

    # -- Rule 6: a human, named by the entitlement table, within a window -----------------------
    decision.approval_required = True
    decision.required_approver_roles = _approver_roles(plan.risk, tiers, blast_radius, entitlements)
    if break_glass:
        # Breaking a freeze is the senior call, never the cheapest sufficient one.
        decision.required_approver_roles = [
            r for r in decision.required_approver_roles if r == BREAK_GLASS_ROLE]
    if not decision.required_approver_roles:
        decision.reasons.append(
            "no seeded role may approve this plan; it must be escalated outside ATLAS")
    decision.expires_at = moment + APPROVAL_TTL
    if not decision.reasons:  # unreachable by construction, but a gate must always explain itself
        decision.reasons.append("approval required by default")
    return decision


def approval_is_valid(
    decision: PolicyDecision,
    *,
    approver_role: str,
    approver_entitlement: Entitlement | None,
    current_plan_id: str,
    now: datetime | None = None,
) -> tuple[bool, str, Literal["ok", "expired", "role", "plan", "blocked"]]:
    """Is this approval, by this person, for this plan, still good?

    Returns (ok, reason, code). The code is what the API turns into a status, so the three ways to
    be refused stay distinguishable in logs and in the audit trail.
    """
    moment = now or datetime.now(UTC)
    if decision.blocked:
        return False, "policy blocked this plan; it cannot be approved", "blocked"
    if decision.plan_id and current_plan_id and decision.plan_id != current_plan_id:
        return False, (
            f"this approval is for plan {decision.plan_id}, but the case now holds plan "
            f"{current_plan_id}; approve the current plan"
        ), "plan"
    if decision.expires_at is not None and moment > decision.expires_at:
        return False, (
            f"the approval window closed at {decision.expires_at.isoformat()}; "
            f"re-run the gate and approve again"
        ), "expired"
    if approver_entitlement is None:
        return False, f"no entitlement found for role '{approver_role}'", "role"
    if decision.required_approver_roles and approver_role not in decision.required_approver_roles:
        return False, (
            f"role '{approver_role}' may not approve this plan; "
            f"required: {' or '.join(decision.required_approver_roles)}"
        ), "role"
    return True, f"approved by role '{approver_role}'", "ok"


__all__ = [
    "APPROVAL_TTL",
    "AUTO_APPROVE_MAX_BLAST_RADIUS",
    "AUTO_APPROVE_TIERS",
    "BREAK_GLASS_ROLE",
    "EXPIRY_ROLE",
    "STALE_RUNBOOK_DAYS",
    "PolicyDecision",
    "approval_is_valid",
    "evaluate",
    "implicated_services",
]
