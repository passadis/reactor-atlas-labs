"""Phase 08: the MCP tool server's contract and behaviour. No Azure, no model.

The contract tests read `opsmcp.tools.TOOLS`, which is exactly what `tools/list` serves, and hold
the pack's two rules: six tools, and every description says when NOT to use the tool.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from atlas_common.models import SLO, Person, Service
from atlas_common.ops_client import OpsApiError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "functions" / "ops_mcp"))

from opsmcp import tools  # noqa: E402

EXPECTED = {"search_ops_knowledge", "get_service_topology", "get_oncall", "open_ticket",
            "submit_remediation", "get_remediation_status"}


def svc(sid: str, tier: int, depends_on: list[str], team: str = "team-payments") -> Service:
    return Service(service_id=sid, name=sid, tier=tier, owner_team=team,  # type: ignore[arg-type]
                   on_call_rotation=f"rot-{sid}", environment="prod", region=["westeurope"],
                   depends_on=depends_on,
                   slo=SLO(availability_target=0.999, latency_p95_ms=250,
                           error_budget_minutes_month=43),
                   runbook_ids=[], change_freeze=False, repository="contoso/x")


SERVICES = [
    svc("svc-payments", 1, ["svc-ledger"]),
    svc("svc-ledger", 1, []),
    svc("svc-checkout-api", 1, ["svc-payments"], team="team-commerce"),
    svc("svc-orders", 2, ["svc-checkout-api"], team="team-commerce"),
]
PEOPLE = [
    Person(user_id="u-b-sre", display_name="Bea", team="team-payments", role="sre",
           email="bea@contoso.example"),
    Person(user_id="u-a-eng", display_name="Ari", team="team-payments", role="engineer",
           email="ari@contoso.example"),
    Person(user_id="u-m", display_name="Mo", team="team-commerce", role="manager",
           email="mo@contoso.example"),
    Person(user_id="u-d", display_name="Dana", team="team-data", role="director",
           email="dana@contoso.example"),
]


class FakeOps:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.refuse_token = False

    async def create_ticket(self, **kw: Any) -> tuple[dict[str, Any], bool]:
        self.calls.append(("create_ticket", kw))
        return {"ticket_id": "TKT-ABC", "case_id": kw["case_id"]}, len(self.calls) > 1

    async def submit_remediation(self, **kw: Any) -> tuple[dict[str, Any], bool]:
        self.calls.append(("submit_remediation", kw))
        if self.refuse_token:
            raise OpsApiError(403, "approval_invalid", "not valid", "cid-1")
        return {"operation_id": "OP-1", "status": "queued"}, False

    async def get_operation(self, operation_id: str) -> dict[str, Any]:
        return {"operation_id": operation_id, "status": "running"}


class FakeBackend:
    def __init__(self) -> None:
        self.fake_ops = FakeOps()
        self.searched: list[dict[str, Any]] = []

    async def services(self) -> list[Service]:
        return SERVICES

    async def people(self) -> list[Person]:
        return list(reversed(PEOPLE))  # order must not matter

    async def search(self, query: str, **kw: Any) -> dict[str, Any]:
        self.searched.append({"query": query, **kw})
        return {"query": query, "citations": ["RB-0060"], "hits": []}

    def ops(self) -> Any:
        return self.fake_ops


def call(name: str, args: dict[str, Any], backend: FakeBackend | None = None) -> dict[str, Any]:
    fake = backend or FakeBackend()
    raw = asyncio.run(tools.invoke(fake, name, json.dumps({"arguments": args})))  # type: ignore[arg-type]
    return dict(json.loads(raw))


# ------------------------------------------------------------------- the contract --


def test_exactly_the_six_tools_the_pack_names() -> None:
    assert set(tools.TOOL_NAMES) == EXPECTED
    assert len(tools.TOOLS) == 6


@pytest.mark.parametrize("tool", tools.TOOLS, ids=lambda t: t.name)
def test_every_description_says_when_not_to_use_the_tool(tool: tools.Tool) -> None:
    assert "Do NOT" in tool.description, f"{tool.name} has no negative guidance"


@pytest.mark.parametrize("tool", tools.TOOLS, ids=lambda t: t.name)
def test_tool_properties_are_well_formed_mcp(tool: tools.Tool) -> None:
    props = json.loads(tool.properties_json())
    assert props, f"{tool.name} takes no arguments"
    for prop in props:
        assert set(prop) == {"propertyName", "propertyType", "description", "isRequired",
                             "isArray"}
        assert prop["propertyType"] in {"string", "number", "integer", "boolean", "object"}
        assert prop["description"]
    assert any(p["isRequired"] for p in props)


def test_the_remediation_tool_warns_against_invented_tokens() -> None:
    text = next(t for t in tools.TOOLS if t.name == "submit_remediation").description
    assert "approval_token" in text and "Never invent" in text


# ------------------------------------------------------------------- behaviour --


def test_topology_walks_both_directions_deterministically() -> None:
    out = call("get_service_topology", {"service_id": "svc-payments", "depth": 3})
    assert out["ok"] is True
    assert out["dependencies"] == ["svc-ledger"]
    assert set(out["dependents"]) == {"svc-checkout-api", "svc-orders"}
    assert out["tier1_dependents"] == ["svc-checkout-api"]


def test_an_unknown_service_is_refused_with_suggestions_not_an_exception() -> None:
    out = call("get_service_topology", {"service_id": "svc-paym"})
    assert out["ok"] is False and "svc-payments" in out["did_you_mean"]


def test_oncall_is_the_same_whatever_order_the_people_arrive_in() -> None:
    out = call("get_oncall", {"service_id": "svc-payments"})
    roles = [c["as"] for c in out["oncall"]]
    assert roles[0] == "primary" and out["oncall"][0]["role"] == "sre"
    assert out["oncall"][1]["as"] == "secondary"
    assert "escalation: director" in roles


def test_search_passes_the_kind_through_as_doc_types() -> None:
    backend = FakeBackend()
    call("search_ops_knowledge", {"query": "payments latency", "kind": "runbooks"}, backend)
    assert backend.searched[0]["doc_types"] == ["runbook"]


def test_search_rejects_an_unknown_kind() -> None:
    assert call("search_ops_knowledge", {"query": "x y z", "kind": "rumours"})["ok"] is False


def test_submit_without_a_token_is_refused_before_any_call() -> None:
    backend = FakeBackend()
    out = call("submit_remediation", {"case_id": "CASE-X", "plan_id": "a" * 16}, backend)
    assert out["ok"] is False and "approval" in out["error"]
    assert not backend.fake_ops.calls, "a token-less submission reached the ops API"


def test_a_refused_token_is_explained_and_not_retried() -> None:
    backend = FakeBackend()
    backend.fake_ops.refuse_token = True
    out = call("submit_remediation", {"case_id": "CASE-X", "plan_id": "a" * 16,
                                      "approval_token": "atlas_apv_" + "z" * 40}, backend)
    assert out["ok"] is False and out["code"] == "approval_invalid"
    assert "Do not retry" in out["error"]
    assert len(backend.fake_ops.calls) == 1


def test_open_ticket_reports_a_replay() -> None:
    backend = FakeBackend()
    args = {"case_id": "CASE-X", "severity": "Sev2", "summary": "Checkout is timing out."}
    first, second = call("open_ticket", args, backend), call("open_ticket", args, backend)
    assert first["replayed"] is False and second["replayed"] is True


def test_a_crashing_tool_returns_a_refusal_not_a_stack_trace() -> None:
    class Broken(FakeBackend):
        async def services(self) -> list[Service]:
            raise RuntimeError("connection string with secrets in it")

    out = call("get_oncall", {"service_id": "svc-payments"}, Broken())
    assert out["ok"] is False and "secrets" not in json.dumps(out)
