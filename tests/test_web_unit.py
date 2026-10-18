"""Phase 04 unit tests: identity, entitlements and stream translation. No Azure."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any

import atlas_web.auth as auth
import pytest
from atlas_agent.streaming import ActivityTracker, classify_result
from atlas_common.entitlements import (
    APP_ROLE_APPROVER,
    APP_ROLE_ENGINEER,
    APP_ROLE_READONLY,
    can_approve,
    can_chat,
    default_persona_role,
    narrow,
    pick_persona,
)
from atlas_common.models import Entitlement, Person
from atlas_web.auth import parse_client_principal
from fastapi import HTTPException

MANAGER = Entitlement(role="manager", may_approve_risk=["low", "medium", "high"],
                      may_approve_tiers=[1, 2, 3, 4], may_execute=False, max_blast_radius=15)
SRE = Entitlement(role="sre", may_approve_risk=["low", "medium"], may_approve_tiers=[2, 3, 4],
                  may_execute=True, max_blast_radius=8)
READONLY = Entitlement(role="readonly", may_approve_risk=[], may_approve_tiers=[],
                       may_execute=False, max_blast_radius=0)


def _header(claims: list[tuple[str, str]]) -> str:
    body = {"auth_typ": "aad", "claims": [{"typ": t, "val": v} for t, v in claims]}
    return base64.b64encode(json.dumps(body).encode()).decode()


# ----------------------------------------------------------------------------- principal --


def test_easy_auth_principal_is_parsed_with_roles_and_email() -> None:
    p = parse_client_principal(_header([
        ("name", "Kosta P"),
        ("preferred_username", "Kosta@Example.com"),
        ("oid", "0000-1111"),
        ("roles", APP_ROLE_APPROVER),
        ("roles", APP_ROLE_ENGINEER),
    ]))
    assert p is not None
    assert p.email == "kosta@example.com"
    assert p.object_id == "0000-1111"
    assert p.app_roles == (APP_ROLE_APPROVER, APP_ROLE_ENGINEER)


def test_malformed_principal_header_is_rejected() -> None:
    assert parse_client_principal("%%%not-base64%%%") is None


def test_no_header_and_no_bypass_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATLAS_DEMO_AUTH_BYPASS", raising=False)
    request: Any = SimpleNamespace(headers={}, method="GET", url=SimpleNamespace(path="/api/me"))
    with pytest.raises(HTTPException) as err:
        auth.current_principal(request)
    assert err.value.status_code == 401


def test_bypass_is_refused_in_azure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_DEMO_AUTH_BYPASS", "true")
    monkeypatch.setenv("ATLAS_CREDENTIAL", "managed")
    with pytest.raises(RuntimeError, match="Refusing to start"):
        auth.check_bypass_at_startup()
    request: Any = SimpleNamespace(headers={}, method="GET", url=SimpleNamespace(path="/"))
    with pytest.raises(HTTPException):
        auth.current_principal(request)


def test_bypass_works_locally_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_DEMO_AUTH_BYPASS", "true")
    monkeypatch.setenv("ATLAS_CREDENTIAL", "cli")
    request: Any = SimpleNamespace(headers={}, method="GET", url=SimpleNamespace(path="/"))
    assert auth.current_principal(request).source == "bypass"


# --------------------------------------------------------------------------- personas --


def test_default_persona_follows_the_highest_app_role() -> None:
    assert default_persona_role([APP_ROLE_APPROVER, APP_ROLE_ENGINEER]) == "manager"
    assert default_persona_role([APP_ROLE_ENGINEER]) == "sre"
    assert default_persona_role([APP_ROLE_READONLY]) == "readonly"
    assert default_persona_role([]) == "readonly"


def test_act_as_only_ever_narrows() -> None:
    assert narrow("manager", "readonly") == "readonly"
    assert narrow("readonly", "director") == "readonly"
    assert narrow("sre", "manager") == "sre"
    assert narrow("sre", None) == "sre"
    assert narrow("sre", "root") == "sre"


def test_canonical_persona_is_deterministic() -> None:
    people = [
        Person(user_id=u, display_name=u, team="t", role="manager", email=f"{u}@contoso.example")
        for u in ("u-b", "u-a")
    ]
    chosen = pick_persona(people, "manager")
    assert chosen is not None
    assert chosen.user_id == "u-a"


# ------------------------------------------------------------------------- entitlements --


def test_chat_needs_an_atlas_role() -> None:
    assert can_chat([APP_ROLE_READONLY]).allowed
    assert not can_chat(["Some.Other.Role"]).allowed


@pytest.mark.parametrize(
    ("roles", "ent", "risk", "tier", "blast", "allowed"),
    [
        ([APP_ROLE_APPROVER], MANAGER, "high", 1, 10, True),
        ([APP_ROLE_ENGINEER], MANAGER, "low", 4, 1, False),  # no approver role
        ([APP_ROLE_APPROVER], READONLY, "low", 4, 0, False),  # narrowed to readonly
        ([APP_ROLE_APPROVER], SRE, "high", 2, 3, False),      # risk too high
        ([APP_ROLE_APPROVER], SRE, "low", 1, 3, False),       # tier 1 not allowed
        ([APP_ROLE_APPROVER], SRE, "low", 2, 9, False),       # blast radius over 8
        ([APP_ROLE_APPROVER], None, "low", 4, 0, False),      # no entitlement found
    ],
)
def test_approval_needs_role_and_entitlement(
    roles: list[str], ent: Entitlement | None, risk: str, tier: int, blast: int, allowed: bool
) -> None:
    decision = can_approve(roles, ent, risk=risk, tier=tier, blast_radius=blast)
    assert decision.allowed is allowed, decision.reason


# ------------------------------------------------------------------------ stream events --


def test_tool_outcomes_are_classified_from_typed_results() -> None:
    assert classify_result('{"service_id": "svc-payments"}', None) == ("ok", "")
    assert classify_result('{"ok": false, "kind": "service", "message": "No service"}',
                           None)[0] == "not_found"
    assert classify_result('{"ok": false, "backend": "telemetry store", "message": "down"}',
                           None) == ("failed", "down")
    assert classify_result(None, "boom") == ("failed", "boom")


def _content(**kw: Any) -> Any:
    base = {"type": "text", "text": None, "name": None, "call_id": None, "result": None,
            "arguments": None,
            "exception": None}
    return SimpleNamespace(**{**base, **kw})


def test_tracker_pairs_calls_and_results_once() -> None:
    tracker = ActivityTracker()
    start = SimpleNamespace(contents=[
        _content(type="function_call", call_id="c1", name="lookup_service"),
        _content(type="function_call", call_id="c1", name="lookup_service"),  # argument fragment
    ])
    events = tracker.feed(start)  # type: ignore[arg-type]
    assert [e.event for e in events] == ["tool_start"]
    end = SimpleNamespace(contents=[
        _content(type="function_result", call_id="c1", result='{"ok": false, "kind": "x"}'),
        _content(type="text", text="done"),
    ])
    events = tracker.feed(end)  # type: ignore[arg-type]
    assert events[0].event == "tool_end"
    assert events[0].data["name"] == "lookup_service"
    assert events[0].data["outcome"] == "not_found"
    assert events[1].event == "token"
    assert tracker.tool_calls == ["lookup_service"]
