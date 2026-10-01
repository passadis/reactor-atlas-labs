"""Print one case's trace as a text waterfall and assert it crosses every hop.

    uv run python -m scripts.trace_check <case_id>             # or: make trace-check CASE=<id>
    uv run python -m scripts.trace_check <case_id> --full      # include Cosmos/token/storage noise
    uv run python -m scripts.trace_check --operation <trace_id>

Finds the case's operation (the trace holding its `atlas.case` span; if a case spans several
traces, the one touching the most components), pulls every request and dependency in it from
Application Insights, and prints it as a tree with start offsets and durations.

Asserts, exit non-zero on failure:
- one operation_Id holds web, orchestrator, agent nodes, APIM, the operations Function, Service Bus
  and the worker;
- every span emitted by ATLAS's own code (cloud_RoleName `atlas.*`) inside the case carries
  `atlas.case_id`. APIM and Logic Apps spans are platform-emitted and cannot carry it; they are
  joined by operation_Id instead, which is the point of propagating traceparent.

The MCP Function is reported, not required: the canonical orchestrator path calls the REST
operations API through APIM; the MCP server is the Foundry toolbox's path (API-NOTES 18).

Read-only. Telemetry ingestion lags 1-3 minutes; `--wait` polls until the worker hop lands.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from atlas_common.config import load_local_env
from azure.identity import AzureCliCredential

# A hop is present when any span matches (role substring, name prefix or dependency type substring).
HOPS: dict[str, tuple[str, ...]] = {
    "web": ("role:atlas-web",),
    "orchestrator": ("role:atlas-orchestrator",),
    "agent nodes": ("name:atlas.node.", "name:invoke_agent "),
    "APIM": ("role:apim-",),
    "ops-api Function": ("role:atlas-ops-api", "role:func-atlas-api"),
    "Service Bus": ("type:Microsoft.ServiceBus", "name:ServiceBus.",
                    "name:atlas.remediation.enqueue"),
    "worker": ("role:atlas-worker",),
}
OPTIONAL_HOPS: dict[str, tuple[str, ...]] = {
    "Logic App": ("role:logic-",),
    "MCP Function": ("role:atlas-ops-mcp", "role:func-atlas-mcp"),
}
# Hidden by default: real, but they bury the story (Cosmos calls, token fetches, Logic Apps'
# internal storage traffic, framework edge bookkeeping).
NOISE = ("POST /dbs/", "GET /dbs/", "PUT /dbs/", "ContainerProxy.", "DatabaseProxy.", "GET /msi/",
         "edge_group.process", "POST statlaslogic", "PUT statlaslogic", "GET statlaslogic",
         "HEAD statlaslogic", "DELETE statlaslogic", "PATCH statlaslogic")


@dataclass
class Span:
    id: str
    parent: str
    start_ms: float
    duration_ms: float
    role: str
    name: str
    kind: str
    dep_type: str
    success: bool
    case_id: str
    children: list[Span] = field(default_factory=list)


def _az(*args: str) -> str:
    return subprocess.run([shutil.which("az") or "az", *args], capture_output=True, text=True,
                          timeout=120, check=True).stdout.strip()


class AppInsights:
    def __init__(self) -> None:
        self._app_id = _az("monitor", "app-insights", "component", "show", "--app",
                           os.environ["ATLAS_APPINSIGHTS_NAME"], "-g",
                           os.environ["AZURE_RESOURCE_GROUP"], "--query", "appId", "-o", "tsv")
        self._credential = AzureCliCredential()

    def query(self, kql: str, timespan: str = "P7D") -> list[dict[str, Any]]:
        r = httpx.post(f"https://api.applicationinsights.io/v1/apps/{self._app_id}/query",
                       json={"query": kql, "timespan": timespan}, timeout=90,
                       headers={"Authorization": "Bearer " + self._credential.get_token(
                           "https://api.applicationinsights.io/.default").token})
        if r.status_code != 200:  # KQL errors arrive as a 400 whose body says why
            raise RuntimeError(f"App Insights query failed: HTTP {r.status_code} {r.text[:400]}")
        table = r.json()["tables"][0]
        cols = [c["name"] for c in table["columns"]]
        return [dict(zip(cols, row, strict=True)) for row in table["rows"]]


def _quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def find_operation(ai: AppInsights, case_id: str) -> str | None:
    rows = ai.query(f"""
        union requests, dependencies
        | where tostring(customDimensions["atlas.case_id"]) == {_quote(case_id)}
        | summarize roles = dcount(cloud_RoleName), has_case = countif(name == "atlas.case"),
                    started = min(timestamp) by operation_Id
        | order by has_case desc, roles desc, started asc
        | take 1""")
    return str(rows[0]["operation_Id"]) if rows else None


def load_spans(ai: AppInsights, operation_id: str) -> list[Span]:
    rows = ai.query(f"""
        union requests, dependencies
        | where operation_Id == {_quote(operation_id)}
        | project timestamp, id, operation_ParentId, duration, cloud_RoleName, name, itemType,
                  type, success, case_id = tostring(customDimensions["atlas.case_id"])
        | order by timestamp asc
        | take 5000""")
    if not rows:
        return []
    from datetime import datetime

    def ms(ts: str) -> float:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000

    t0 = ms(rows[0]["timestamp"])
    return [Span(id=str(r["id"]), parent=str(r["operation_ParentId"] or ""),
                 start_ms=ms(r["timestamp"]) - t0, duration_ms=float(r["duration"] or 0),
                 role=str(r["cloud_RoleName"] or ""), name=str(r["name"] or ""),
                 kind=str(r["itemType"]), dep_type=str(r["type"] or ""),
                 success=str(r["success"]).lower() in ("true", "1"),
                 case_id=str(r["case_id"] or "")) for r in rows]


def build_tree(spans: list[Span]) -> list[Span]:
    by_id = {s.id: s for s in spans}
    roots: list[Span] = []
    for s in spans:
        parent = by_id.get(s.parent)
        if parent is not None and parent is not s:
            parent.children.append(s)
        else:
            roots.append(s)
    for s in spans:
        s.children.sort(key=lambda c: c.start_ms)
    return sorted(roots, key=lambda r: r.start_ms)


def _short_role(role: str) -> str:
    role = role.removeprefix("atlas.")
    return role.split(" ")[0][:22]


def render(roots: list[Span], full: bool) -> tuple[list[str], int]:
    lines: list[str] = []
    hidden = 0

    def walk(span: Span, depth: int, orphan: bool = False) -> None:
        nonlocal hidden
        noisy = not full and span.name.startswith(NOISE)
        if noisy:
            hidden += 1 + _count(span)
            return
        mark = ("" if span.success else "  FAILED") + ("  (parent not ingested)" if orphan else "")
        lines.append(f"{span.start_ms / 1000:8.2f}s {span.duration_ms / 1000:8.2f}s  "
                     f"{_short_role(span.role):22}  {'  ' * depth}{span.name}{mark}")
        for child in span.children:
            walk(child, depth + 1)

    for i, root in enumerate(roots):
        walk(root, 0, orphan=i > 0 and bool(root.parent))
    return lines, hidden


def _count(span: Span) -> int:
    return sum(1 + _count(c) for c in span.children)


def _matches(span: Span, rules: tuple[str, ...]) -> bool:
    for rule in rules:
        kind, _, needle = rule.partition(":")
        value = {"role": span.role, "name": span.name, "type": span.dep_type}[kind]
        if (value.startswith(needle) if kind == "name" else needle in value):
            return True
    return False


def hops(spans: list[Span]) -> tuple[dict[str, bool], dict[str, bool]]:
    return ({h: any(_matches(s, r) for s in spans) for h, r in HOPS.items()},
            {h: any(_matches(s, r) for s in spans) for h, r in OPTIONAL_HOPS.items()})


def case_id_coverage(spans: list[Span]) -> tuple[int, list[Span]]:
    """ATLAS-emitted spans after the case exists, and those missing atlas.case_id.

    Spans before the case's first stamped span (the web's create call, the case id is minted by
    the orchestrator inside it) are excluded: there was no case yet to stamp.
    """
    ours = [s for s in spans if s.role.startswith("atlas.")]
    stamped = [s.start_ms for s in ours if s.case_id]
    if not stamped:
        return len(ours), ours
    first = min(stamped)
    inside = [s for s in ours if s.start_ms >= first]
    return len(inside), [s for s in inside if not s.case_id]


def run(case_id: str | None, operation_id: str | None, full: bool, wait_s: int) -> int:
    load_local_env()
    ai = AppInsights()
    deadline = time.monotonic() + wait_s
    while True:
        op = operation_id or (find_operation(ai, case_id) if case_id else None)
        spans = load_spans(ai, op) if op else []
        present, optional = hops(spans)
        if all(present.values()) or time.monotonic() >= deadline:
            break
        time.sleep(20)
    if not op:
        print(f"FAIL  no trace found for case {case_id}")
        return 1
    lines, hidden = render(build_tree(spans), full)
    print(f"case {case_id or '-'}  operation_Id {op}  {len(spans)} spans "
          f"({hidden} infrastructure spans hidden; --full shows them)\n")
    print(f"{'start':>9} {'duration':>9}  {'component':22}  span")
    print("\n".join(lines))
    print()
    ok = True
    for hop, there in present.items():
        print(f"{'PASS' if there else 'FAIL'}  hop {hop}")
        ok &= there
    for hop, there in optional.items():
        print(f"{'INFO'}  hop {hop}: {'present' if there else 'not on this path'}")
    total, missing = case_id_coverage(spans)
    print(f"{'PASS' if not missing else 'FAIL'}  atlas.case_id on {total - len(missing)}/{total} "
          "ATLAS spans")
    for s in missing[:10]:
        print(f"        missing: {_short_role(s.role)} {s.name}")
    ok &= not missing
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("case_id", nargs="?")
    parser.add_argument("--operation", help="trace id (operation_Id) instead of a case id")
    parser.add_argument("--full", action="store_true", help="include infrastructure spans")
    parser.add_argument("--wait", type=int, default=0,
                        help="poll up to N seconds for late-arriving hops")
    args = parser.parse_args()
    if not args.case_id and not args.operation:
        parser.error("give a case id or --operation")
    return run(args.case_id, args.operation, args.full, args.wait)


if __name__ == "__main__":
    sys.exit(main())
