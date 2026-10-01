"""Phase 07 verification: typed state, checkpoints, the policy gate, approval and recovery.

Against the deployed orchestrator, through the web app's proxy - the same door the demo uses.

Check 2 scales the orchestrator to zero and back, which takes several minutes and is a
control-plane write. It is the Episode 2 set piece, so it is verified rather than assumed; set
`ATLAS_SKIP_KILL=1` for a fast run that skips only that step and says so.

Writes application data (cases, approvals) and, in check 2 only, changes replica counts. Exit
non-zero on any failure. Wrapped by scripts/verify_phase07.sh for `make verify-all`.
"""

from __future__ import annotations

import json
import os
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

GATE_BUDGET_S = 120.0
RESUME_BUDGET_S = 30.0
MIN_CHECKPOINTS = 5


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


def guarded(name: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except BaseException as exc:
        check(name, False, f"{type(exc).__name__}: {str(exc)[:300]}")


def client(timeout: float = 120.0) -> httpx.Client:
    url = os.environ["ATLAS_WEB_URL"].rstrip("/")
    return httpx.Client(base_url=url, headers={"Authorization": f"Bearer {cli_token()}"},
                        timeout=timeout)


def run_to_gate(web: httpx.Client, message: str = CANONICAL) -> dict[str, Any]:
    """Start a case and follow it until it parks on a human."""
    started = time.perf_counter()
    case_id = str(web.post("/api/workflow", json={"message": message, "mode": "graph"})
                  .raise_for_status().json()["case_id"])
    parked: dict[str, Any] = {}
    nodes: list[str] = []
    event = "message"
    with web.stream("GET", f"/api/workflow/{case_id}/stream", timeout=GATE_BUDGET_S * 2) as stream:
        for line in stream.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                payload = json.loads(line.split(":", 1)[1].strip() or "{}")
                if event == "node":
                    nodes.append(payload["node"])
                elif event == "approval_required":
                    parked = payload["data"]
                    break
                elif event in ("report", "end"):
                    break
    return {"case_id": case_id, "parked": parked, "nodes": nodes,
            "elapsed_s": round(time.perf_counter() - started, 1)}


# ----------------------------------------------- 1 the case parks, and it is durable --
def parks_and_checkpoints() -> None:
    with client() as web:
        run = run_to_gate(web)
        state["run"] = run
        parked = run["parked"]
        check("1 the canonical incident reaches awaiting_approval",
              bool(parked) and bool(parked.get("required_roles")),
              f"{run['elapsed_s']}s, roles {parked.get('required_roles')}, "
              f"plan {parked.get('plan_id')}, {len(parked.get('reasons', []))} reasons")

        detail = web.get(f"/api/workflow/{run['case_id']}").json()
        policy = ((detail.get("state") or {}).get("policy") or {})
        check("1 the case is stored as awaiting_approval with its policy decision",
              detail.get("status") == "awaiting_approval" and bool(policy),
              f"status {detail.get('status')}, {len(policy.get('reasons', []))} policy reasons")

        cps = web.get(f"/api/workflow/{run['case_id']}/checkpoints").json()
        state["checkpoints"] = cps
        rows = cps.get("checkpoints", [])
        parked_cps = [c for c in rows if c.get("pending_requests")]
        newest = max((c["iteration"] for c in rows), default=-1)
        check("1 a checkpoint in Cosmos holds the pending approval",
              cps.get("storage") == "cosmos" and bool(parked_cps),
              f"{cps.get('count')} checkpoints, {len(parked_cps)} parked, newest iter {newest}")


# ----------------------------------------------------------------- 3 the listing --
def checkpoint_listing() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.inspect_checkpoints",
         "--case", str(state.get("run", {}).get("case_id", ""))],
        capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    parked_lines = [line for line in lines if "PARKED ON A HUMAN" in line]
    rows = [line for line in lines if line.strip().split(" ")[0].isdigit()]
    check(f"3 inspect_checkpoints lists at least {MIN_CHECKPOINTS} checkpoints with the approval",
          proc.returncode == 0 and len(rows) >= MIN_CHECKPOINTS and bool(parked_lines),
          f"{len(rows)} rows, {len(parked_lines)} parked")
    # Every checkpoint must agree on the graph, or a restore into another replica would be
    # refused. The signature is the one column that is 12 hex characters.
    hashes = {
        token for line in rows for token in line.split()
        if len(token) == 12 and all(c in "0123456789abcdef" for c in token)
    }
    check("3 every checkpoint shares one graph signature", len(hashes) <= 1,
          f"signatures {sorted(hashes)}")


