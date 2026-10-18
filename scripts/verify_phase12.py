"""Phase 12 verification: evaluation, red teaming, and the instruction-version comparison.

    uv run python -m scripts.verify_phase12
    ATLAS_EVAL_LIVE=1 uv run python -m scripts.verify_phase12   # also a fresh 4-case live run

1. the evaluation pipeline works: the latest full run of the ACTIVE instructions has a scorecard
   (and with ATLAS_EVAL_LIVE=1, a fresh harness run + local checks completes now)
2. that scorecard passes every threshold in evals/thresholds.yaml
3. the local checks run in under 60 s and catch every deliberately broken fixture
4. the latest red-team scan completed with 0 % prohibited-action ASR
5. evals/reports/instructions-comparison.md exists and shows the active version ahead of v0
6. the eval.run event is queryable by the Phase 11 workbook's Quality section

Full evaluations take 30+ minutes of judge time, so this verifies the recorded runs (`make eval`
produces them) rather than repeating them. Read-only apart from the optional live smoke.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from atlas_common.config import load_local_env

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "evals" / "results"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


def guarded(name: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except BaseException as exc:
        check(name, False, f"{type(exc).__name__}: {str(exc)[:300]}")


def active_version() -> str:
    import asyncio

    from atlas_common.config import Settings
    from atlas_common.repositories.base import CosmosStore

    async def read() -> str:
        async with CosmosStore(Settings.from_env()) as store:
            rows = await store.query("agent-instructions", "SELECT * FROM c WHERE c.id = @id",
                                     [{"name": "@id", "value": "atlas-incident-agent:active"}])
            return str(rows[0]["version"]) if rows else "v1"
    return asyncio.run(read())


def latest_card(version: str, dataset: str = "v1") -> dict[str, Any] | None:
    cards = []
    for path in RESULTS.glob("*/scorecard.json"):
        card = json.loads(path.read_text(encoding="utf-8"))
        if card.get("instructions_version") == version and card.get("dataset") == dataset \
                and card.get("mode") == "dataset":
            cards.append((path.stat().st_mtime, card))
    return max(cards, key=lambda c: c[0])[1] if cards else None


def pipeline() -> None:
    version = active_version()
    card = latest_card(version)
    ctx["card"], ctx["version"] = card, version
    detail = (f"active {version}: run {card['run_id']}, composite {card['composite']}, "
              f"{card['local']['cases']} cases") if card else f"no scorecard for active {version}"
    if os.environ.get("ATLAS_EVAL_LIVE") == "1":
        run_id = f"verify-{time.strftime('%Y%m%d-%H%M%S')}"
        live = subprocess.run([sys.executable, "-m", "evals.harness", "--run-id", run_id,
                               "--only", "eval-normal-live-01", "eval-toolfail-01",
                               "eval-inject-indirect-01", "eval-safety-01"],
                              capture_output=True, text=True, timeout=900)
        checked = subprocess.run([sys.executable, "-m", "evals.local_checks",
                                  str(RESULTS / run_id / "responses.jsonl")],
                                 capture_output=True, text=True, timeout=120)
        detail += f"; live smoke: {checked.stdout.strip().splitlines()[-1]}"
        card = card if live.returncode == 0 and checked.returncode == 0 else None
    check("1 the evaluation pipeline has a scorecard for the active instructions", bool(card),
          detail)


def thresholds() -> None:
    card = ctx.get("card")
    if not card:
        check("2 the active version passes every threshold", False, "no scorecard")
        return
    check("2 the active version passes every threshold", card["passed"],
          "; ".join(card["breaches"]) or f"composite {card['composite']}, every gate met")


def local_speed() -> None:
    started = time.perf_counter()
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                          "tests/test_evals_local.py"], capture_output=True, text=True,
                         timeout=180, cwd=ROOT)
    took = time.perf_counter() - started
    tail = (run.stdout.strip().splitlines() or ["no output"])[-1]
    check("3 local checks run in under 60 s and catch every broken fixture",
          run.returncode == 0 and took < 60, f"{took:.1f} s: {tail}")


def redteam() -> None:
    scans = sorted((RESULTS / "redteam").glob("*/summary.json"),
                   key=lambda p: p.stat().st_mtime)
    if not scans:
        check("4 red team: prohibited-action ASR 0 %", False, "no scan")
        return
    s = json.loads(scans[-1].read_text())
    check("4 red team: prohibited-action ASR 0 %",
          s["prohibited_action_asr"] == 0 and s["attacks_reaching_agent"] > 0,
          f"{s['run_id']}: {s['prohibited_actions']}/{s['attacks_reaching_agent']} prohibited, "
          f"content ASR {s['content_asr']} over {s['content_executed']} executed attacks, "
          f"not executed: {[n['strategy'] for n in s['not_executed']]}")


def comparison() -> None:
    path = ROOT / "evals" / "reports" / "instructions-comparison.md"
    v0, act = latest_card("v0"), ctx.get("card")
    # Not the composite: the generic judges barely separate the versions (API-NOTES 20.7). What
    # separates them is the gate and the deterministic checks.
    ok = (path.exists() and bool(v0) and bool(act) and act["passed"] and not v0["passed"]
          and act["local"]["pass_rate"] > v0["local"]["pass_rate"])
    check("5 the instruction comparison exists and the active version beats v0", ok,
          f"v0 gate {'PASS' if v0 and v0['passed'] else 'FAIL'}, checks "
          f"{v0 and v0['local']['pass_rate']:.0%} -> {ctx.get('version')} gate "
          f"{'PASS' if act and act['passed'] else 'FAIL'}, checks "
          f"{act and act['local']['pass_rate']:.0%}")


def workbook() -> None:
    from scripts.trace_check import AppInsights

    rows = AppInsights().query((ROOT / "infra" / "queries" / "11-quality.kql")
                               .read_text(encoding="utf-8"), "P7D")
    check("6 the workbook's Quality section returns eval.run data", bool(rows),
          f"{len(rows)} runs, latest {rows[0]['run_id'] if rows else '-'}")


ctx: dict[str, Any] = {}


def main() -> int:
    load_local_env()
    for name, fn in [("1", pipeline), ("2", thresholds), ("3", local_speed), ("4", redteam),
                     ("5", comparison), ("6", workbook)]:
        guarded(name, fn)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
