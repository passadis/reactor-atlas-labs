"""Episode 1, Beat 1: the same question to the bare model - no tools, no data, no contract.

    uv run python -m scripts.demo_raw_model

Same deployment (`gpt-main`), same incident report as `demo_agent_local`. The model has never seen
Contoso's services, changes or runbooks, so everything specific it says is either a hedge or an
invention. Put it next to the agent's answer: the difference is the tools and the contract, not a
better model.

`--json` prints the answer plus a crude check of which ATLAS-only facts it happened to name
(CHG-2026-0873, RB-0042, svc-payments, 865 ms). The expected count is zero.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from atlas_common.auth import SCOPE_FOUNDRY, get_credential
from atlas_common.config import Settings
from azure.identity import get_bearer_token_provider
from openai import OpenAI

from scripts.demo_agent_local import CANONICAL_REPORT, _console_utf8

GROUNDED_FACTS = ("CHG-2026-0873", "RB-0042", "svc-payments", "865")
PROMPT = (
    "You are the on-call assistant for Contoso Cloud Operations. Triage this incident: severity, "
    "likely cause, the change responsible, the runbook to follow, and which services are affected."
    f"\n\n{CANONICAL_REPORT}"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    client = OpenAI(
        base_url=settings.foundry_openai_base_url,
        api_key=get_bearer_token_provider(get_credential(), SCOPE_FOUNDRY),
        timeout=60,
    )
    started = time.perf_counter()
    response = client.responses.create(model=settings.model_main, input=PROMPT, store=False)
    elapsed = time.perf_counter() - started
    text = response.output_text
    named = [fact for fact in GROUNDED_FACTS if fact in text]

    if args.json:
        print(json.dumps({"answer": text, "grounded_facts_named": named,
                          "seconds": round(elapsed, 1)}, indent=2))
        return 0
    print(f"RAW MODEL ({settings.model_main}, no tools, no data)\n")
    print(f"PROMPT  {CANONICAL_REPORT}\n")
    print(text)
    print(f"\n  {elapsed:.1f}s. ATLAS facts it could know: {len(named)} of {len(GROUNDED_FACTS)}"
          f" ({', '.join(named) or 'none'})")
    return 0


if __name__ == "__main__":
    _console_utf8()
    sys.exit(main())
