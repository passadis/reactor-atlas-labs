"""Rehydrate a parked case from its checkpoint and finish it.

    uv run python -m scripts.resume_case CASE-A9EAA3B8
    uv run python -m scripts.resume_case CASE-A9EAA3B8 --approve --role director
    uv run python -m scripts.resume_case CASE-A9EAA3B8 --checkpoint 11a3a624-...

Runs the workflow in THIS process, rebuilt from `node_ids`, so it works whether or not the
orchestrator is up. That is the point: the case lives in the checkpoint, not in a replica.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from atlas_agent.factory import IncidentAgentRuntime
from atlas_common.config import Settings, load_local_env
from atlas_orchestrator.approval import ApprovalResponse
from atlas_orchestrator.runner import CaseRunner


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id")
    parser.add_argument("--checkpoint", default=None, help="a specific checkpoint id")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--approve", action="store_true", help="answer the pending approval: yes")
    group.add_argument("--reject", action="store_true", help="answer the pending approval: no")
    parser.add_argument("--by", default="dana@contoso.com")
    parser.add_argument("--role", default="director")
    parser.add_argument("--comment", default="Resumed from the checkpoint by an operator.")
    args = parser.parse_args(argv)

    load_local_env()
    response: ApprovalResponse | None = None
    if args.approve or args.reject:
        response = ApprovalResponse(approved=args.approve, by=args.by, role=args.role,
                                    comment=args.comment)

    started = time.perf_counter()
    async with IncidentAgentRuntime(Settings.from_env()) as runtime:
        runner = CaseRunner(runtime)
        print(await runner.load_reference_data())
        try:
            outcome = await runner.resume_from_checkpoint(
                args.case_id, response=response, checkpoint_id=args.checkpoint)
        except KeyError:
            print(f"no case {args.case_id}", file=sys.stderr)
            return 1
        except LookupError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        finally:
            await runner.close()

    outcome["wall_clock_s"] = round(time.perf_counter() - started, 2)
    print(json.dumps(outcome, indent=2))
    return 0 if not outcome.get("still_suspended") else 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(asyncio.run(main()))
