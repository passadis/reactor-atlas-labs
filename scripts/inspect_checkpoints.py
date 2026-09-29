"""List the checkpoints for a case. This is what goes on the projector in Episode 2.

    make checkpoints CASE=CASE-A9EAA3B8
    uv run python -m scripts.inspect_checkpoints --case CASE-A9EAA3B8

Reads Cosmos directly rather than through the orchestrator, so it still answers while the
orchestrator is scaled to zero - which is exactly when you want to look.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from atlas_common.config import Settings, load_local_env
from atlas_orchestrator.checkpoints import build_storage
from atlas_orchestrator.node_ids import WORKFLOW_GRAPH, WORKFLOW_HANDOFF, WORKFLOW_SEQUENTIAL

WORKFLOWS = (WORKFLOW_GRAPH, WORKFLOW_SEQUENTIAL, WORKFLOW_HANDOFF)


async def collect(storage: Any, workflow_name: str,
                  case_id: str | None) -> list[dict[str, Any]]:
    if storage is None:
        return []
    checkpoints = await storage.list_checkpoints(workflow_name=workflow_name)
    rows: list[dict[str, Any]] = []
    for checkpoint in checkpoints:
        pending = list(checkpoint.pending_request_info_events)
        if case_id and pending and not any(case_id in p for p in pending):
            continue
        rows.append({
            "checkpoint_id": str(checkpoint.checkpoint_id),
            "iteration": checkpoint.iteration_count,
            "timestamp": checkpoint.timestamp,
            "pending": pending,
            "hash": checkpoint.graph_signature_hash[:12],
            "parent": str(checkpoint.previous_checkpoint_id or "")[:8],
        })
    return sorted(rows, key=lambda r: (r["iteration"], r["timestamp"]))


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", default=None, help="only checkpoints mentioning this case id")
    parser.add_argument("--workflow", default=WORKFLOW_GRAPH, choices=WORKFLOWS)
    parser.add_argument("--all-workflows", action="store_true")
    args = parser.parse_args(argv)

    load_local_env()
    storage = build_storage(Settings.from_env())
    names = WORKFLOWS if args.all_workflows else (args.workflow,)
    total = 0
    for name in names:
        rows = await collect(storage, name, args.case)
        total += len(rows)
        print(f"\n{name}: {len(rows)} checkpoint(s)"
              + (f" for {args.case}" if args.case else ""))
        if not rows:
            continue
        print(f"  {'iter':>4}  {'checkpoint':12}  {'parent':8}  {'graph':12}  "
              f"{'timestamp':26}  pending")
        for row in rows:
            marker = " <- PARKED ON A HUMAN" if row["pending"] else ""
            print(f"  {row['iteration']:>4}  {row['checkpoint_id'][:12]}  {row['parent']:8}  "
                  f"{row['hash']:12}  {row['timestamp'][:26]:26}  "
                  f"{','.join(row['pending']) or '-'}{marker}")
    closer = getattr(storage, "close", None)
    if closer is not None:
        await closer()
    if total == 0:
        print("\nNo checkpoints found. If a run just happened, checkpointing is not working - "
              "a failed checkpoint does NOT fail the run (docs/API-NOTES.md 14.1).")
        return 1
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(asyncio.run(main()))
