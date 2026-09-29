"""Talk to the deployed ATLAS MCP server over Streamable HTTP, as an MCP client would.

    uv run python -m scripts.mcp_probe                       # initialize + tools/list
    uv run python -m scripts.mcp_probe --call get_oncall '{"service_id": "svc-payments"}'

The `mcp_extension` system key is fetched from Azure at run time and never written anywhere. In
Phase 09 API Management holds it (Key Vault-backed) and no client ever sees it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Any

import httpx
from atlas_common.config import load_local_env

ENDPOINT = "/runtime/webhooks/mcp"


def mcp_app() -> tuple[str, str]:
    """(hostname, system key) for the MCP function app."""
    az = shutil.which("az") or "az"
    rg = os.environ["AZURE_RESOURCE_GROUP"]
    name = subprocess.run(
        [az, "functionapp", "list", "-g", rg, "--query",
         "[?contains(name, 'atlas-mcp')].name | [0]", "-o", "tsv"],
        capture_output=True, text=True, timeout=120, check=True).stdout.strip()
    key = subprocess.run(
        [az, "functionapp", "keys", "list", "-g", rg, "-n", name,
         "--query", "systemKeys.mcp_extension", "-o", "tsv"],
        capture_output=True, text=True, timeout=120, check=True).stdout.strip()
    return f"https://{name}.azurewebsites.net", key


def parse(response: httpx.Response) -> dict[str, Any]:
    """Streamable HTTP may answer as plain JSON or as a one-event SSE stream. Handle both."""
    if "text/event-stream" in response.headers.get("content-type", ""):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return dict(json.loads(line[5:].strip()))
        return {}
    return dict(response.json()) if response.text else {}


class McpSession:
    def __init__(self, base: str, key: str) -> None:
        self.http = httpx.Client(base_url=base, timeout=90, headers={
            "x-functions-key": key, "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"})
        self.session_id: str | None = None
        self._id = 0

    def rpc(self, method: str, params: dict[str, Any] | None = None,
            notify: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            self._id += 1
            body["id"] = self._id
        headers = {"Mcp-Session-Id": self.session_id} if self.session_id else {}
        response = self.http.post(ENDPOINT, json=body, headers=headers)
        if response.status_code >= 400:
            raise RuntimeError(f"{method}: HTTP {response.status_code} {response.text[:300]}")
        self.session_id = response.headers.get("Mcp-Session-Id", self.session_id)
        return {} if notify else parse(response)

    def open(self) -> dict[str, Any]:
        init = self.rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "atlas-mcp-probe", "version": "1.0.0"}})
        self.rpc("notifications/initialized", notify=True)
        return init


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--call", nargs=2, metavar=("TOOL", "ARGS_JSON"))
    parser.add_argument("--json", action="store_true", help="print the raw tools/list result")
    args = parser.parse_args(argv)

    load_local_env()
    base, key = mcp_app()
    session = McpSession(base, key)
    init = session.open()
    server = (init.get("result") or {}).get("serverInfo", {})
    print(f"server: {server.get('name')} {server.get('version')}  ({base}{ENDPOINT})")

    listed = (session.rpc("tools/list").get("result") or {}).get("tools", [])
    if args.json:
        print(json.dumps(listed, indent=2))
    else:
        print(f"tools/list: {len(listed)} tools")
        for tool in listed:
            props = (tool.get("inputSchema") or {}).get("properties", {})
            required = (tool.get("inputSchema") or {}).get("required", [])
            print(f"  {tool['name']:24s} args: "
                  + ", ".join(f"{p}{'*' if p in required else ''}" for p in props))

    if args.call:
        name, raw = args.call
        result = session.rpc("tools/call", {"name": name, "arguments": json.loads(raw)})
        content = ((result.get("result") or {}).get("content") or [{}])[0].get("text", "")
        print(f"\ntools/call {name}:")
        try:
            print(json.dumps(json.loads(content), indent=2)[:3000])
        except ValueError:
            print(content[:3000] or json.dumps(result)[:3000])
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
