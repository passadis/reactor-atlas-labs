"""The agent quality gate, evaluate-only (CLAUDE.md 9.2).

    uv run python -m evals.gate --version bad        # the pre-deployed regression: must FAIL
    uv run python -m evals.gate --version good       # the pinned good build: must PASS
    uv run python -m evals.gate --version 7 --set-baseline

Evaluates an ALREADY-DEPLOYED hosted-agent version - by number, or `good` / `bad` from
evals/agent_versions.yaml - on the held-out dataset: each case is sent to that exact version
(`agent_reference.version`, the pin is never moved), the full turn is recorded, the deterministic
checks and the Foundry judges score it, and the result is compared with evals/thresholds.yaml and
the last green baseline. Nothing is
built or deployed: on stage this is safe to run, and on stage you show a run that already
happened anyway.

Writes evals/results/<run>/gate.md (the PASS/FAIL report with the per-category diff - what CI
posts to the job summary) and exits 0 on PASS, 1 on FAIL.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "evals" / "results"
REPORTS = ROOT / "evals" / "reports"
VERSIONS = ROOT / "evals" / "agent_versions.yaml"
HOLDOUT = ROOT / "evals" / "datasets" / "atlas-eval-holdout-v1.jsonl"


def resolve(version: str) -> str:
    known = yaml.safe_load(VERSIONS.read_text(encoding="utf-8"))
    return str(known.get(version, version))


def report(card: dict[str, Any], label: str, version: str) -> str:
    base = card.get("baseline")
    lines = [f"# Agent quality gate: **{'PASS' if card['passed'] else 'FAIL'}**", "",
             f"- Evaluated: hosted agent `atlas-incident-agent` version **{version}** ({label}), "
             f"evaluate-only - nothing was built or deployed",
             f"- Dataset: holdout ({card['local']['cases']} cases, never tuned against; tool-fault "
             f"cases are harness-only)",
             f"- Composite {card['composite']:.3f}"
             + (f" vs last green baseline {base:.3f}" if base is not None else ""),
             f"- Deterministic checks {card['local']['passed']}/{card['local']['cases']}",
             f"- Foundry report: {card.get('report_url')}", "",
             "| Category | Rate | Gate |", "|---|---|---|"]
    thresholds = yaml.safe_load((ROOT / "evals" / "thresholds.yaml").read_text())["categories"]
    for cat, v in card["categories"].items():
        need = thresholds.get(cat, {}).get("min_pass_rate")
        mark = "" if need is None else ("ok" if v["pass_rate"] >= need else f"FAIL (< {need:.0%})")
        lines.append(f"| {cat} | {v['passed']}/{v['total']} ({v['pass_rate']:.0%}) | {mark} |")
    lines += ["", "## Why", ""]
    lines += [f"- {b}" for b in card["breaches"]] or ["- every threshold met"]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True, help="hosted agent version, or good / bad")
    parser.add_argument("--evaluate-only", action="store_true", default=True,
                        help="always on: this gate never builds or deploys (CLAUDE.md 9.2)")
    parser.add_argument("--set-baseline", action="store_true")
    parser.add_argument("--rescore", metavar="RUN_ID",
                        help="re-score an existing gate recording (no agent calls, no new judges)")
    args = parser.parse_args()
    version = resolve(args.version)
    run_id = args.rescore or f"gate-{time.strftime('%Y%m%d-%H%M%S')}-v{version}"
    # 1. drive the deployed version itself and record every full turn (target-mode output items
    #    come back incomplete for long turns - API-NOTES 21.2 - and a 100 % gate needs every row);
    # 2. deterministic checks + Foundry judges on those recorded turns (dataset mode).
    harness = [sys.executable, "-m", "evals.harness", "--dataset", str(HOLDOUT),
               "--hosted-version", version, "--run-id", run_id]
    if not args.rescore:
        subprocess.run(harness, cwd=ROOT, check=False)  # noqa: S603 - our own module, fixed argv
    judge = [sys.executable, "-m", "evals.run_eval", "--responses",
             str(RESULTS / run_id / "responses.jsonl")]
    if args.rescore:
        judge.append("--reuse-cloud")
    if args.set_baseline:
        judge.append("--set-baseline")
    subprocess.run(judge, cwd=ROOT, check=False)  # noqa: S603 - our own module, fixed argv
    card_path = RESULTS / run_id / "scorecard.json"
    if not card_path.exists():
        print(f"gate: no scorecard was produced for version {version}", file=sys.stderr)
        return 2
    card = json.loads(card_path.read_text())
    text = report(card, args.version, version)
    (RESULTS / run_id / "gate.md").write_text(text, encoding="utf-8")
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"gate-{args.version}.md").write_text(text, encoding="utf-8")
    print(text)
    return 0 if card["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
