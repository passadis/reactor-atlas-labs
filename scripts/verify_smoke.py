"""The smoke check: is ATLAS at its known-good baseline and serving? Read-only, about 30 s.

    uv run python -m scripts.verify_smoke
    ATLAS_SMOKE_FULL=1 uv run python -m scripts.verify_smoke   # + the canonical case end to end

1. the web app's /api/health is ok (it checks Cosmos, Search, the model and the orchestrator)
2. every regression-kit injection is at baseline
3. the instructions pointer is the baseline version
4. the hosted agent endpoint is pinned to the good version
5. the remediation dead-letter queue is empty
6. (full) the canonical incident, through the workflow, to a remediated case

Run by restore_baseline.py, the nightly workflow, and the T-10 routine.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import yaml
from atlas_common.config import load_local_env

from scripts.regression_kit import KIT

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


def health() -> None:
    r = httpx.get(os.environ["ATLAS_WEB_URL"].rstrip("/") + "/api/health", timeout=30)
    body = r.json() if r.status_code == 200 else {}
    bad = [k for k, v in (body.get("checks") or {}).items()
           if isinstance(v, dict) and v.get("status") != "ok"]
    check("1 web /api/health", r.status_code == 200 and body.get("status") == "ok",
          f"HTTP {r.status_code}, {body.get('status')}" + (f", degraded: {bad}" if bad else ""))


def injections() -> None:
    states = {name: inj.status() for name, inj in KIT.items()}
    off = {k: v for k, v in states.items() if v != "baseline"}
    check("2 every injection at baseline", not off, f"applied: {off}" if off else
          f"{len(states)} at baseline")


def pointer() -> None:
    import asyncio

    from atlas_agent.instructions import ACTIVE_BASELINE

    from scripts.regression_kit import _pointer

    active = asyncio.run(_pointer())
    check("3 instructions pointer at baseline", active == ACTIVE_BASELINE,
          f"{active} (baseline {ACTIVE_BASELINE})")


def pinned() -> None:
    good = str(yaml.safe_load((ROOT / "evals" / "agent_versions.yaml").read_text())["good"])
    out = subprocess.run([sys.executable, "-m", "scripts.hosted_agent", "status"], cwd=ROOT,
                         capture_output=True, text=True, timeout=180, check=True).stdout
    status = json.loads(out[out.index("{"):])
    check("4 hosted agent pinned to the good version", str(status.get("pinned")) == good,
          f"pinned {status.get('pinned')}, good {good}")


def dlq() -> None:
    from scripts.verify_phase11 import _dlq_depth

    depth = _dlq_depth()
    check("5 remediation DLQ empty", depth == 0, f"{depth} message(s)")


def full() -> None:
    from scripts.e2e_remediation import follow, to_gate, web
    from scripts.sse_probe import CANONICAL

    client = web()
    gate = to_gate(client, CANONICAL)
    r = client.post(f"/api/workflow/{gate['case_id']}/decision",
                    json={"decision": "approve", "comment": "smoke: canonical end to end"})
    doc = follow(client, gate["case_id"])["doc"] if r.status_code < 300 else {}
    rm = (doc or {}).get("remediation") or {}
    check("6 canonical case end to end", rm.get("status") == "succeeded",
          f"{gate['case_id']}: {doc.get('status')} / {rm.get('status')}")


def main() -> int:
    load_local_env()
    steps = [("1", health), ("2", injections), ("3", pointer), ("4", pinned), ("5", dlq)]
    if os.environ.get("ATLAS_SMOKE_FULL") == "1":
        steps.append(("6", full))
    for name, fn in steps:
        guarded(name, fn)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"smoke: {passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
