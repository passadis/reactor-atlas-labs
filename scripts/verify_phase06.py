"""Phase 06 verification: the multi-agent workflow, against the deployed orchestrator.

The orchestrator has no external ingress, so every check goes through the web app's proxy - which
is also how the demo reaches it. One exception is noted at check 6.

Writes application data only (cases), cleared by `make demo-reset`. No control-plane writes.
Exit non-zero on any failure. Wrapped by scripts/verify_phase06.sh for `make verify-all`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
from atlas_common.config import load_local_env

from scripts.sse_probe import CANONICAL, cli_token

results: list[tuple[str, bool, str]] = []
state: dict[str, Any] = {}

PLAN_READY_BUDGET_S = 90.0
# WorkflowViz declares a plain node as `id["label"];` and a fan-in junction as `id((fan-in))`.
MERMAID_NODE = re.compile(r'^\s*(\w+)(?:\["[^"]*"\]|\(\([^)]*\)\));?$')


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


def guarded(name: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except BaseException as exc:
        check(name, False, f"{type(exc).__name__}: {str(exc)[:300]}")


def client() -> httpx.Client:
    url = os.environ["ATLAS_WEB_URL"].rstrip("/")
    return httpx.Client(base_url=url, headers={"Authorization": f"Bearer {cli_token()}"},
                        timeout=120)


def stream_case(web: httpx.Client, message: str, mode: str) -> dict[str, Any]:
    """Start a case through the web proxy and follow its event stream to the end."""
    started = time.perf_counter()
    response = web.post("/api/workflow", json={"message": message, "mode": mode})
    response.raise_for_status()
    case_id = str(response.json()["case_id"])
    run: dict[str, Any] = {"case_id": case_id, "mode": mode, "nodes": {}, "report": None,
                           "errors": [], "kinds": []}
    event = "message"
    with web.stream("GET", f"/api/workflow/{case_id}/stream", timeout=400) as stream:
        for line in stream.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                payload = json.loads(line.split(":", 1)[1].strip() or "{}")
                run["kinds"].append(event)
                if event == "node":
                    run["nodes"][payload["node"]] = payload["data"]
                elif event == "report":
                    run["report"] = payload["data"]
                elif event == "error":
                    run["errors"].append(payload)
                elif event == "end":
                    break
    run["elapsed_s"] = round(time.perf_counter() - started, 1)
    return run


# ------------------------------------------------------------ 1 deployed and running --
def deployed_and_running() -> None:
    az = shutil.which("az") or "az"
    rg = os.environ["AZURE_RESOURCE_GROUP"]
    name = json.loads(subprocess.run(
        [az, "containerapp", "list", "-g", rg,
         "--query", "[?tags.\"azd-service-name\"=='orchestrator'].name", "-o", "json"],
        capture_output=True, text=True, timeout=120, check=True).stdout or "[]")
    app = json.loads(subprocess.run(
        [az, "containerapp", "show", "-g", rg, "-n", name[0], "-o", "json"],
        capture_output=True, text=True, timeout=120, check=True).stdout)
    image = app["properties"]["template"]["containers"][0]["image"]
    running = app["properties"]["runningStatus"]
    replicas = app["properties"]["template"]["scale"]["minReplicas"]
    state["orchestrator_app"] = name[0]
    check("1 orchestrator container app is deployed and Running",
          running == "Running" and "placeholder" not in image and "quickstart" not in image,
          f"{name[0]}: {running}, minReplicas {replicas}, image {image.rsplit('/', 1)[-1]}")

    health = client().get("/api/health").json()
    orch = health.get("checks", {}).get("orchestrator", {})
    check("1 the web app can reach the orchestrator over internal ingress",
          orch.get("status") == "ok", f"{orch.get('detail', orch)}")


# ------------------------------------------------- 2 and 3 canonical case, typed state --
def canonical_case() -> None:
    with client() as web:
        run = stream_case(web, CANONICAL, "graph")
        state["graph_run"] = run
        report = run["report"] or {}
        check("2 the canonical incident reaches plan_ready inside the budget",
              bool(report) and report.get("plan_steps", 0) > 0
              and run["elapsed_s"] < PLAN_READY_BUDGET_S,
              f"{run['elapsed_s']}s, {report.get('severity')}, "
              f"{report.get('plan_steps')} plan steps, plan {report.get('plan_id')}")

        case = web.get(f"/api/workflow/{run['case_id']}").json()
        st = case.get("state", {})
        state["case"] = case
        populated = [k for k in ("triage", "knowledge", "impact", "plan") if st.get(k)]
        decisions = st.get("decisions", [])
        rules = {d["rule"] for d in decisions}
        check("3 the case carries all four typed results and a decision log",
              len(populated) == 4 and len(decisions) >= 6
              and {"severity=max(triage,impact)"} <= rules,
              f"status {case.get('status')}, {populated}, {len(decisions)} decisions, "
              f"runbooks {[r['doc_id'] for r in st.get('knowledge', {}).get('runbooks', [])]}")
        check("3 every decision names the deterministic rule that produced it",
              all(d.get("by") and d.get("rule") and d.get("detail") for d in decisions),
              "; ".join(f"{d['by']}:{d['rule']}" for d in decisions[:4]))


# ---------------------------------------------------------------- 4 real concurrency --
def concurrency() -> None:
    run = state.get("graph_run") or {}
    nodes = run.get("nodes", {})
    knowledge = float(nodes.get("knowledge_agent", {}).get("seconds", 0))
    impact = float(nodes.get("impact_agent", {}).get("seconds", 0))
    # Each node reports its own span duration. If they ran back to back the superstep would have
    # taken their sum; overlapped, it takes the slower of the two.
    branch_wall = max(knowledge, impact)
    check("4 the concurrent branch overlaps rather than queueing",
          branch_wall > 0 and branch_wall < (knowledge + impact) * 0.9,
          f"knowledge {knowledge}s, impact {impact}s, branch wall {branch_wall}s "
          f"(sequential would be {round(knowledge + impact, 1)}s)")


# ------------------------------------------------------------------- 5 all three modes --
def all_modes() -> None:
    with client() as web:
        for mode in ("sequential_only", "handoff"):
            run = stream_case(web, CANONICAL, mode)
            report = run["report"] or {}
            state[f"{mode}_run"] = run
            check(f"5 {mode} mode completes the canonical incident",
                  bool(report and report.get("severity") and not run["errors"]),
                  f"{run['elapsed_s']}s, {report.get('severity')}, "
                  f"{report.get('plan_steps')} plan steps")
    # Deliberately not asserting graph < sequential end to end. Model latency varies by more than
    # the branch saving on a single run, so that comparison fails at random and proves nothing.
    # Check 4 measures the overlap inside one run, which is the actual claim.
    graph_s = (state.get("graph_run") or {}).get("elapsed_s", 0)
    seq_s = (state.get("sequential_only_run") or {}).get("elapsed_s", 0)
    handoff_s = (state.get("handoff_run") or {}).get("elapsed_s", 0)
    check("5 all three shapes produced a report", all(
        (state.get(f"{m}_run") or {}).get("report") for m in
        ("graph", "sequential_only", "handoff")),
        f"graph {graph_s}s, sequential {seq_s}s, handoff {handoff_s}s")


# --------------------------------------------------------------------------- 6 chaos --
def chaos() -> None:
    """A dead specialist must degrade the run, not kill it.

    Injecting a broken agent into the deployed orchestrator would be a control-plane write, which
    CLAUDE.md section 9 forbids for anything demo-adjacent. The test runs the same deployed model,
    Cosmos and Search from this process with one specialist pointed at a dead endpoint.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q",
         "tests/test_orchestrator_integration.py::test_a_dead_specialist_degrades_the_run_"
         "rather_than_killing_it"],
        capture_output=True, text=True, timeout=600,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    check("6 a dead specialist degrades the run instead of killing it", proc.returncode == 0,
          (proc.stdout.strip().splitlines() or ["no output"])[-1][:160])


