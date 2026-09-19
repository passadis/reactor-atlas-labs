"""Phase 03 verification: the incident agent against the deployed model, Cosmos, Search, Foundry.

Every check runs against live resources. The demo scripts run as separate processes, exactly as
they run on stage, so a check that passes here is the thing the presenter will type.

Writes: one demo session document in Cosmos (TTL 24 h, then removed by this script) and, only if
the definition drifted, one new prompt-agent version. Nothing else.

Exits non-zero on any failure. Wrapped by scripts/verify_phase03.sh for `make verify-all`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

from atlas_agent.factory import IncidentAgentRuntime
from atlas_agent.instructions import KNOWN_VERSIONS, packaged_text
from atlas_agent.tools import NotFound
from atlas_common.config import Settings

PY = [sys.executable]
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


def run(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [*PY, *args], capture_output=True, text=True, encoding="utf-8", timeout=timeout, env=env
    )


def run_json(args: list[str], timeout: int) -> dict[str, Any]:
    proc = run(args, timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-600:] or f"exit {proc.returncode}")
    return dict(json.loads(proc.stdout))


def guarded(name: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception as exc:
        check(name, False, f"{type(exc).__name__}: {str(exc)[:300]}")


# ------------------------------------------------------------------------ 1-2 live wiring --


async def instructions_and_tools() -> None:
    async with IncidentAgentRuntime(Settings.from_env()) as rt:
        for version in KNOWN_VERSIONS:
            loaded = await rt.instructions.load(version)
            same = loaded.text == packaged_text(version)
            check(f"1 instructions {version} served from Cosmos, identical to git",
                  loaded.source == "cosmos" and same, f"source={loaded.source} same={same}")
        active = await rt.instructions.active_version()
        check("1 active instruction pointer is v1 (baseline)", active == "v1", active)

        t = rt.tools
        changes = await t.get_recent_changes("svc-payments", 240)
        ids = [c.change_id for c in getattr(changes, "changes", [])]
        check("2 get_recent_changes finds only CHG-2026-0873", ids == ["CHG-2026-0873"], str(ids))

        tele = await t.get_telemetry("svc-checkout-api", 60)
        onset = getattr(tele, "first_anomaly_at", None)
        check("2 get_telemetry anomaly onset 14:05",
              onset is not None and onset.strftime("%H:%M") == "14:05", str(onset))

        kb = await t.search_knowledge("checkout latency after payments deploy", "runbooks",
                                      ["svc-checkout-api", "svc-payments"], 5)
        docs = getattr(kb, "documents", [])
        top = [d.doc_id for d in docs]
        full = all(len(d.text) > 600 for d in docs[:3])
        check("2 search_knowledge returns RB-0042 with full chunk text",
              "RB-0042" in top[:3] and full, f"{top} full={full}")

        br = await t.compute_blast_radius("svc-payments", 3)
        affected = getattr(br, "affected_service_ids", [])
        check("2 compute_blast_radius(svc-payments) reaches svc-checkout-api",
              "svc-checkout-api" in affected, f"{len(affected)} services")

        nf = await t.lookup_service("search")
        check("2 unknown service -> NotFound with did_you_mean",
              isinstance(nf, NotFound) and "svc-search" in nf.did_you_mean,
              str(getattr(nf, "did_you_mean", nf)))


# --------------------------------------------------------------------------- 3 the demo --

state: dict[str, Any] = {}


def canonical_demo() -> None:
    started = time.perf_counter()
    report = run_json(["-m", "scripts.demo_agent_local", "--json"], timeout=300)
    seconds = time.perf_counter() - started
    t = report.get("triage", {})
    state["session_id"] = report.get("session_id")
    state["trace_id"] = report.get("trace_id")
    check("3 canonical triage is a validated IncidentTriage", t.get("kind") != "triage_failure",
          f"{report.get('triage_seconds')}s, {len(report.get('tool_calls', []))} tool calls, "
          f"{report.get('tokens', {}).get('input')} input tokens")
    check("3 severity Sev2 (rubric row)", t.get("severity") == "Sev2", str(t.get("severity")))
    check("3 checkout affected, CHG-2026-0873 suspected",
          "svc-checkout-api" in t.get("affected_services", [])
          and "CHG-2026-0873" in t.get("suspected_change_ids", []))
    cited = {c["doc_id"] for c in t.get("citations", [])}
    check("3 >= 2 citations including RB-0042", "RB-0042" in cited and len(cited) >= 2,
          str(sorted(cited)))
    follow = str(report.get("follow_up", "")).lower()
    check("3 follow-up resolves 'the service that caused this'",
          "team-payments" in follow and "freeze" in follow, follow[:120])
    check("3 whole demo under 120 s wall clock", seconds < 120, f"{seconds:.0f}s")


def resume_in_new_process() -> None:
    sid = state.get("session_id")
    if not sid:
        check("3 session resumes in a fresh process", False, "no session id from the demo")
        return
    report = run_json(["-m", "scripts.demo_agent_local", "--json", "--resume", sid,
                       "--follow-up", "Which change id did you blame? Answer with the id only."],
                      timeout=180)
    answer = str(report.get("follow_up", ""))
    check("3 session resumes from Cosmos in a fresh process", "CHG-2026-0873" in answer,
          answer[:80])


# -------------------------------------------------------------------- 4 raw model contrast --


def raw_model_contrast() -> None:
    report = run_json(["-m", "scripts.demo_raw_model", "--json"], timeout=120)
    named = report.get("grounded_facts_named", [])
    check("4 bare model knows none of the ATLAS facts", named == [], str(named))


# ----------------------------------------------------------------------- 5 prompt agent --


def prompt_agent() -> None:
    proc = run(["-m", "scripts.create_prompt_agent", "--check"], timeout=120)
    check("5 Foundry prompt agent atlas-triage-prompt matches its definition",
          proc.returncode == 0, (proc.stdout or proc.stderr).strip()[-200:])
    proc = run(["-m", "scripts.create_prompt_agent", "--ask",
                "Which runbook covers elevated p95 latency on the Checkout API after a "
                "deployment? Give the runbook id."],
               timeout=180)
    # The tool never exposes `doc_id`; runbook titles lead with the id so the model can cite it.
    ok = proc.returncode == 0 and "RB-0042" in proc.stdout
    detail = proc.stdout.strip()[-160:] if ok else (proc.stderr.strip()[-300:] or proc.stdout)
    check("5 prompt agent answers grounded in the index via the aisearch connection", ok, detail)


# --------------------------------------------------------------------- 6 tests, guardrails --


def pytest_suites() -> None:
    proc = subprocess.run(
        [*PY, "-m", "pytest", "-p", "no:cacheprovider", "tests/test_agent_core.py",
         "tests/test_telemetry.py", "tests/test_guardrails.py"],
        capture_output=True, text=True, encoding="utf-8", timeout=420,
    )
    last = (proc.stdout.strip().splitlines() or ["no output"])[-1]
    check("6 agent unit, telemetry and live guardrail tests", proc.returncode == 0, last)


# ------------------------------------------------------------------------- 7 telemetry --


def trace_exported() -> None:
    tid = state.get("trace_id")
    app, rg = os.environ.get("ATLAS_APPINSIGHTS_NAME"), os.environ.get("AZURE_RESOURCE_GROUP")
    if not (tid and app and rg):
        check("7 demo trace reached Application Insights", False, "no trace id or app name")
        return
    query = (f"union requests, dependencies | where timestamp > ago(2h) "
             f"and operation_Id == '{tid}' | count")
    az = shutil.which("az") or "az"  # az.cmd on Windows; no shell, the KQL contains pipes
    deadline = time.monotonic() + 420
    count = 0
    while time.monotonic() < deadline:
        proc = subprocess.run(
            [az, "monitor", "app-insights", "query", "--app", app, "-g", rg,
             "--analytics-query", query, "--query", "tables[0].rows[0][0]", "-o", "tsv"],
            capture_output=True, text=True, timeout=90,
        )
        count = int(proc.stdout.strip() or 0) if proc.returncode == 0 else 0
        if count:
            break
        time.sleep(30)
    check("7 demo trace reached Application Insights", count > 0, f"{count} spans for {tid}")


def cleanup() -> None:
    sid = state.get("session_id")
    if not sid:
        return

    async def expire() -> None:
        async with IncidentAgentRuntime(Settings.from_env()) as rt:
            await rt.sessions.expire(sid)

    asyncio.run(expire())


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8")
    from atlas_common.config import load_local_env

    load_local_env()
    guarded("1-2 live wiring", lambda: asyncio.run(instructions_and_tools()))
    guarded("3 canonical demo", canonical_demo)
    guarded("3 resume", resume_in_new_process)
    guarded("4 raw model", raw_model_contrast)
    guarded("5 prompt agent", prompt_agent)
    guarded("6 pytest", pytest_suites)
    guarded("7 telemetry", trace_exported)
    guarded("cleanup", cleanup)

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
