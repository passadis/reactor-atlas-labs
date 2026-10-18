"""Phase 13 verification: CI, the evaluate-only gate, rollback, the eval-required rule, nightly.

    uv run python -m scripts.verify_phase13

1. CI passed on the latest commit of the current branch (GitHub Actions)
2. the quality gate FAILS the bad hosted-agent version and PASSES the good one (the reports in
   evals/reports/, produced by `make gate` or the agent-quality-gate workflow)
3. canary traffic splitting - descoped (CLAUDE.md 7.12); reported, not counted
4. the latest rollback.yml run succeeded inside its 180 s budget
5. a pull request that edits instructions without a new evaluation fails CI
6. the latest nightly-restore run (apply every injection, restore, smoke) succeeded
7. right now: every injection at baseline and the smoke check green

Read-only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from atlas_common.config import load_local_env

ROOT = Path(__file__).resolve().parent.parent
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


def guarded(name: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except BaseException as exc:
        check(name, False, f"{type(exc).__name__}: {str(exc)[:200]}")


def gh(*args: str) -> Any:
    out = subprocess.run([shutil.which("gh") or "gh", *args], capture_output=True, text=True,
                         timeout=120, check=True).stdout
    return json.loads(out) if out.strip().startswith(("[", "{")) else out.strip()


def last_run(workflow: str, branch: str | None = None) -> dict[str, Any]:
    args = ["run", "list", "--workflow", workflow, "-L", "1", "--json",
            "conclusion,status,headSha,createdAt,updatedAt,databaseId,url"]
    if branch:
        args += ["--branch", branch]
    runs = gh(*args)
    return runs[0] if runs else {}


def ci() -> None:
    branch = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True,
                            check=True).stdout.strip()
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    run = last_run("ci.yml", branch)
    check("1 CI passed on the latest commit", run.get("conclusion") == "success"
          and run.get("headSha") == head,
          f"{branch}@{head[:8]}: run {run.get('databaseId')} {run.get('status')}/"
          f"{run.get('conclusion')} on {str(run.get('headSha'))[:8]}")


def gate() -> None:
    reports = ROOT / "evals" / "reports"
    good = (reports / "gate-good.md").read_text(encoding="utf-8").splitlines()[0] \
        if (reports / "gate-good.md").exists() else "missing"
    bad = (reports / "gate-bad.md").read_text(encoding="utf-8").splitlines()[0] \
        if (reports / "gate-bad.md").exists() else "missing"
    check("2 the gate fails the bad build and passes the good one",
          "PASS" in good and "FAIL" in bad, f"good: {good.strip('# ')}; bad: {bad.strip('# ')}")


def rollback() -> None:
    from datetime import datetime

    def ts(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    # Timed from the job's start: the time GitHub keeps a run queued before a runner picks it up
    # is outside our control (an Actions incident queued every run for 10+ minutes on
    # 2026-10-05), so it is reported, not counted. On stage, `make restore-baseline` is the
    # fallback when Actions is slow.
    run = last_run("rollback.yml")
    took = queued = None
    if run:
        job = gh("run", "view", str(run["databaseId"]), "--json", "jobs")["jobs"][0]
        if job.get("startedAt") and job.get("completedAt"):
            took = (ts(job["completedAt"]) - ts(job["startedAt"])).total_seconds()
            queued = (ts(job["startedAt"]) - ts(run["createdAt"])).total_seconds()
    check("4 rollback.yml succeeded inside 180 s",
          run.get("conclusion") == "success" and took is not None and took < 180,
          f"run {run.get('databaseId')}: {run.get('conclusion')}, job {took and round(took)} s "
          f"(runner start included; {queued and round(queued)} s queued before it, not counted)")


def eval_required() -> None:
    prs = gh("pr", "list", "--state", "all", "--search", "verify-phase13 eval-required in:title",
             "--json", "number,title,statusCheckRollup", "-L", "5")
    pr = prs[0] if prs else {}
    checks = pr.get("statusCheckRollup") or []
    ci_check = next((c for c in checks if c.get("name") == "checks"), {})
    check("5 a PR editing instructions without an eval fails CI",
          ci_check.get("conclusion") == "FAILURE",
          f"PR #{pr.get('number')}: CI {ci_check.get('conclusion')}" if pr else "no synthetic PR")


def nightly() -> None:
    run = last_run("nightly-restore.yml")
    check("6 nightly apply-then-restore succeeded", run.get("conclusion") == "success",
          f"run {run.get('databaseId')} {run.get('createdAt')}: {run.get('conclusion')}")


def now() -> None:
    smoke = subprocess.run([shutil.which("bash") or "bash", "scripts/verify_smoke.sh"],
                           cwd=ROOT, capture_output=True, text=True, timeout=600, check=False)
    tail = (smoke.stdout.strip().splitlines() or ["no output"])[-1]
    check("7 baseline restored and smoke green now", smoke.returncode == 0, tail)


def main() -> int:
    load_local_env()
    for name, fn in [("1", ci), ("2", gate), ("4", rollback), ("5", eval_required),
                     ("6", nightly), ("7", now)]:
        guarded(name, fn)
    print("SKIP  3 canary traffic splitting - descoped (CLAUDE.md 7.12); rollback.yml instead")
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
