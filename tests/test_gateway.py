"""Phase 09: the gateway's source-controlled pieces, and the code that talks to it.

Offline. The deployed gateway is proven by scripts/prove_governance.py and verify_phase09.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from atlas_agent.tools import TOOL_NAMES, AtlasTools
from atlas_common.ops_client import OpsApiClient, OpsApiUnavailableError
from atlas_common.tool_catalog import ToolCatalog

ROOT = Path(__file__).resolve().parents[1]
POLICIES = ROOT / "apim" / "policies"
TOOLS_JSON = ROOT / "apim" / "mcp" / "atlas-ops-mcp-tools.json"
SPEC = ROOT / "apim" / "openapi" / "atlas-ops-api.yaml"

sys.path.insert(0, str(ROOT / "functions" / "ops_api"))
from opsapi import core  # noqa: E402

# ------------------------------------------------------------------------ policy files --

ALL_POLICIES = sorted(POLICIES.rglob("*.xml"))


def mask_expressions(text: str) -> str:
    """APIM policy expressions (`"@(...)"`, `"@{...}"`, and element text `@(...)`) may contain
    unescaped quotes - APIM's parser allows it, strict XML does not. Replace each expression with
    a placeholder, bracket-matched, so the document's STRUCTURE can be checked strictly."""
    out, i = [], 0
    while i < len(text):
        if text.startswith(("@(", "@{"), i):
            opener = text[i + 1]
            closer = ")" if opener == "(" else "}"
            depth, j, in_str = 0, i + 1, False
            while j < len(text):
                ch = text[j]
                if ch == '"' and text[j - 1] != "\\":
                    in_str = not in_str
                elif not in_str and ch == opener:
                    depth += 1
                elif not in_str and ch == closer:
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            out.append("EXPR")
            i = j + 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def strip_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


@pytest.mark.parametrize("path", ALL_POLICIES, ids=lambda p: p.name)
def test_every_policy_is_well_formed_xml(path: Path) -> None:
    # Our own source-controlled policy files, not untrusted input.
    text = mask_expressions(strip_comments(path.read_text(encoding="utf-8")))
    root = ET.fromstring(text)  # noqa: S314
    assert root.tag == ("fragment" if path.parent.name == "fragments" else "policies")


def test_every_included_fragment_exists() -> None:
    fragments = {p.stem for p in (POLICIES / "fragments").glob("*.xml")}
    for path in ALL_POLICIES:
        for ref in re.findall(r'include-fragment fragment-id="([^"]+)"', path.read_text("utf-8")):
            assert ref in fragments, f"{path.name} includes missing fragment {ref}"


@pytest.mark.parametrize("name", ["global.xml", "ops-mcp.xml", "tools-mcp.xml"])
def test_mcp_scopes_never_touch_the_response_body(name: str) -> None:
    """Reading context.Response.Body buffers the response and breaks MCP streaming."""
    text = strip_comments((POLICIES / name).read_text(encoding="utf-8"))
    assert "Response.Body" not in text
    assert "set-body" not in text


def test_the_agent_gate_distinguishes_401_from_403() -> None:
    text = (POLICIES / "fragments" / "atlas-agent-auth.xml").read_text(encoding="utf-8")
    assert 'failed-validation-httpcode="401"' in text
    assert '<set-status code="403"' in text
    assert "Atlas.Agent" in text
    # v2 tokens carry the client id as aud; accepting only the URI would 401 every agent.
    assert "{{gateway-app-id}}" in text and "api://atlas-gateway" in text


def test_the_ops_api_retries_three_times_and_counts_attempts() -> None:
    text = (POLICIES / "ops-api.xml").read_text(encoding="utf-8")
    assert 'count="3"' in text and "x-atlas-gateway-attempts" in text
    assert 'calls="120" renewal-period="60"' in text
    # The caller's token is swapped for APIM's own identity before the backend.
    assert 'authentication-managed-identity resource="api://atlas-ops-api"' in text


# ------------------------------------------------------------------------ MCP tool contract --


def test_mcp_tools_map_to_real_spec_operations() -> None:
    tools = json.loads(TOOLS_JSON.read_text(encoding="utf-8"))["tools"]
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    ops = {op["operationId"] for item in spec["paths"].values() for op in item.values()}
    for entry in tools:
        assert entry["operation"] in ops, entry
        assert entry["name"] in TOOL_NAMES, f"{entry['name']} is not an in-process tool"


def test_the_offline_fallback_matches_the_governed_descriptions() -> None:
    """The docstrings are what an agent sees if APIM is unreachable. Keep them in step, so a
    gateway outage never silently changes what the model is told."""
    tools = json.loads(TOOLS_JSON.read_text(encoding="utf-8"))["tools"]
    for entry in tools:
        doc = inspect.cleandoc(getattr(AtlasTools, entry["name"]).__doc__ or "")
        assert doc == entry["description"], f"{entry['name']}: docstring drifted from APIM"


# ------------------------------------------------------------------------ tool catalog --


