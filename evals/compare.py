"""Scorecards side by side: the instruction-version table for Episode 4.

    uv run python -m evals.compare evals/results/<v0-run>/scorecard.json \
        evals/results/<v1-run>/scorecard.json evals/results/<v2-run>/scorecard.json \
        --out evals/results/instructions-comparison.md

Same model, same tools, same data, same 72 cases - only the instructions differ. Every number is
read from the scorecards named in the header, so the table is reproducible from those runs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evals.run_eval import QUALITY, SAFETY


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v:.0%}"


def _pts(a: float | None, b: float | None) -> str:
    if a is None or b is None:
        return ""
    d = round((b - a) * 100, 1)
    return f"{'+' if d >= 0 else ''}{d}"


def compare(cards: list[dict[str, Any]]) -> str:
    names = [c["instructions_version"] for c in cards]
    first, last = cards[0], cards[-1]
    head = "| | " + " | ".join(names) + f" | {names[0]} to {names[-1]} (pts) |"
    sep = "|---" * (len(cards) + 2) + "|"
    lines = [f"# Instructions {' vs '.join(names)}", "",
             f"Same model, same six tools, same data, same {first['local']['cases']} cases; only "
             "the instructions differ. Runs: " + ", ".join(f"`{c['run_id']}`" for c in cards) + ".",
             "", head, sep,
             "| **Composite** | " + " | ".join(f"{c['composite']:.3f}" for c in cards)
             + f" | {_pts(first['composite'], last['composite'])} |",
             "| Local checks | " + " | ".join(_pct(c["local"]["pass_rate"]) for c in cards)
             + f" | {_pts(first['local']['pass_rate'], last['local']['pass_rate'])} |",
             "| Gate | " + " | ".join("PASS" if c["passed"] else "FAIL" for c in cards) + " | |",
             "", "## By category", "", head.replace("| |", "| Category |"), sep]
    for cat in sorted({k for c in cards for k in c["categories"]}):
        vals = [c["categories"].get(cat, {}).get("pass_rate") for c in cards]
        lines.append(f"| {cat} | " + " | ".join(_pct(v) for v in vals)
                     + f" | {_pts(vals[0], vals[-1])} |")
    lines += ["", "## By evaluator", "", head.replace("| |", "| Evaluator |"), sep]
    for name in QUALITY + SAFETY:
        vals = [c["evaluators"].get(name, {}).get("pass_rate") for c in cards]
        if all(v is None for v in vals):
            continue
        lines.append(f"| {name} | " + " | ".join(_pct(v) for v in vals)
                     + f" | {_pts(vals[0], vals[-1])} |")
    lines += ["", "## Gate breaches", ""]
    for c in cards:
        lines.append(f"- {c['instructions_version']}: "
                     + ("; ".join(c["breaches"]) if c["breaches"] else "none"))
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scorecards", type=Path, nargs="+")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    text = compare([json.loads(p.read_text()) for p in args.scorecards])
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
