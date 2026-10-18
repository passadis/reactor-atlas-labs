"""Change ONE tool description in APIM and watch all four agent surfaces pick it up. Then undo it.

    uv run python -m scripts.tool_description_change             # change, verify, restore
    uv run python -m scripts.tool_description_change --keep      # leave the change in place
    uv run python -m scripts.tool_description_change --restore   # put the governed text back

PHASE-09 acceptance: "Changing one tool description in APIM changes behaviour on all four agent
surfaces." The description of `lookup_service` on the APIM MCP server `atlas-ops-mcp` gets a
visible revision marker, and the script checks where that exact text is now in force:

  prompt agent   the Foundry toolbox's tools/list (the toolbox lists the APIM MCP server)
  web app        its tool catalog digest, via /api/gateway/probe
  orchestrator   its tool catalog digest, via the web app (it is internal-only)
  hosted agent   one invocation, then its "tool catalog updated: digest ..." log line in
                 Application Insights

The in-process surfaces refresh on a 60 s cache. The Foundry toolbox (prompt agent) caches its
tool list with a sliding expiry, so this republishes the toolbox - same definition, new version -
which makes it rediscover at once (docs/API-NOTES.md 16.10). The wall clock per surface is
printed; CLAUDE.md 9.6 wants it recorded in docs/DEMO-NOTES.md.

This is a control-plane write to APIM (one tool resource), and it always restores the governed
text from apim/mcp/atlas-ops-mcp-tools.json unless --keep. It is a rehearsed beat, not something
to improvise during a broadcast (CLAUDE.md section 9).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from atlas_common.config import load_local_env
from atlas_common.tool_catalog import description_digest, list_mcp_tools
from azure.identity import AzureCliCredential

from scripts.sse_probe import cli_token

ROOT = Path(__file__).resolve().parents[1]
TOOL = "lookup_service"
API_VERSION = "2025-09-01-preview"
# The Foundry toolbox caches the APIM MCP server's tool list with a sliding expiry: 68 s when asked
# once a minute, never within 420 s when asked every 10 s (docs/API-NOTES.md 16.10). The
# in-process surfaces follow within the 60 s catalogue cache.
BUDGET_S = 420.0
TOOLBOX_EVERY_S = 60.0


def az(*args: str, body: dict[str, Any] | None = None) -> str:
    cmd = [shutil.which("az") or "az", *args]
    with tempfile.TemporaryDirectory() as tmp:
        if body is not None:
            # Through a file: a multi-line description with quotes does not survive az.cmd's
            # command-line quoting on Windows.
            path = Path(tmp) / "body.json"
            path.write_text(json.dumps(body), encoding="utf-8")
            cmd += ["--body", f"@{path}", "--headers", "Content-Type=application/json",
                    "If-Match=*"]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                              check=True).stdout.strip()


def tool_url() -> str:
    sub = az("account", "show", "--query", "id", "-o", "tsv")
    return (f"https://management.azure.com/subscriptions/{sub}/resourceGroups/"
            f"{os.environ['AZURE_RESOURCE_GROUP']}/providers/Microsoft.ApiManagement/service/"
            f"{os.environ['ATLAS_APIM_NAME']}/apis/atlas-ops-mcp/tools/{TOOL}"
            f"?api-version={API_VERSION}")


def governed_text() -> str:
    tools = json.loads((ROOT / "apim/mcp/atlas-ops-mcp-tools.json").read_text("utf-8"))["tools"]
    return next(str(t["description"]) for t in tools if t["name"] == TOOL)


def set_description(text: str) -> None:
    url = tool_url()
    current = json.loads(az("rest", "--method", "get", "--url", url))
    props = current["properties"]
    props["description"] = text
    az("rest", "--method", "put", "--url", url,
       body={"properties": {k: props[k] for k in ("displayName", "description", "operationId")}})


class Surfaces:
    def __init__(self) -> None:
        self.web = httpx.Client(base_url=os.environ["ATLAS_WEB_URL"].rstrip("/"), timeout=120,
                                headers={"Authorization": f"Bearer {cli_token()}"})
        self.cred = AzureCliCredential()

    def probe(self) -> dict[str, Any]:
        r = self.web.post("/api/gateway/probe", json={"kind": "mcp_tools"})
        r.raise_for_status()
        return dict(r.json())

    def expected_digest(self, probe: dict[str, Any]) -> str:
        tools = probe["servers"]["ops-mcp"]["tools"]
        return description_digest({t["name"]: t["description"] for t in tools if t["description"]})

    def toolbox_has(self, marker: str) -> bool:
        endpoint = (f"{os.environ['FOUNDRY_PROJECT_ENDPOINT'].rstrip('/')}"
                    "/toolboxes/atlas-tools/mcp?api-version=v1")
        token = self.cred.get_token("https://ai.azure.com/.default").token
        tools = asyncio.run(list_mcp_tools(endpoint, {"Authorization": f"Bearer {token}"},
                                           timeout_s=60))
        return any(marker in str(t.get("description") or "") for t in tools)

    def invoke_hosted(self) -> None:
        from atlas_common.auth import get_credential
        from azure.ai.projects import AIProjectClient

        from scripts import hosted_agent as ha
        project = AIProjectClient(endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
                                  credential=get_credential())
        client = project.get_openai_client(agent_name=ha.AGENT).with_options(max_retries=0)
        client.responses.create(input="Checkout API p95 latency is elevated for EU customers.",
                                timeout=240)

    def republish_toolbox(self, reason: str) -> str:
        """Make the prompt agent's toolbox rediscover now (docs/API-NOTES.md 16.10)."""
        from atlas_common.auth import get_credential
        from atlas_common.config import Settings
        from azure.ai.projects import AIProjectClient

        from scripts.upsert_toolbox import publish
        settings = Settings.from_env()
        project = AIProjectClient(endpoint=settings.foundry_project_endpoint,
                                  credential=get_credential())
        return publish(project, settings, reason)

    def hosted_logged(self, digest: str) -> list[str]:
        """App Insights roles that logged this digest. REST with a hard timeout, not `az`: a
        hung az.cmd child on Windows can hold its pipes past subprocess's own timeout."""
        if not getattr(self, "app_id", ""):
            self.app_id = az("monitor", "app-insights", "component", "show", "--app",
                             os.environ["ATLAS_APPINSIGHTS_NAME"], "-g",
                             os.environ["AZURE_RESOURCE_GROUP"], "--query", "appId", "-o", "tsv")
        token = self.cred.get_token("https://api.applicationinsights.io/.default").token
        query = ("traces | where timestamp > ago(30m) and message has "
                 f"'tool catalog updated: digest {digest}' | distinct cloud_RoleName")
        try:
            r = httpx.post(f"https://api.applicationinsights.io/v1/apps/{self.app_id}/query",
                           json={"query": query}, timeout=60,
                           headers={"Authorization": f"Bearer {token}"})
            rows = r.json()["tables"][0]["rows"] if r.status_code == 200 else []
        except (httpx.HTTPError, KeyError, ValueError):
            rows = []
        return [str(row[0]) for row in rows if row and row[0]]

