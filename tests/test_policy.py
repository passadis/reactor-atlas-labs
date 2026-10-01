"""Phase 07: the policy gate and the case state machine, exhaustively.

This is the module the whole approval story rests on, so it is tested three ways:

1. Table-driven over every rule, including the order they fire in.
2. A property test over 1000 generated plans asserting the one invariant that must never break:
   **no production action is ever auto-approved, and nothing irreversible is.**
3. A structural test that the module cannot reach a model at all.

No Azure, no model, no network.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from atlas_common.models import SLO, Entitlement, Service
from atlas_orchestrator.models import (
    ALLOWED_TRANSITIONS,
    CaseStatus,
    IllegalTransitionError,
    ImpactResult,
    IncidentCase,
    RemediationPlan,
    RemediationStep,
    RunbookRef,
)
from atlas_orchestrator.policy import (
    APPROVAL_TTL,
    PolicyDecision,
    approval_is_valid,
    evaluate,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def service(service_id: str, tier: int, *, freeze: bool = False,
            environment: str = "prod") -> Service:
    return Service(
        service_id=service_id, name=service_id, tier=tier,  # type: ignore[arg-type]
        owner_team="team-checkout", on_call_rotation="rota-a",
        environment=environment,  # type: ignore[arg-type]
        region=["eu-west"], depends_on=[],
        slo=SLO(availability_target=0.999, latency_p95_ms=250,
                error_budget_minutes_month=43),
        runbook_ids=["RB-0042"], change_freeze=freeze, repository="contoso/checkout",
    )


CATALOGUE = {
    "svc-checkout-api": service("svc-checkout-api", 1),
    "svc-payments": service("svc-payments", 1),
    "svc-reporting": service("svc-reporting", 3),
    "svc-sandbox": service("svc-sandbox", 4, environment="staging"),
    "svc-frozen": service("svc-frozen", 2, freeze=True),
    "svc-frozen-staging": service("svc-frozen-staging", 4, freeze=True, environment="staging"),
}

ENTITLEMENTS = {
    "readonly": Entitlement(role="readonly", may_approve_risk=[], may_approve_tiers=[],
                            may_execute=False, max_blast_radius=0),
    "engineer": Entitlement(role="engineer", may_approve_risk=["low"], may_approve_tiers=[3, 4],
                            may_execute=True, max_blast_radius=2),
    "sre": Entitlement(role="sre", may_approve_risk=["low", "medium"],
                       may_approve_tiers=[2, 3, 4], may_execute=True, max_blast_radius=5),
    # manager and director approve but never execute: separation of duties, as seeded.
    "manager": Entitlement(role="manager", may_approve_risk=["low", "medium", "high"],
                           may_approve_tiers=[1, 2, 3, 4], may_execute=False, max_blast_radius=15),
    "director": Entitlement(role="director", may_approve_risk=["low", "medium", "high"],
                            may_approve_tiers=[1, 2, 3, 4], may_execute=False,
                            max_blast_radius=25),
}


def step(target: str = "svc-reporting", *, destructive: bool = False,
         order: int = 1, rollback: str = "Re-enable the flag") -> RemediationStep:
    return RemediationStep(
        order=order, action="Disable the offending feature flag", target_service=target,
        command_ref="RB-0042#3", destructive=destructive,
        expected_result="Latency returns to baseline", rollback=rollback,
    )


def plan(*steps: RemediationStep, risk: str = "low") -> RemediationPlan:
    return RemediationPlan(
        steps=list(steps) or [step()], risk=risk,  # type: ignore[arg-type]
        estimated_minutes=10, justification="From RB-0042.",
    ).with_plan_id("CASE-TEST0001")


def gate(p: RemediationPlan | None, *, requester: str = "sre", blast_radius: int = 1,
         runbooks: list[RunbookRef] | None = None,
         implicated: tuple[str, ...] = ()) -> PolicyDecision:
    return evaluate(
        p, catalogue=CATALOGUE, entitlements=ENTITLEMENTS,
        requester=ENTITLEMENTS.get(requester),
        impact=ImpactResult(blast_radius=blast_radius), runbooks=runbooks,
        implicated=implicated, now=NOW,
    )


# ------------------------------------------------------------------- rule 1: freeze --


def test_a_destructive_step_against_a_frozen_service_is_blocked() -> None:
    decision = gate(plan(step("svc-frozen", destructive=True), risk="high"))
    assert decision.blocked is True
    assert decision.approval_required is False and decision.auto_approved is False
    assert any("change freeze" in r for r in decision.reasons)
    assert "svc-frozen" in decision.reasons[0]


def test_a_non_destructive_step_against_a_frozen_service_is_not_blocked() -> None:
    """The freeze stops changes, not investigation. A read-only step is still allowed."""
    decision = gate(plan(step("svc-frozen", destructive=False)))
    assert decision.blocked is False


def test_a_freeze_blocks_even_outside_production() -> None:
    decision = gate(plan(step("svc-frozen-staging", destructive=True)))
    assert decision.blocked is True


# ------------------------------------------------------- rule 1b: break glass --


def test_a_frozen_service_implicated_in_the_incident_breaks_glass_instead_of_blocking() -> None:
    """The canonical incident is caused by a deploy to a frozen service. Remediating it is the job.

    Verified against six stored canonical cases on 2026-09-21: every real plan targets
    svc-payments destructively, so a hard block would leave the flagship incident unfixable.
    """
    decision = gate(plan(step("svc-frozen", destructive=True), risk="high"),
                    implicated=("svc-frozen",))
    assert decision.blocked is False
    assert decision.approval_required is True
    assert any("break glass" in r for r in decision.reasons)
    assert decision.required_approver_roles == ["director"]


def test_break_glass_is_always_the_senior_call() -> None:
    """Even a low-risk, Tier-4 plan needs a director once it touches a frozen service."""
    decision = gate(plan(step("svc-frozen-staging", destructive=True), risk="low"),
                    implicated=("svc-frozen-staging",))
    assert decision.required_approver_roles == ["director"]
    assert decision.auto_approved is False


def test_a_frozen_service_the_incident_never_touched_is_still_blocked() -> None:
    """The refusal path stays real: a plan reaching for an unrelated frozen service is wrong."""
    decision = gate(plan(step("svc-frozen", destructive=True)),
                    implicated=("svc-checkout-api", "svc-payments"))
    assert decision.blocked is True
    assert any("not implicated in this incident" in r for r in decision.reasons)


def test_a_mixed_plan_is_blocked_by_its_unrelated_frozen_target() -> None:
    """One good break-glass step does not license an unrelated one. Blocking wins."""
    decision = gate(
        plan(step("svc-frozen", destructive=True, order=1),
             step("svc-frozen-staging", destructive=True, order=2)),
        implicated=("svc-frozen",))
    assert decision.blocked is True
    assert "svc-frozen-staging" in decision.reasons[0]


def test_implication_comes_from_the_incident_not_the_plan() -> None:
    """A plan cannot talk itself into break-glass by naming a service the incident never touched."""
    decision = gate(plan(step("svc-frozen", destructive=True)), implicated=())
    assert decision.blocked is True


def test_implicated_services_is_the_union_of_what_was_observed() -> None:
    from atlas_orchestrator.policy import implicated_services

    assert implicated_services(
        services=["svc-checkout-api"],
        triage_affected=["svc-checkout-api", "svc-payments"],
        impact_affected=["svc-payments", "svc-orders"],
    ) == {"svc-checkout-api", "svc-payments", "svc-orders"}
    assert implicated_services() == set()


# ------------------------------------------------------------- rule 2: entitlement --


def test_readonly_may_neither_execute_nor_approve_so_is_blocked() -> None:
    decision = gate(plan(), requester="readonly")
    assert decision.blocked is True
    assert any("neither execute nor approve" in r for r in decision.reasons)


def test_a_persona_that_approves_but_does_not_execute_is_not_blocked() -> None:
    """Managers and directors authorise rather than act, which is authority, not powerlessness.

    Blocking them was a real bug: a case raised by a manager could never be remediated by anyone.
    Caught by verification against the deployed system, not by a unit test.
    """
    assert ENTITLEMENTS["manager"].may_execute is False
    decision = gate(plan(step("svc-checkout-api"), risk="high"), requester="manager")
    assert decision.blocked is False
    assert decision.approval_required is True


def test_an_unknown_persona_is_blocked_rather_than_assumed() -> None:
    decision = gate(plan(), requester="nobody")
    assert decision.blocked is True
    assert any("no entitlement" in r for r in decision.reasons)


def test_blocking_wins_over_auto_approval() -> None:
    """An otherwise perfectly auto-approvable plan is still blocked for a readonly requester."""
    safe = plan(step("svc-sandbox"), risk="low")
    assert gate(safe, requester="engineer", blast_radius=1).auto_approved is True
    assert gate(safe, requester="readonly", blast_radius=1).blocked is True


# --------------------------------------------------------- rule 3: what needs a human --


def test_high_risk_requires_approval() -> None:
    decision = gate(plan(step("svc-reporting"), risk="high"))
    assert decision.approval_required is True and decision.auto_approved is False
    assert "the plan is high risk" in decision.reasons


def test_a_tier_1_target_requires_approval() -> None:
    decision = gate(plan(step("svc-checkout-api")))
    assert decision.approval_required is True
    assert any("Tier 1 or Tier 2" in r for r in decision.reasons)


def test_a_destructive_step_requires_approval() -> None:
    decision = gate(plan(step("svc-reporting", destructive=True)))
    assert decision.approval_required is True
    assert any("destructive" in r for r in decision.reasons)


def test_a_blast_radius_beyond_the_requesters_ceiling_requires_approval() -> None:
    decision = gate(plan(step("svc-reporting")), requester="engineer", blast_radius=9)
    assert decision.approval_required is True
    assert any("blast radius 9 exceeds the 2" in r for r in decision.reasons)


def test_an_unknown_target_service_is_treated_as_tier_1() -> None:
    """Not knowing what a service is, is not a reason to be permissive."""
    decision = gate(plan(step("svc-never-heard-of")))
    assert decision.approval_required is True
    assert any("Tier 1 or Tier 2" in r for r in decision.reasons)


# ------------------------------------------------------------- rule 4: stale runbooks --


def test_a_stale_runbook_requires_approval() -> None:
    decision = gate(plan(step("svc-sandbox")),
                    runbooks=[RunbookRef(doc_id="RB-0099", title="Old", stale=True)])
    assert decision.approval_required is True
    assert any("not been reviewed in over 12 months" in r for r in decision.reasons)


def test_a_runbook_older_than_a_year_counts_as_stale_even_if_unflagged() -> None:
    decision = gate(plan(step("svc-sandbox")),
                    runbooks=[RunbookRef(doc_id="RB-0098", title="Old", review_age_days=400)])
    assert decision.approval_required is True
    assert any("RB-0098" in r for r in decision.reasons)


def test_a_freshly_reviewed_runbook_does_not_force_approval() -> None:
    decision = gate(plan(step("svc-sandbox")),
                    runbooks=[RunbookRef(doc_id="RB-0042", title="Fresh", review_age_days=30)])
    assert decision.auto_approved is True


# ----------------------------------------------------------- rule 5: auto-approval --


def test_the_narrow_auto_approval_door() -> None:
    decision = gate(plan(step("svc-sandbox"), risk="low"), requester="engineer", blast_radius=1)
    assert decision.auto_approved is True
    assert decision.approval_required is False and decision.blocked is False
    assert decision.reasons and "auto-approved" in decision.reasons[0]


def test_a_reversible_change_to_a_staging_service_is_auto_approved() -> None:
    """Decided 2026-10-01: auto-approval exists for exactly this - low risk, non-prod, undoable."""
    decision = gate(plan(step("svc-sandbox", destructive=True), risk="low"),
                    requester="engineer", blast_radius=1)
    assert decision.auto_approved is True, decision.reasons
    assert "1 reversible destructive step(s)" in decision.reasons[0]


@pytest.mark.parametrize("rollback", ["None", "n/a", "", " none. "])
def test_a_destructive_staging_step_without_a_real_rollback_needs_a_human(rollback: str) -> None:
    decision = gate(plan(step("svc-sandbox", destructive=True, rollback=rollback), risk="low"),
                    requester="engineer", blast_radius=1)
    assert decision.auto_approved is False and decision.approval_required is True


def test_a_destructive_step_in_production_is_never_waved_through_by_low_risk() -> None:
    decision = gate(plan(step("svc-reporting", destructive=True), risk="low"),
                    requester="engineer", blast_radius=1)
    assert decision.auto_approved is False
    assert any("destructive" in r for r in decision.reasons)


def test_one_production_step_closes_the_door_for_the_whole_plan() -> None:
    decision = gate(plan(step("svc-sandbox", destructive=True, order=1),
                         step("svc-reporting", order=2), risk="low"),
                    requester="engineer", blast_radius=1)
    assert decision.auto_approved is False


@pytest.mark.parametrize(("target", "risk", "destructive", "blast", "why"), [
    ("svc-sandbox", "medium", False, 1, "risk is not low"),
    ("svc-checkout-api", "low", False, 1, "Tier 1 target"),
    ("svc-sandbox", "low", False, 9, "blast radius too wide"),
    ("svc-reporting", "low", False, 1, "production environment"),
])
def test_auto_approval_is_refused_for(target: str, risk: str, destructive: bool,
                                      blast: int, why: str) -> None:
    decision = gate(plan(step(target, destructive=destructive), risk=risk),
                    requester="sre", blast_radius=blast)
    assert decision.auto_approved is False, why
    assert decision.approval_required is True


def test_an_empty_plan_needs_no_approval_and_says_so() -> None:
    decision = gate(RemediationPlan())
    assert decision.approval_required is False and decision.auto_approved is False
    assert decision.reasons == ["there is no plan to approve"]


def test_no_decision_is_ever_unexplained() -> None:
    """Every path out of the gate carries at least one reason the console can print."""
    for candidate in (
        gate(plan(step("svc-frozen", destructive=True))),
        gate(plan(), requester="readonly"),
        gate(plan(step("svc-checkout-api"), risk="high")),
        gate(plan(step("svc-sandbox")), requester="engineer"),
        gate(RemediationPlan()),
    ):
        assert candidate.reasons, candidate


# ------------------------------------------------------- required approver roles --


def test_the_required_role_comes_from_the_entitlement_table() -> None:
    decision = gate(plan(step("svc-checkout-api"), risk="high"), blast_radius=10)
    # Only manager and director may approve high risk on Tier 1 with a blast radius of 10,
    # and the cheapest sufficient one is named first.
    assert decision.required_approver_roles == ["manager", "director"]


def test_the_cheapest_sufficient_approver_is_named_first() -> None:
    decision = gate(plan(step("svc-reporting", destructive=True), risk="low"), blast_radius=1)
    assert decision.required_approver_roles[0] == "engineer"
    assert "manager" in decision.required_approver_roles


def test_a_plan_no_seeded_role_can_approve_says_so() -> None:
    decision = gate(plan(step("svc-checkout-api"), risk="high"), blast_radius=999)
    assert decision.required_approver_roles == []
    assert any("escalated outside ATLAS" in r for r in decision.reasons)


# ------------------------------------------------------------- approval validity --


def approved_decision(**over: object) -> PolicyDecision:
    base = dict(approval_required=True, plan_id="plan-abc",
                required_approver_roles=["manager"], expires_at=NOW + timedelta(minutes=30))
    base.update(over)
    return PolicyDecision(**base)  # type: ignore[arg-type]


def test_a_valid_approval_is_accepted() -> None:
    ok, reason, code = approval_is_valid(
        approved_decision(), approver_role="manager",
        approver_entitlement=ENTITLEMENTS["manager"], current_plan_id="plan-abc", now=NOW)
    assert ok is True and code == "ok" and "manager" in reason


def test_an_expired_approval_is_refused() -> None:
    ok, reason, code = approval_is_valid(
        approved_decision(), approver_role="manager",
        approver_entitlement=ENTITLEMENTS["manager"], current_plan_id="plan-abc",
        now=NOW + timedelta(minutes=31))
    assert ok is False and code == "expired" and "window closed" in reason


def test_an_approval_for_a_superseded_plan_is_refused() -> None:
    ok, reason, code = approval_is_valid(
        approved_decision(), approver_role="manager",
        approver_entitlement=ENTITLEMENTS["manager"], current_plan_id="plan-xyz", now=NOW)
    assert ok is False and code == "plan"
    assert "plan-abc" in reason and "plan-xyz" in reason


def test_the_wrong_role_is_refused() -> None:
    ok, reason, code = approval_is_valid(
        approved_decision(), approver_role="engineer",
        approver_entitlement=ENTITLEMENTS["engineer"], current_plan_id="plan-abc", now=NOW)
    assert ok is False and code == "role" and "manager" in reason


def test_a_blocked_plan_cannot_be_approved_by_anyone() -> None:
    ok, _reason, code = approval_is_valid(
        approved_decision(blocked=True), approver_role="manager",
        approver_entitlement=ENTITLEMENTS["manager"], current_plan_id="plan-abc", now=NOW)
    assert ok is False and code == "blocked"


def test_a_persona_with_no_entitlement_is_refused() -> None:
    ok, _reason, code = approval_is_valid(
        approved_decision(), approver_role="ghost", approver_entitlement=None,
        current_plan_id="plan-abc", now=NOW)
    assert ok is False and code == "role"


# ------------------------------------------------------------- the invariant --


def test_no_destructive_production_action_is_ever_auto_approved() -> None:
    """1000 generated plans. If this ever fails, the gate is not a gate."""
    rng = random.Random(20260921)  # noqa: S311 - deterministic test data, not security
    targets = list(CATALOGUE)
    risks = ["low", "medium", "high"]
    roles = list(ENTITLEMENTS)
    violations: list[str] = []

    for _ in range(1000):
        chosen = rng.sample(targets, rng.randint(1, 3))
        steps = [step(t, destructive=rng.random() < 0.5, order=i + 1)
                 for i, t in enumerate(chosen)]
        candidate = RemediationPlan(
            steps=steps, risk=rng.choice(risks),  # type: ignore[arg-type]
            estimated_minutes=rng.randint(1, 90), justification="generated",
        ).with_plan_id("CASE-PROP")
        # Half the runs implicate every target, exercising the break-glass branch too.
        implicated = tuple(chosen) if rng.random() < 0.5 else ()
        decision = gate(candidate, requester=rng.choice(roles),
                        blast_radius=rng.randint(0, 25), implicated=implicated)

        if not decision.auto_approved:
            continue
        # The invariant, exactly: an auto-approved plan touches no production service at all, and
        # every destructive step in it can be undone (decided 2026-10-01; before that, no
        # destructive step of any kind could be auto-approved).
        in_prod = any(s.target_service not in CATALOGUE
                      or CATALOGUE[s.target_service].environment == "prod"
                      for s in candidate.steps)
        irreversible = any(s.destructive and not s.rollback.strip() for s in candidate.steps)
        if in_prod or irreversible:
            violations.append(
                f"auto-approved prod={in_prod} irreversible={irreversible} "
                f"risk={candidate.risk} targets={[s.target_service for s in candidate.steps]}")

    assert not violations, violations[:5]


def test_the_gate_reaches_no_model() -> None:
    """Structural: the policy module imports nothing that could call one."""
    import atlas_orchestrator.policy as module

    source = (module.__file__ or "")
    assert source
    text = Path(source).read_text(encoding="utf-8")
    for forbidden in ("agent_framework", "IncidentAgentRuntime", "SpecialistFactory",
                      "openai", "FoundryChatClient", ".run("):
        assert forbidden not in text, f"policy.py must not reference {forbidden}"


# ------------------------------------------------------------ the state machine --


def a_case(status: CaseStatus = "intake") -> IncidentCase:
    return IncidentCase(case_id="CASE-TEST0001", report="Checkout is timing out.", status=status)


@pytest.mark.parametrize(("start", "target"), [
    ("intake", "analysing"),
    ("analysing", "aggregated"),
    ("aggregated", "plan_ready"),
    ("plan_ready", "awaiting_approval"),
    ("awaiting_approval", "approved"),
    ("awaiting_approval", "rejected"),
    ("approved", "executing"),
    ("executing", "completed"),
])
def test_legal_transitions_are_recorded(start: CaseStatus, target: CaseStatus) -> None:
    case = a_case(start)
    case.transition(target, by="policy_gate", rule="test", detail="moving")
    assert case.status == target
    assert case.decisions[-1].rule == "test"


@pytest.mark.parametrize(("start", "target"), [
    ("intake", "approved"),
    ("plan_ready", "executing"),
    ("awaiting_approval", "executing"),
    ("awaiting_approval", "completed"),
    ("rejected", "approved"),
    ("completed", "executing"),
    ("failed", "analysing"),
])
def test_illegal_transitions_raise(start: CaseStatus, target: CaseStatus) -> None:
    case = a_case(start)
    with pytest.raises(IllegalTransitionError):
        case.transition(target, by="test", rule="test", detail="should not be allowed")
    assert case.status == start
    assert not case.decisions


def test_approval_can_never_be_skipped() -> None:
    """The one edge the Episode 2 set piece depends on not existing."""
    assert "executing" not in ALLOWED_TRANSITIONS["awaiting_approval"]
    assert "executing" not in ALLOWED_TRANSITIONS["plan_ready"]
    assert ALLOWED_TRANSITIONS["approved"] == frozenset({"executing", "failed"})


def test_terminal_states_are_terminal() -> None:
    for terminal in ("completed", "rejected", "failed"):
        assert ALLOWED_TRANSITIONS[terminal] == frozenset()


def test_every_status_appears_in_the_machine() -> None:
    from typing import get_args

    assert set(get_args(CaseStatus)) == set(ALLOWED_TRANSITIONS)


def test_failure_is_reachable_from_every_live_status() -> None:
    for status, allowed in ALLOWED_TRANSITIONS.items():
        if allowed:
            assert "failed" in allowed, f"{status} cannot fail"


def test_a_repeated_status_is_not_a_transition() -> None:
    """Re-stating the current status is a no-op, so an idempotent retry does not raise."""
    case = a_case("analysing")
    case.transition("analysing", by="test", rule="retry", detail="same status again")
    assert case.status == "analysing"


def test_policy_ttl_is_thirty_minutes() -> None:
    assert APPROVAL_TTL == timedelta(minutes=30)
