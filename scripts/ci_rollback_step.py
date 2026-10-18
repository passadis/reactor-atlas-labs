"""Rollback: pin the hosted agent to a version, run the smoke check, report the wall clock.

    python scripts/ci_rollback_step.py <version|good>

Fails if the pin or the smoke fails, or if the whole thing took 180 s or more - the budget for a
rollback that runs on stage (PHASE-13 section 2).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

BUDGET_S = 180


def main() -> int:
    started = time.perf_counter()
    version = sys.argv[1] if len(sys.argv) > 1 else "good"
    if version == "good":
        version = str(yaml.safe_load(Path("evals/agent_versions.yaml").read_text())["good"])
    pin = subprocess.run([sys.executable, "-m", "scripts.hosted_agent", "pin", version],
                         check=False).returncode
    smoke = subprocess.run([shutil.which("bash") or "bash", "scripts/verify_smoke.sh"],
                           check=False).returncode
    took = time.perf_counter() - started
    line = (f"### Rollback to hosted agent version {version}: {took:.0f} s (budget {BUDGET_S} s) - "
            f"pin {'ok' if pin == 0 else 'FAILED'}, smoke {'green' if smoke == 0 else 'RED'}\n")
    print(line)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as f:
            f.write(line)
    return 0 if pin == 0 and smoke == 0 and took < BUDGET_S else 1


if __name__ == "__main__":
    sys.exit(main())