def converge(s: Surfaces, marker: str | None, t0: float) -> dict[str, float | None]:
    """Seconds until each surface shows the current text; None if it never did in the budget."""
    seen: dict[str, float | None] = {"prompt agent": None, "web app": None, "orchestrator": None}
    expected = ""
    last_toolbox = -TOOLBOX_EVERY_S
    while time.perf_counter() - t0 < BUDGET_S and None in seen.values():
        probe = s.probe()
        expected = s.expected_digest(probe)
        elapsed = round(time.perf_counter() - t0, 1)
        if seen["web app"] is None and (probe.get("catalog") or {}).get("digest") == expected:
            seen["web app"] = elapsed
        if seen["orchestrator"] is None and \
                (probe.get("orchestrator_catalog") or {}).get("digest") == expected:
            seen["orchestrator"] = elapsed
        if seen["prompt agent"] is None and time.perf_counter() - last_toolbox >= TOOLBOX_EVERY_S:
            # Not on every loop: asking the toolbox for its tool list appears to keep its cached
            # copy alive (sliding expiry), so polling it every few seconds delays the very change
            # being measured (docs/API-NOTES.md 16.10).
            last_toolbox = time.perf_counter()
            ok = s.toolbox_has(marker) if marker else not s.toolbox_has("[rev ")
            if ok:
                seen["prompt agent"] = elapsed
        if None in seen.values():
            time.sleep(10)
    seen["_digest"] = expected  # type: ignore[assignment]
    return seen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args(argv)
    load_local_env()
    s = Surfaces()

    if args.restore:
        set_description(governed_text())
        version = s.republish_toolbox("restore governed descriptions")
        print(f"{TOOL}: governed description restored (toolbox v{version})")
        return 0

    marker = f"[rev {datetime.now(UTC).strftime('%H%M%S')}]"
    print(f"changing ONE description in APIM: atlas-ops-mcp / {TOOL}  ->  prefix {marker}")
    set_description(f"{marker} {governed_text()}")
    t0 = time.perf_counter()
    version = s.republish_toolbox(f"tool description change {marker}")
    print(f"toolbox republished as v{version} so the prompt agent rediscovers now")
    ok = False
    try:
        seen = converge(s, marker, t0)
        digest = str(seen.pop("_digest"))
        s.invoke_hosted()
        roles = s.hosted_logged(digest)
        deadline = time.monotonic() + 240
        while not any("incident-agent" in r for r in roles) and time.monotonic() < deadline:
            time.sleep(20)
            roles = s.hosted_logged(digest)
        hosted = [r for r in roles if "incident-agent" in r]
        seen["hosted agent"] = round(time.perf_counter() - t0, 1) if hosted else None
        print(f"\nnew description digest {digest}")
        for surface, when in seen.items():
            state = f"in force after {when} s" if when is not None else "NOT SEEN"
            print(f"  {surface:14s} {state}")
        print(f"  (App Insights roles that logged digest {digest}: {', '.join(roles) or 'none'})")
        ok = all(v is not None for v in seen.values())
    finally:
        if not args.keep:
            set_description(governed_text())
            version = s.republish_toolbox("restore governed descriptions")
            print(f"\nrestored the governed description of {TOOL} (toolbox v{version})")
    print("\nall four surfaces picked up the change" if ok else "\nNOT all surfaces picked it up")
    return 0 if ok else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
