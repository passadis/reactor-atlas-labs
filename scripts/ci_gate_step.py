"""CI step for the quality gate: run it, publish the report, set step outputs.

    python scripts/ci_gate_step.py <version|good|bad>

Runs evals.gate, appends gate.md to the job summary, writes `passed` and `version` to
$GITHUB_OUTPUT, and exits with the gate's own code - so a FAIL turns the job red.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    version = sys.argv[1] if len(sys.argv) > 1 else "good"
    code = subprocess.run([sys.executable, "-m", "evals.gate", "--version", version],
                          check=False).returncode
    runs = sorted(Path("evals/results").glob("gate-*"), key=lambda p: p.stat().st_mtime)
    if not runs:
        print("no gate run folder was produced", file=sys.stderr)
        return 2
    run = runs[-1]
    report = (run / "gate.md").read_text(encoding="utf-8") if (run / "gate.md").exists() else ""
    evaluated = json.loads((run / "scorecard.json").read_text())["target"].split(":")[-1] \
        if (run / "scorecard.json").exists() else version
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report)
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"passed={'true' if code == 0 else 'false'}\nversion={evaluated}\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