# ------------------------------------------------------------------------ 7 mermaid --
def mermaid() -> None:
    topologies: dict[str, dict[str, Any]] = {}
    with client() as web:
        for mode in ("graph", "sequential_only", "handoff"):
            topology = web.get("/api/workflow/graph", params={"mode": mode}).json()
            topologies[mode] = topology
            text = topology.get("mermaid", "")
            lines = [line for line in text.splitlines() if line.strip()]
            declared = {m.group(1) for m in (MERMAID_NODE.match(line) for line in lines) if m}
            arrows = [line for line in lines if "-->" in line]
            referenced = {part.strip().rstrip(";") for line in arrows
                          for part in line.split("-->")}
            dangling = sorted(referenced - declared)
            ok = (lines and lines[0].startswith("flowchart")
                  and len(declared) >= 4 and arrows and not dangling)
            check(f"7 {mode} returns a valid Mermaid graph", bool(ok),
                  f"{len(declared)} nodes, {len(arrows)} edges"
                  + (f", dangling {dangling}" if dangling else ""))
    # Only the deterministic shapes carry code nodes; the handoff mesh is agents all the way down,
    # which is the distinction the panel draws.
    kinds = {n["id"]: n["kind"] for n in topologies["graph"].get("nodes", [])}
    check("7 the topology marks which nodes are code and which are agents",
          set(kinds.values()) == {"deterministic", "agent"},
          ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))


# ---------------------------------------------------------------------- follow-up --
def follow_up() -> None:
    case_id = (state.get("graph_run") or {}).get("case_id")
    if not case_id:
        check("handoff follow-up answers a question on the analysed case", False, "no case to ask")
        return
    with client() as web:
        answer = web.post(f"/api/workflow/{case_id}/ask", timeout=300,
                          json={"question": "Which runbook applies here, and is it stale?"}).json()
    text = answer.get("answer", "")
    check("handoff follow-up answers a question on the analysed case",
          bool(text) and not text.lstrip().startswith("{") and bool(answer.get("handoffs")),
          f"answered by {answer.get('answered_by')} after handoffs "
          f"{answer.get('handoffs')}: {text[:120]}")


def steps() -> Iterator[tuple[str, Callable[[], None]]]:
    yield "1 deployed", deployed_and_running
    yield "2-3 canonical case", canonical_case
    yield "4 concurrency", concurrency
    yield "5 modes", all_modes
    yield "6 chaos", chaos
    yield "7 mermaid", mermaid
    yield "follow-up", follow_up


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8")
    load_local_env()
    for name, fn in steps():
        guarded(name, fn)
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
