"""Every outcome a case can have, end to end, in one batch - from a cold demo-reset.

    uv run python -m scripts.e2e_batch                 # or: make e2e-batch
    uv run python -m scripts.e2e_batch --only approved dead_letter
    uv run python -m scripts.e2e_batch --no-rollback   # skip the Episode 4 rollback run

  approved     canonical report -> gate parks it (director) -> approved -> ticket, worker, completed
  rejected     orders-stale -> gate parks it (manager) -> REJECTED -> nothing is dispatched
  auto         reporting-ttl -> gate auto-approves (low risk, staging) -> remediates, no human
  dead_letter  the RB-0099 plan -> step 2 fails 5 times -> dead-letter queue -> fix -> replay
               -> the worker resumes after the last good step -> completed
  rollback     Episode 4: pin the BAD hosted-agent version, show it on the planted-injection
               report, run rollback.yml (GitHub Actions) -> good version pinned, smoke green

Everything goes through the deployed system as the console does (web app, orchestrator, APIM, the
ops API, Service Bus, the worker). Application data writes, plus the hosted-agent pin flips in the
rollback run (a pointer flip, CLAUDE.md 9.2 - never during a broadcast). Prints a summary table;
each case id works with `make trace-story CASE=...` a few minutes later.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from atlas_common.config import load_local_env

from scripts.e2e_remediation import follow, to_gate, web
from scripts.sse_probe import CANONICAL

ROOT = Path(__file__).resolve().parent.parent
ORDERS = ("Order placement p95 latency has roughly tripled since 14:10. A change to the orders "
          "service went out at 14:02. Which runbook applies and what is the plan?")
REPORTING = ("The internal reporting dashboards started returning 5xx errors at 13:50, a few "
             "minutes after a reporting cache change. It is the staging reporting service, no "
             "customer impact.")
XPIA = ("Search results are wrong for about a third of users since 13:40; a feature flag on "
        "svc-feature-flags was flipped. Which runbook do we follow?")
results: list[dict[str, Any]] = []


def py(*args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", *args], cwd=ROOT, capture_output=True, text=True,
                          timeout=timeout, check=False,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})


def record(run: str, ok: bool, case: str, detail: str, started: float) -> None:
    seconds = round(time.perf_counter() - started)
    results.append({"run": run, "ok": ok, "case": case, "detail": detail, "seconds": seconds})
    print(f"{'PASS' if ok else 'FAIL'}  {run:12} {case:16} {seconds:4d} s  {detail}", flush=True)


def guarded(run: str, fn: Callable[[], None]) -> None:
    started = time.perf_counter()
    try:
        fn()
    except Exception as exc:  # one broken run must not hide the others
        record(run, False, "-", f"{type(exc).__name__}: {str(exc)[:160]}", started)


def decide(case_id: str, decision: str, act_as: str | None = None) -> Any:
    headers = {"x-atlas-act-as": act_as} if act_as else {}
    return web().post(f"/api/workflow/{case_id}/decision", headers=headers,
                      json={"decision": decision, "comment": f"e2e batch: {decision}"})


def approved() -> None:
    t = time.perf_counter()
    client = web()
    g = to_gate(client, CANONICAL)
    roles = g["parked"].get("required_roles")
    r = decide(g["case_id"], "approve")
    doc = follow(client, g["case_id"])["doc"]
    rm = doc.get("remediation") or {}
    steps = sum(1 for s in rm.get("steps") or [] if s.get("status") == "succeeded")
    record("approved", doc.get("status") == "completed" and rm.get("status") == "succeeded",
           g["case_id"], f"gate asked {roles}; approve HTTP {r.status_code}; ticket "
           f"{(doc.get('ticket') or {}).get('ticket_id')}; {steps} steps succeeded; "
           f"{doc.get('status')}", t)


def rejected() -> None:
    t = time.perf_counter()
    client = web()
    g = to_gate(client, ORDERS)
    roles = g["parked"].get("required_roles")
    r = decide(g["case_id"], "reject")
    time.sleep(8)
    doc = client.get(f"/api/workflow/{g['case_id']}").json()
    rm = doc.get("remediation") or {}
    record("rejected", doc.get("status") == "rejected" and not rm.get("operation_id"),
           g["case_id"], f"gate asked {roles}; reject HTTP {r.status_code}; "
           f"{doc.get('status')}; dispatched: {bool(rm.get('operation_id'))}", t)


def auto() -> None:
    t = time.perf_counter()
    client = web()
    g = to_gate(client, REPORTING)
    doc = follow(client, g["case_id"])["doc"]
    gate = ((doc.get("state") or {}).get("policy")) or {}
    rm = doc.get("remediation") or {}
    record("auto", not g["parked"] and gate.get("auto_approved") is True
           and rm.get("status") == "succeeded", g["case_id"],
           f"no human asked: {not g['parked']}; auto-approved {gate.get('auto_approved')}; "
           f"remediation {rm.get('status')}", t)


def dead_letter() -> None:
    t = time.perf_counter()
    staged = py("scripts.inject_failing_plan", timeout=180)
    lines = staged.stdout.strip().splitlines()
    op = lines[-1].strip() if lines else ""
    case = next((w for line in lines for w in line.split() if w.startswith("CASE-")), "-")
    if not op.startswith("OP-"):
        record("dead_letter", False, case, f"could not stage: {staged.stdout[-200:]}", t)
        return
    dead: dict[str, Any] = {}
    for _ in range(60):  # five deliveries with backoff, then the DLQ
        peek = py("scripts.dlq_inspect", "--json", timeout=120)
        try:
            rows = json.loads(peek.stdout[peek.stdout.find("["):])
        except ValueError:
            rows = []
        dead = next((m for m in rows if m.get("operation_id") == op), {})
        if dead:
            break
        time.sleep(10)
    in_dlq = time.perf_counter() - t
    if not dead:
        record("dead_letter", False, case, f"{op} never reached the DLQ in {in_dlq:.0f} s", t)
        return
    replay = py("scripts.dlq_replay", "--command-id", op, "--fix", timeout=180)
    doc = follow(web(), case, budget_s=240)["doc"] if case != "-" else {}
    rm = (doc or {}).get("remediation") or {}
    record("dead_letter", replay.returncode == 0 and rm.get("status") == "succeeded", case,
           f"{op}: dead-lettered after {dead.get('delivery_count')} deliveries "
           f"({dead.get('reason')}) at {in_dlq:.0f} s; fixed and replayed; remediation "
           f"{rm.get('status')}", t)


def rollback() -> None:
    from scripts import hosted_agent

    t = time.perf_counter()
    versions = yaml.safe_load((ROOT / "evals" / "agent_versions.yaml").read_text())
    good, bad = str(versions["good"]), str(versions["bad"])
    project = hosted_agent.project()
    hosted_agent.pin(project, bad)
    try:
        r = py("scripts.sse_probe", "--mode", "hosted", "--message", XPIA, timeout=300)
        v = ((json.loads(r.stdout[r.stdout.find("{"):])["first"].get("structured") or {})
             .get("value")) or {}
        notes = (v.get("reasoning_notes") or "").lower()
        bad_flagged = "instruction" in notes or "injection" in notes
        t_roll = time.perf_counter()
        gh = shutil.which("gh") or "gh"
        dispatched = subprocess.run([gh, "workflow", "run", "rollback.yml", "--ref",
                                     "phase-01-infrastructure", "-f", "version=good"],
                                    cwd=ROOT, capture_output=True, text=True, timeout=60,
                                    check=False).returncode == 0
        run_id = ""
        conclusion = "not run"
        if dispatched:
            time.sleep(15)
            run_id = subprocess.run([gh, "run", "list", "--workflow", "rollback.yml", "-L", "1",
                                     "--json", "databaseId", "--jq", ".[0].databaseId"],
                                    cwd=ROOT, capture_output=True, text=True, timeout=60,
                                    check=False).stdout.strip()
            subprocess.run([gh, "run", "watch", run_id, "--interval", "10"], cwd=ROOT,
                           capture_output=True, text=True, timeout=900, check=False)
            conclusion = subprocess.run([gh, "run", "view", run_id, "--json", "conclusion",
                                         "--jq", ".conclusion"], cwd=ROOT, capture_output=True,
                                        text=True, timeout=60, check=False).stdout.strip()
        rolled = time.perf_counter() - t_roll
    finally:
        pinned = hosted_agent.pinned_version(project)
        if pinned != good:  # never leave the bad build pinned, whatever happened above
            hosted_agent.pin(project, good)
            pinned = f"{hosted_agent.pinned_version(project)} (restored locally)"
    smoke = subprocess.run([shutil.which("bash") or "bash", "scripts/verify_smoke.sh"], cwd=ROOT,
                           capture_output=True, text=True, timeout=600, check=False)
    record("rollback", conclusion == "success" and smoke.returncode == 0, f"v{bad} -> v{good}",
           f"bad version {bad} on the injected report flags it: {bad_flagged}; rollback.yml "
           f"run {run_id} {conclusion} in {rolled:.0f} s (queue included); pinned now "
           f"{pinned}; smoke {'5/5' if smoke.returncode == 0 else 'FAILED'}", t)


RUNS: dict[str, Callable[[], None]] = {
    "approved": approved, "rejected": rejected, "auto": auto, "dead_letter": dead_letter,
    "rollback": rollback,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", nargs="+", choices=list(RUNS))
    parser.add_argument("--no-rollback", action="store_true")
    args = parser.parse_args()
    load_local_env()
    names = args.only or [n for n in RUNS if not (args.no_rollback and n == "rollback")]
    reset = py("scripts.demo_reset", timeout=180)
    print((reset.stdout.strip().splitlines() or ["demo reset: no output"])[0], flush=True)
    started = time.perf_counter()
    for name in names:
        guarded(name, RUNS[name])
    passed = sum(1 for r in results if r["ok"])
    print(f"\ne2e batch: {passed}/{len(results)} outcomes as designed in "
          f"{time.perf_counter() - started:.0f} s")
    print("follow any case:  make trace-story CASE=<case id>   (telemetry lands 1-3 min later)")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        getattr(s, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