class FakeCredential:
    async def get_token(self, *_scopes: str) -> Any:
        return type("T", (), {"token": "t", "expires_on": 4102444800})()


class FakeTool:
    def __init__(self, name: str, description: str) -> None:
        self.name, self.description = name, description


def test_the_catalog_overrides_descriptions_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    served = [{"name": "lookup_service", "description": "FROM APIM"}]

    async def fake_list(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return served

    monkeypatch.setattr("atlas_common.tool_catalog.list_mcp_tools", fake_list)
    catalog = ToolCatalog("https://gw/ops-mcp/mcp", FakeCredential(), "scope", ttl_s=0)
    live, other = FakeTool("lookup_service", "docstring"), FakeTool("search_knowledge", "kept")
    catalog.track(live)
    catalog.track(other)
    assert asyncio.run(catalog.refresh_if_stale()) is True
    assert live.description == "FROM APIM"
    assert other.description == "kept"
    # A later change in APIM reaches the SAME object: that is how a cached agent sees it.
    served[0]["description"] = "CHANGED IN APIM"
    asyncio.run(catalog.refresh_if_stale())
    assert live.description == "CHANGED IN APIM"


def test_a_dead_gateway_keeps_the_docstrings(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        raise httpx.ConnectError("down")

    monkeypatch.setattr("atlas_common.tool_catalog.list_mcp_tools", boom)
    catalog = ToolCatalog("https://gw/ops-mcp/mcp", FakeCredential(), "scope", ttl_s=0)
    live = FakeTool("lookup_service", "docstring")
    catalog.track(live)
    assert asyncio.run(catalog.refresh_if_stale()) is False
    assert live.description == "docstring"
    assert catalog.last_error and "ConnectError" in catalog.last_error


def test_the_catalog_respects_its_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def counting(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr("atlas_common.tool_catalog.list_mcp_tools", counting)
    catalog = ToolCatalog("https://gw/ops-mcp/mcp", FakeCredential(), "scope", ttl_s=60)

    async def twice() -> None:
        await catalog.refresh_if_stale()
        await catalog.refresh_if_stale()

    asyncio.run(twice())
    assert calls == 1


# ------------------------------------------------------------------------ ops client --


def test_the_subscription_key_comes_from_the_provider_once() -> None:
    seen: list[httpx.Request] = []
    fetched = 0

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"services": []})

    async def provider() -> str:
        nonlocal fetched
        fetched += 1
        return "k-123"

    async def go() -> None:
        client = OpsApiClient("https://gw/ops", FakeCredential(), scope="api://atlas-gateway/.default",
                              key_provider=provider, transport=httpx.MockTransport(handler))
        await client.list_services()
        await client.list_services()
        await client.close()

    asyncio.run(go())
    assert fetched == 1
    assert all(r.headers["Ocp-Apim-Subscription-Key"] == "k-123" for r in seen)
    assert all(r.headers["Authorization"] == "Bearer t" for r in seen)


def test_an_unreadable_key_is_an_outage_not_a_crash() -> None:
    async def provider() -> str:
        raise PermissionError("403 from Key Vault")

    async def go() -> None:
        client = OpsApiClient("https://gw/ops", FakeCredential(), key_provider=provider,
                              transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        try:
            await client.list_services()
        finally:
            await client.close()

    with pytest.raises(OpsApiUnavailableError):
        asyncio.run(go())


# ------------------------------------------------------------------------ chaos switch --


class ControlStore:
    def __init__(self, doc: dict[str, Any] | None) -> None:
        self.doc, self.reads = doc, 0

    async def query(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
        self.reads += 1
        return [self.doc] if self.doc else []


def _later(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def test_chaos_on_turns_every_guarded_route_into_a_typed_503() -> None:
    store = ControlStore({"id": core.CHAOS_DOC_ID, "enabled": True, "expires_at": _later(60)})
    switch = core.ChaosSwitch(lambda: store, cache_s=0)

    async def work() -> core.Result:
        raise AssertionError("the handler must not run during an outage")

    result = asyncio.run(core.guarded("cid", work, chaos=switch))
    assert result.status == 503
    assert result.body["code"] == "unavailable"
    assert result.headers["x-atlas-chaos"] == "on"


@pytest.mark.parametrize("doc", [
    None,
    {"id": core.CHAOS_DOC_ID, "enabled": False, "expires_at": _later(60)},
    {"id": core.CHAOS_DOC_ID, "enabled": True, "expires_at": _later(-5)},  # a forgotten flag
])
def test_chaos_off_or_expired_lets_requests_through(doc: dict[str, Any] | None) -> None:
    switch = core.ChaosSwitch(lambda: ControlStore(doc), cache_s=0)

    async def work() -> core.Result:
        return core.Result(200, {"ok": True})

    assert asyncio.run(core.guarded("cid", work, chaos=switch)).status == 200


def test_an_unreadable_chaos_switch_is_never_an_outage() -> None:
    class Broken:
        async def query(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
            raise RuntimeError("cosmos down")

    assert asyncio.run(core.ChaosSwitch(lambda: Broken(), cache_s=0).active()) is False
