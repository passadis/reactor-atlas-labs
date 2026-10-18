"""The approval console's own logic: who may sign, and answering while the orchestrator is down.

No Azure. The orchestrator re-checks every decision; these tests pin the web's early answer to the
same rules, and the queued-answer path the Episode 2 kill-and-resume beat depends on.
"""

from __future__ import annotations

from typing import Any

import pytest
from atlas_common.entitlements import APP_ROLE_APPROVER, APP_ROLE_DIRECTOR, APP_ROLE_READONLY
from atlas_common.models import Entitlement
from atlas_web.app import may_approve_workflow
from atlas_web.auth import Principal
from atlas_web.personas import Actor
from atlas_web.stores import CaseStore
from azure.core.exceptions import ResourceModifiedError

ENT = {
    "manager": Entitlement(role="manager", may_approve_risk=["low", "medium", "high"],
                           may_approve_tiers=[1, 2, 3, 4], may_execute=False, max_blast_radius=15),
    "director": Entitlement(role="director", may_approve_risk=["low", "medium", "high"],
                            may_approve_tiers=[1, 2, 3, 4], may_execute=False,
                            max_blast_radius=25),
    "readonly": Entitlement(role="readonly", may_approve_risk=[], may_approve_tiers=[],
                            may_execute=False, max_blast_radius=0),
}
APP_ROLE = {"manager": APP_ROLE_APPROVER, "director": APP_ROLE_DIRECTOR,
            "readonly": APP_ROLE_READONLY}


def actor(role: str) -> Actor:
    principal = Principal(email="presenter@contoso.example", name="Presenter",
                          object_id="oid", app_roles=(APP_ROLE[role],), source="easyauth")
    return Actor(principal=principal, person=None, persona_role=role, entitlement=ENT[role],
                 narrowed=False, binding="app-role")


BREAK_GLASS = {"case_id": "CASE-1", "risk": "high", "blast_radius": 10,
               "required_roles": ["director"], "status": "pending"}


def test_a_director_may_sign_break_glass() -> None:
    assert may_approve_workflow(actor("director"), BREAK_GLASS).allowed


def test_a_manager_is_told_up_front_that_break_glass_needs_a_director() -> None:
    """Entitlements alone would say yes (high risk, Tier 1, radius 10 <= 15); the gate said no."""
    decision = may_approve_workflow(actor("manager"), BREAK_GLASS)
    assert not decision.allowed
    assert "director" in decision.reason and "manager" in decision.reason


def test_a_manager_may_sign_what_the_gate_offered_to_managers() -> None:
    row = {**BREAK_GLASS, "required_roles": ["manager", "director"]}
    assert may_approve_workflow(actor("manager"), row).allowed


def test_readonly_may_never_sign() -> None:
    row = {**BREAK_GLASS, "required_roles": ["readonly"]}
    assert not may_approve_workflow(actor("readonly"), row).allowed


# ------------------------------------------------- answering while the orchestrator is down --


class FakeContainer:
    def __init__(self, rows: dict[str, dict[str, Any]], *, race: bool = False) -> None:
        self.rows, self.race = rows, race

    async def replace_item(self, item: str, body: dict[str, Any], *, etag: str,
                           match_condition: Any) -> None:
        if self.race or self.rows[item]["_etag"] != etag:
            raise ResourceModifiedError("etag mismatch")
        self.rows[item] = {**body, "_etag": etag + "'"}


class FakeStore:
    def __init__(self, rows: list[dict[str, Any]], *, race: bool = False) -> None:
        self._c = FakeContainer({r["id"]: r for r in rows}, race=race)

    def container(self, _name: str) -> FakeContainer:
        return self._c

    async def query(self, _container: str, _sql: str, params: list[dict[str, Any]],
                    partition_key: str | None = None) -> list[dict[str, Any]]:
        wanted = params[0]["value"]
        return [dict(r) for r in self._c.rows.values() if r["id"] == wanted]


def row(status: str = "pending") -> dict[str, Any]:
    return {"id": "approval:CASE-1", "case_id": "CASE-1", "kind": "approval", "status": status,
            "required_roles": ["director"], "_etag": "e1"}


ANSWER = {"approved": True, "by": "presenter@contoso.example", "role": "director",
          "comment": "approved during the kill window", "at": "2026-10-04T10:00:00+00:00"}


async def test_an_answer_is_queued_exactly_where_the_startup_sweep_looks() -> None:
    store = FakeStore([row()])
    assert await CaseStore(store).queue_workflow_answer("CASE-1", ANSWER)  # type: ignore[arg-type]
    saved = store._c.rows["approval:CASE-1"]
    # orchestrator.store.answered_while_away selects kind='approval' AND status='answered' and
    # resumes with `answer`'s ApprovalResponse fields.
    assert saved["status"] == "answered" and saved["answer"] == ANSWER
    assert saved["kind"] == "approval" and saved["answered_via"] == "web-queued"


@pytest.mark.parametrize("status", ["answered", "resolved", "abandoned"])
async def test_a_row_that_is_no_longer_pending_is_not_answered_again(status: str) -> None:
    store = FakeStore([row(status)])
    assert not await CaseStore(store).queue_workflow_answer("CASE-1", ANSWER)  # type: ignore[arg-type]
    assert store._c.rows["approval:CASE-1"]["status"] == status


async def test_a_racing_answer_loses_instead_of_overwriting() -> None:
    store = FakeStore([row()], race=True)
    assert not await CaseStore(store).queue_workflow_answer("CASE-1", ANSWER)  # type: ignore[arg-type]
    assert store._c.rows["approval:CASE-1"]["status"] == "pending"


async def test_no_row_means_nothing_is_written() -> None:
    store = FakeStore([])
    assert not await CaseStore(store).queue_workflow_answer("CASE-9", ANSWER)  # type: ignore[arg-type]
