"""CI rule: a change to what the agent is told or what its tools promise needs a new evaluation.

    python scripts/ci_eval_required.py <base-ref>        # e.g. origin/main

Fails when the diff against <base-ref> touches agent instructions, tool descriptions or the tool
API contract, but no evaluation report under evals/reports/ changed with it. Instructions and tool
descriptions are versioned artefacts (PHASE-13 section 3): changing one without measuring it is
the regression Episode 4 is about. CODEOWNERS adds the human half - a named reviewer.

No Azure, no ATLAS imports: runs on a bare CI checkout.
"""

from __future__ import annotations

import fnmatch
import subprocess
import sys

GUARDED = ["src/*/*/instructions/*", "src/atlas_orchestrator/atlas_orchestrator/instructions/*",
           "apim/mcp/*", "apim/openapi/*", "src/atlas_agent/atlas_agent/tools.py"]
EVIDENCE = ["evals/reports/*"]


def changed(base: str) -> list[str]:
    out = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], capture_output=True,
                         text=True, check=True).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    files = changed(sys.argv[1])
    guarded = [f for f in files if matches(f, GUARDED)]
    evidence = [f for f in files if matches(f, EVIDENCE)]
    if guarded and not evidence:
        print("FAIL: agent instructions or tool contract changed without a new evaluation:")
        for f in guarded:
            print(f"  {f}")
        print("Run `make eval` (and commit evals/reports/) for this change.")
        return 1
    print(f"ok: {len(guarded)} guarded file(s) changed, {len(evidence)} evaluation report(s) "
          "changed with them")
    return 0


if __name__ == "__main__":
    sys.exit(main())
