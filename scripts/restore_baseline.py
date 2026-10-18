"""Put ATLAS back to its known-good baseline, then prove it (CLAUDE.md section 6).

    uv run python -m scripts.restore_baseline            # revert every injection, pin good, smoke
    uv run python -m scripts.restore_baseline --check    # report only, change nothing

Reverts every regression-kit injection (scripts/regression_kit.py) whatever state it is in -
reverting an injection that is not applied is a no-op - re-pins the hosted agent to the good
version from evals/agent_versions.yaml, and then runs scripts/verify_smoke.sh. Exit 0 only when
the smoke check is green. The nightly workflow runs apply-then-restore to prove this never rots.

Data and pointer writes only; no build, no deploy, no restart.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml
from atlas_common.config import load_local_env

from scripts.regression_kit import KIT

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report only")
    args = parser.parse_args()
    load_local_env()
    started = time.perf_counter()
    for name, inj in KIT.items():
        state = inj.status()
        if args.check:
            print(f"  {name:<22} {state}")
        elif state != "baseline":
            print(f"  {name:<22} {inj.revert()}")
        else:
            print(f"  {name:<22} already baseline")
    if not args.check:
        good = str(yaml.safe_load((ROOT / "evals" / "agent_versions.yaml").read_text())["good"])
        subprocess.run([sys.executable, "-m", "scripts.hosted_agent", "pin", good], cwd=ROOT,
                       check=True, capture_output=True, text=True, timeout=180)
        print(f"  {'hosted agent':<22} pinned to version {good} (good)")
    print(f"restore took {time.perf_counter() - started:.1f} s; running the smoke check")
    smoke = subprocess.run([shutil.which("bash") or "bash", "scripts/verify_smoke.sh"],
                           cwd=ROOT, check=False)
    return smoke.returncode


if __name__ == "__main__":
    sys.exit(main())