# ----------------------------------------------------- 4 the gate cannot be talked round --
def policy_invariant() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_policy.py",
         "-k", "invariant or auto_approval or frozen or break_glass or blocked"],
        capture_output=True, text=True, timeout=600,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    check("4 no destructive production action is ever auto-approved (1000-case property test)",
          proc.returncode == 0,
          (proc.stdout.strip().splitlines() or ["no output"])[-1][:160])


# ------------------------------------------------- 5 and 6 who may answer, and for what --
def approval_is_guarded() -> None:
    """Whether the signed-in persona may approve depends on the plan, so assert the RULE.

    The gate names its approvers from the entitlement table: a break-glass case needs a director,
    an ordinary Tier-1 case accepts a manager too. Asserting one branch made this check depend on
    what the planner happened to produce, and it failed roughly one run in five for a system that
    was behaving correctly.
    """
    case_id = state.get("run", {}).get("case_id")
    if not case_id:
        check("5 the approval rule is enforced against the signed-in persona", False,
              "no parked case")
        return

    with client() as web:
        identity = web.get("/api/me").json()
        persona = str((identity.get("persona") or {}).get("role") or "")
        pending = web.get("/api/workflow/approvals").json().get("pending", [])
        row = next((p for p in pending if p.get("case_id") == case_id), {})
        required = list(row.get("required_roles") or [])
        entitled = persona in required

        response = web.post(f"/api/workflow/{case_id}/decision", json={
            "decision": "approve",
            "comment": "Verification: exercising the approval rule end to end."})

    if entitled:
        check("5 the approval rule is enforced against the signed-in persona",
              response.status_code == 200,
              f"persona '{persona}' IS in {required}, so approval was accepted "
              f"(HTTP {response.status_code})")
        # A warm approval returns as soon as the answer joins the live run; the run resumes a
        # moment later. Since Phase 10 the presenter is a director, so this path now runs - poll.
        settled: dict[str, Any] = {}
        for _ in range(15):
            settled = _orchestrator_case(case_id) or {}
            if settled.get("status") not in (None, "awaiting_approval"):
                break
            time.sleep(2)
        check("5 an accepted approval advances the case",
              settled.get("status") in ("approved", "executing", "completed"),
              f"status {settled.get('status')}")
    else:
        check("5 the approval rule is enforced against the signed-in persona",
              response.status_code == 403,
              f"persona '{persona}' is NOT in {required}, so approval was refused "
              f"(HTTP {response.status_code})")
        audit = _orchestrator_case(case_id) or {}
        check("5 a refused approval leaves the case parked",
              audit.get("status") == "awaiting_approval", f"status {audit.get('status')}")

    state["case_consumed"] = entitled

    # Independent of whatever the planner produced: a readonly persona may never approve, and the
    # operator path takes an explicit role, so this branch is deterministic on every run.
    refusal = subprocess.run(
        [sys.executable, "-m", "scripts.resume_case", case_id, "--approve",
         "--role", "readonly", "--by", "guest@contoso.com",
         "--comment", "Verification: a readonly persona must never be able to approve."],
        capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    combined = refusal.stdout + refusal.stderr
    check("5 a readonly persona can never approve, whatever the plan",
          '"approved"' not in combined or "refus" in combined.lower()
          or refusal.returncode != 0,
          f"exit {refusal.returncode}")


def _orchestrator_case(case_id: str) -> dict[str, Any] | None:
    with client() as web:
        response = web.get(f"/api/workflow/{case_id}")
        return dict(response.json()) if response.status_code == 200 else None


def superseded_plan_is_refused() -> None:
    """An approval is for one plan. Change the plan and the approval is void."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_policy.py",
         "-k", "superseded or expired or wrong_role or blocked_plan"],
        capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    check("6 expired, out-of-role and superseded-plan approvals are each refused distinctly",
          proc.returncode == 0,
          (proc.stdout.strip().splitlines() or ["no output"])[-1][:160])


# ------------------------------------------------------ 2 kill the orchestrator, resume --
def kill_and_resume() -> None:
    case_id = state.get("run", {}).get("case_id")
    if not case_id:
        check("2 kill/resume", False, "no parked case to resume")
        return
    if state.get("case_consumed"):
        # Check 5's persona was entitled and approved the case, so there is nothing parked to
        # resume. Park a fresh one rather than assert against a completed case.
        with client() as web:
            run = run_to_gate(web)
        state["run"] = run
        case_id = run["case_id"]
        if not run["parked"]:
            check("2 kill/resume", False, "could not park a fresh case for the kill test")
            return

    if os.environ.get("ATLAS_SKIP_KILL"):
        check("2 kill/resume across an orchestrator restart", True,
              "SKIPPED by ATLAS_SKIP_KILL; run `make verify-phase07` without it before an episode")
        return

    started = time.perf_counter()
    # Resolve bash explicitly: on Windows it is Git Bash and is not always on the PATH that a
    # `uv run` subprocess inherits, which shows up as an instant failure with no output at all.
    bash = shutil.which("bash") or "bash"
    proc = subprocess.run([bash, "scripts/kill_orchestrator.sh"],
                          capture_output=True, text=True, timeout=900)
    killed_s = round(time.perf_counter() - started, 1)
    detail = (proc.stdout.strip().splitlines() or [proc.stderr.strip()[:120] or "no output"])[-1]
    check("2 every orchestrator replica was terminated and came back", proc.returncode == 0,
          f"{killed_s}s | {detail[:140]}")
    state["kill_wall_clock_s"] = killed_s

    # The replicas that held the case are gone. Approve through the operator path, which rebuilds
    # the workflow from `node_ids` and resumes from the checkpoint - the same thing the
    # orchestrator does on the cold path, and the same script the runbook tells you to use.
    #
    # Not the web proxy: that approves as the *signed-in* persona, and a break-glass case needs a
    # director. The operator path names the role explicitly, which is what an operator has.
    resume_started = time.perf_counter()
    resume = subprocess.run(
        [sys.executable, "-m", "scripts.resume_case", case_id, "--approve",
         "--role", "director", "--by", "dana@contoso.com",
         "--comment", "Break-glass: approved after the orchestrator restart."],
        capture_output=True, text=True, timeout=600,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    resume_s = round(time.perf_counter() - resume_started, 1)
    try:
        body = json.loads(resume.stdout[resume.stdout.index("{"):])
    except (ValueError, json.JSONDecodeError):
        body = {}
    check("2 a case parked before the restart is approved and completed afterwards",
          resume.returncode == 0 and body.get("status") == "approved",
          f"{resume_s}s, path {body.get('path')}, status {body.get('status')}, "
          f"checkpoint {str(body.get('checkpoint_id'))[:8]}, exit {resume.returncode}, "
          f"still_suspended {body.get('still_suspended')}"
          + (f" | {(resume.stderr.strip() or resume.stdout.strip())[-200:]}"
             if resume.returncode else ""))

    settled = _orchestrator_case(case_id) or {}
    check("2 the resumed case reached approved",
          settled.get("status") in ("approved", "executing", "completed"),
          f"status {settled.get('status')}")

    # A cold resume is a checkpoint restore plus a report. If it took anything like as long as the
    # original analysis, the agents ran again and the claim is false.
    resumed_in = float(body.get("seconds", resume_s))
    original = float(state.get("run", {}).get("elapsed_s") or 0)
    check("2 the resume did not re-run the agents",
          resumed_in < RESUME_BUDGET_S and (original == 0 or resumed_in < original / 2),
          f"resume {resumed_in}s against {original}s for the original analysis")


def steps() -> Iterator[tuple[str, Callable[[], None]]]:
    yield "1 parks and checkpoints", parks_and_checkpoints
    yield "3 checkpoint listing", checkpoint_listing
    yield "4 policy invariant", policy_invariant
    yield "5 approval guarded", approval_is_guarded
    yield "6 superseded approval", superseded_plan_is_refused
    yield "2 kill and resume", kill_and_resume


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8")
    load_local_env()
    for name, fn in steps():
        guarded(name, fn)
    failed = [n for n, ok, _ in results if not ok]
    if state.get("kill_wall_clock_s"):
        print(f"\nkill/resume wall clock: {state['kill_wall_clock_s']}s "
              f"(record it in docs/DEMO-NOTES.md, CLAUDE.md 9.6)")
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
