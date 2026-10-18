"""Tool descriptions, governed in API Management.

PHASE-09's claim: change one tool description in APIM and every agent surface picks it up. The
prompt agent already gets it - its tools come from the toolbox, which lists the APIM MCP server.
The web app, the orchestrator and the hosted agent run the tools in-process, with descriptions
from docstrings. This closes that gap: it reads `tools/list` from the same APIM MCP server
(`atlas-ops-mcp`) and overrides the description of every in-process tool with the same name.

- **Cached for 60 s.** A description changed in APIM reaches every surface within a minute,
  without a restart or a redeploy (CLAUDE.md 9.6 names this refresh as a timed demo beat).
- **Updated in place.** The orchestrator builds its agents once per workflow shape and keeps them;
  a tool's description is read each time its schema is sent to the model, so mutating the live
  tool objects is what reaches an already-built agent.
- **Never blocks a run.** A dead or slow gateway keeps the last good descriptions, or the
  docstrings if there never were any, and says so in the log. Tool descriptions are not worth an
  outage.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import weakref
from typing import Any

import httpx

log = logging.getLogger("atlas.tool_catalog")

TTL_S = 60.0
TIMEOUT_S = 5.0
PROTOCOL_VERSION = "2025-06-18"


def parse_mcp(response: httpx.Response) -> dict[str, Any]:
    """Streamable HTTP answers as plain JSON or as a one-event SSE stream. Handle both."""
    if "text/event-stream" in response.headers.get("content-type", ""):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return dict(json.loads(line[5:].strip()))
        return {}
    return dict(response.json()) if response.text else {}


async def list_mcp_tools(url: str, headers: dict[str, str], *,
                         timeout_s: float = TIMEOUT_S) -> list[dict[str, Any]]:
    """initialize, notifications/initialized, tools/list against one MCP server."""
    base = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            **headers}
    async with httpx.AsyncClient(timeout=timeout_s) as http:
        init = await http.post(url, headers=base, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": "atlas-tool-catalog", "version": "1.0.0"}}})
        init.raise_for_status()
        session = init.headers.get("Mcp-Session-Id")
        follow = {**base, **({"Mcp-Session-Id": session} if session else {})}
        await http.post(url, headers=follow,
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = await http.post(url, headers=follow,
                                 json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed.raise_for_status()
    result = parse_mcp(listed).get("result") or {}
    return list(result.get("tools") or [])


async def call_mcp_tool(url: str, headers: dict[str, str], name: str, arguments: dict[str, Any],
                        *, timeout_s: float = 30.0) -> dict[str, Any]:
    """initialize, then one tools/call. Returns the JSON-RPC result (or error) object."""
    base = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            **headers}
    async with httpx.AsyncClient(timeout=timeout_s) as http:
        init = await http.post(url, headers=base, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": "atlas-gateway-probe", "version": "1.0.0"}}})
        init.raise_for_status()
        session = init.headers.get("Mcp-Session-Id")
        follow = {**base, **({"Mcp-Session-Id": session} if session else {})}
        await http.post(url, headers=follow,
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        called = await http.post(url, headers=follow, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}})
        called.raise_for_status()
    answer = parse_mcp(called)
    return dict(answer.get("result") or {"error": answer.get("error")})


def description_digest(descriptions: dict[str, str]) -> str:
    canonical = json.dumps(descriptions, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


class ToolCatalog:
    """Descriptions from an MCP server, applied to in-process tools that share a name."""

    def __init__(self, url: str, credential: Any, scope: str, *, ttl_s: float = TTL_S) -> None:
        self.url, self.scope, self.ttl_s = url, scope, ttl_s
        self._credential = credential
        self._descriptions: dict[str, str] = {}
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()
        self._tracked: list[weakref.ref[Any]] = []
        self.last_error: str | None = None

    @property
    def descriptions(self) -> dict[str, str]:
        return dict(self._descriptions)

    @property
    def digest(self) -> str:
        """12 hex chars identifying the current description set. Logged on every change, so
        App Insights shows which surface picked up which version, and when."""
        return description_digest(self._descriptions)

    @property
    def is_stale(self) -> bool:
        return self._fetched_at is None or time.monotonic() - self._fetched_at >= self.ttl_s

    def describe(self, name: str, default: str) -> str:
        return self._descriptions.get(name, default)

    def track(self, tool: Any) -> None:
        """Remember a live tool so a later refresh can update it in place."""
        if not hasattr(tool, "_atlas_default_description"):
            tool._atlas_default_description = tool.description
        self._tracked.append(weakref.ref(tool))
        tool.description = self.describe(tool.name, tool._atlas_default_description)

    async def refresh_if_stale(self) -> bool:
        """Re-read tools/list if the cache is older than the TTL. True if anything changed."""
        if not self._stale():
            return False
        async with self._lock:
            # Re-checked under the lock: another coroutine may have refreshed while this waited.
            if not self._stale():
                return False
            return await self._refresh()

    def _stale(self) -> bool:
        return self.is_stale

    async def _refresh(self) -> bool:
        try:
            token = await self._credential.get_token(self.scope)
            tools = await list_mcp_tools(self.url, {"Authorization": f"Bearer {token.token}"})
        except Exception as exc:
            # Retry sooner than a full TTL, but not on every call.
            self._fetched_at = time.monotonic() - self.ttl_s / 2
            self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            log.warning("tool catalog unavailable, keeping %s: %s",
                        "the last descriptions" if self._descriptions else "the docstrings",
                        self.last_error)
            return False
        fresh = {str(t["name"]): str(t.get("description") or "") for t in tools if t.get("name")}
        fresh = {k: v for k, v in fresh.items() if v}
        changed = fresh != self._descriptions
        self._descriptions, self._fetched_at, self.last_error = fresh, time.monotonic(), None
        self._apply()
        if changed:
            log.info("tool catalog updated: digest %s, %d descriptions from %s",
                     self.digest, len(fresh), self.url)
        return changed

    def _apply(self) -> None:
        live: list[weakref.ref[Any]] = []
        for ref in self._tracked:
            tool = ref()
            if tool is None:
                continue
            tool.description = self.describe(tool.name, tool._atlas_default_description)
            live.append(ref)
        self._tracked = live


__all__ = ["ToolCatalog", "call_mcp_tool", "description_digest", "list_mcp_tools",
           "parse_mcp"]
