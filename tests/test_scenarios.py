"""The staged live scenarios: their data is where the demo needs it, and the gate can reach each
one's intended answer.

The model drafts the plan, so the live outcome also depends on it - `scripts/probe_scenarios.py`
checks that against Azure. What is deterministic, and therefore tested here, is (1) the seeded data
each scenario stands on and (2) the gate's answer given a plan of the shape the scenario invites.

No Azure, no model.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from atlas_common.graph import blast_radius
from atlas_common.models import Entitlement, Service
from atlas_orchestrator.models import ImpactResult, RemediationPlan, RemediationStep, RunbookRef
from atlas_orchestrator.policy import evaluate

SEED = Path(__file__).resolve().parent.parent / "data" / "seed"
OPS_NOW = datetime(2026, 9, 1, 14, 20, tzinfo=UTC)  # atlas_common.config default ops clock


def _manifest() -> dict:  # type: ignore[type-arg]
    return json.loads((SEED / "manifest.json").read_text(encoding="utf-8"))


def _jsonl(name: str) -> list[dict]:  # type: ignore[type-arg]
    return [json.loads(line) for line in (SEED / name).read_text(encoding="utf-8").splitlines()
            if line.strip()]


CATALOGUE = {s["service_id"]: Service.model_validate(s)
             for s in json.loads((SEED / "services.json").read_text(encoding="utf-8"))}
ENTITLEMENTS = {e["role"]: Entitlement.model_validate(e)
                for e in json.loads((SEED / "entitlements.json").read_text(encoding="utf-8"))}
SCENARIOS = _manifest()["scenarios"]


def test_three_scenarios_are_staged_besides_the_canonical_incident() -> None:
    assert set(SCENARIOS) == {"search-flag", "reporting-ttl", "orders-stale"}


@pytest.mark.parametrize("key", sorted(SCENARIOS))
def test_each_scenario_change_exists_and_precedes_its_incident(key: str) -> None:
    sc = SCENARIOS[key]
    change = next(c for c in _jsonl("changes.jsonl") if c["change_id"] == sc["change_id"])
    assert change["service_id"] == sc["change_service"]
    assert change["deployed_at"] < sc["incident_at"] <= OPS_NOW.isoformat().replace("+00:00", "Z")


@pytest.mark.parametrize("key", sorted(SCENARIOS))
def test_each_scenario_shows_live_symptoms_from_its_incident_minute(key: str) -> None:
    sc = SCENARIOS[key]
    for service_id in sc["services"]:
        windows = sorted((w for w in _jsonl("telemetry.jsonl") if w["service_id"] == service_id
                          and "2026-09-01T13" <= w["window_start"] <= "2026-09-01T14:20"),
                         key=lambda w: w["window_start"])
        before = [w for w in windows if w["window_start"] < sc["incident_at"]]
        after = [w for w in windows if w["window_start"] >= sc["incident_at"]]
        assert before and not any(w["anomaly"] for w in before), (service_id, before)
        assert after and all(w["anomaly"] for w in after), (service_id, after)


def test_the_scenario_runbooks_carry_the_property_each_scenario_is_about() -> None:
    manifest = _manifest()
    assert SCENARIOS["search-flag"]["runbook_id"] in manifest["injection_canaries"]
    assert SCENARIOS["orders-stale"]["runbook_id"] in manifest["stale_runbooks"]
    assert SCENARIOS["reporting-ttl"]["runbook_id"] not in manifest["stale_runbooks"]


def _plan(target: str, *, risk: str, destructive: bool) -> RemediationPlan:
    return RemediationPlan(
        steps=[RemediationStep(order=1, action="Revert the change", target_service=target,
                               command_ref="RB#1", destructive=destructive,
                               expected_result="Errors return to baseline",
                               rollback="Re-apply the change")],
        risk=risk, estimated_minutes=10, justification="From the runbook.",  # type: ignore[arg-type]
    ).with_plan_id("CASE-SCENARIO")


def _impact(service_id: str) -> ImpactResult:
    services = list(CATALOGUE.values())
    return ImpactResult(blast_radius=blast_radius(service_id, services, depth=3).affected_count,
                        affected_service_ids=[service_id])


def _runbook(doc_id: str) -> RunbookRef:
    text = (SEED / "runbooks" / f"{doc_id}.md").read_text(encoding="utf-8")
    reviewed = next(line.split(":", 1)[1].strip() for line in text.splitlines()
                    if line.startswith("last_reviewed:"))
    age = (OPS_NOW.date() - date.fromisoformat(reviewed)).days
    return RunbookRef(doc_id=doc_id, title=doc_id, review_age_days=age)


def test_reporting_ttl_auto_approves_a_low_risk_revert() -> None:
    # Reverting a config change is destructive by the planner's definition; it is reversible and
    # on a staging Tier 4 service, which is exactly the door rule 5 opens.
    decision = evaluate(_plan("svc-reporting", risk="low", destructive=True),
                        catalogue=CATALOGUE, entitlements=ENTITLEMENTS,
                        requester=ENTITLEMENTS["sre"], impact=_impact("svc-reporting"),
                        runbooks=[_runbook("RB-0011")], implicated={"svc-reporting"},
                        now=OPS_NOW)
    assert decision.auto_approved and not decision.approval_required, decision.reasons


def test_orders_stale_needs_a_manager_and_names_the_stale_runbook() -> None:
    decision = evaluate(_plan("svc-orders", risk="medium", destructive=True),
                        catalogue=CATALOGUE, entitlements=ENTITLEMENTS,
                        requester=ENTITLEMENTS["sre"], impact=_impact("svc-orders"),
                        runbooks=[_runbook("RB-0015")], implicated={"svc-orders"}, now=OPS_NOW)
    assert decision.approval_required and not decision.blocked
    assert decision.required_approver_roles[0] == "manager", decision.required_approver_roles
    assert any("RB-0015" in r and "12 months" in r for r in decision.reasons), decision.reasons


def test_search_flag_needs_a_human_but_not_a_director() -> None:
    decision = evaluate(_plan("svc-search", risk="medium", destructive=True),
                        catalogue=CATALOGUE, entitlements=ENTITLEMENTS,
                        requester=ENTITLEMENTS["sre"], impact=_impact("svc-search"),
                        runbooks=[_runbook("RB-0029")], implicated={"svc-search"}, now=OPS_NOW)
    assert decision.approval_required and not decision.blocked
    assert decision.required_approver_roles[0] == "sre", decision.required_approver_roles


def test_the_console_offers_every_scenario_prompt_verbatim() -> None:
    page = (Path(__file__).resolve().parent.parent / "src" / "atlas_web" / "atlas_web" / "static"
            / "index.html").read_text(encoding="utf-8")
    for key, sc in SCENARIOS.items():
        assert f'data-scenario="{key}">{sc["prompt"]}</button>' in page, key
