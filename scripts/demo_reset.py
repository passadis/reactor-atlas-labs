"""Return runtime state to the known-good baseline. Target: under 60 seconds.

    uv run python -m scripts.demo_reset          # or: make demo-reset
    uv run python -m scripts.demo_reset --dry-run

A DATA reset, never a control-plane one (CLAUDE.md section 9): it belongs in the T-10 routine, not
mid-session. It clears what a demo run creates and leaves what the demo depends on:

  cleared     cases, approvals (tokens, operations, refusals, callbacks), idempotency, tickets,
              sessions, workflow checkpoints; every remediation queue, subscription and DLQ;
              approval workflow runs still waiting
  re-armed    the RB-0099 fault switch ON (so the DLQ beat works), the ops API chaos flag OFF
  untouched   services, changes, telemetry, people, entitlements, incidents, agent instructions,
              eval runs, the search index, every deployed resource
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from typing import Any

from atlas_common.config import Settings, load_local_env
from atlas_common.remediation import (
    COMMANDS_QUEUE,
    EVENTS_TOPIC,
    ORCHESTRATOR_SUBSCRIPTION,
    RESULTS_QUEUE,
    service_bus_namespace,
)
from atlas_common.repositories.base import CosmosStore
from azure.identity.aio import AzureCliCredential
from azure.servicebus import NEXT_AVAILABLE_SESSION, ServiceBusReceiveMode, ServiceBusSubQueue
from azure.servicebus.aio import ServiceBusClient
from azure.servicebus.exceptions import OperationTimeoutError

# container -> partition key field
RUNTIME_CONTAINERS = {
    "cases": "case_id", "approvals": "case_id", "idempotency": "key", "tickets": "ticket_id",
    "sessions": "session_id", "workflow-checkpoints": "workflow_name", "control": "id",
}
SUBSCRIPTIONS = (ORCHESTRATOR_SUBSCRIPTION, "web-progress", "audit")
BASELINE_CONTROL = [
    {"id": "fault-rb-0099", "enabled": True},
    {"id": "chaos-ops-api", "enabled": False},
]


async def clear_container(store: CosmosStore, name: str, pk: str, dry: bool) -> int:
    container = store.container(name)
    # `pk` comes from RUNTIME_CONTAINERS above, never from input.
    fields = "c.id" if pk == "id" else f"c.id, c.{pk}"  # `control` is partitioned by id itself
    query = f"SELECT {fields} FROM c"  # noqa: S608
    rows = [r async for r in container.query_items(query)]
    if dry:
        return len(rows)
    gate = asyncio.Semaphore(32)

    async def delete(row: dict[str, Any]) -> None:
        # Already gone, or a partition key the document does not carry: nothing to reset.
        async with gate:
            with contextlib.suppress(Exception):
                await container.delete_item(item=row["id"], partition_key=row.get(pk))

    await asyncio.gather(*(delete(r) for r in rows))
    return len(rows)


async def drain(receiver: Any, dry: bool) -> int:
    total = 0
    async with receiver:
        if dry:
            return len(await receiver.peek_messages(max_message_count=250))
        # The FIRST receive on a fresh link can come back empty if it waits only briefly - the
        # DLQ "drained" 0 of 7 that way. Wait properly once, then briefly while it keeps giving.
        wait = 8.0
        while True:
            batch = await receiver.receive_messages(max_message_count=100, max_wait_time=wait)
            if not batch:
                return total
            total += len(batch)
            wait = 2.0


async def message_counts(cred: Any) -> dict[str, tuple[int, int]]:
    """(active, dead-lettered) per entity, from the management API - one call each, no receive.

    Draining an EMPTY entity is what cost the time: a receiver waits its full max_wait_time to learn
    there is nothing, and the session queue has to be probed session by session. Ask first.
    """
    from azure.servicebus.aio.management import ServiceBusAdministrationClient

    counts: dict[str, tuple[int, int]] = {}
    async with ServiceBusAdministrationClient(service_bus_namespace(), credential=cred) as admin:
        for queue in (COMMANDS_QUEUE, RESULTS_QUEUE):
            props = await admin.get_queue_runtime_properties(queue)
            counts[queue] = (props.active_message_count, props.dead_letter_message_count)
        for sub in SUBSCRIPTIONS:
            props = await admin.get_subscription_runtime_properties(EVENTS_TOPIC, sub)
            counts[f"{EVENTS_TOPIC}/{sub}"] = (props.active_message_count,
                                               props.dead_letter_message_count)
    return counts


async def purge_service_bus(cred: Any, dry: bool) -> dict[str, int]:
    counts: dict[str, int] = {}
    mode = ServiceBusReceiveMode.RECEIVE_AND_DELETE
    present = await message_counts(cred)
    if dry:
        for name, (active, dead) in present.items():
            counts[name], counts[f"{name}/DLQ"] = active, dead
        return counts
    async with ServiceBusClient(service_bus_namespace(), credential=cred) as client:
        # Session queue: every session has to be accepted to be drained.
        sessions, total = 0, 0
        while present[COMMANDS_QUEUE][0] and sessions < 200:
            try:
                receiver = client.get_queue_receiver(
                    COMMANDS_QUEUE, session_id=NEXT_AVAILABLE_SESSION, receive_mode=mode,
                    max_wait_time=3)
                total += await drain(receiver, dry)
                sessions += 1
            except OperationTimeoutError:
                break
        counts[COMMANDS_QUEUE] = total
        jobs = {
            f"{COMMANDS_QUEUE}/DLQ": client.get_queue_receiver(
                COMMANDS_QUEUE, sub_queue=ServiceBusSubQueue.DEAD_LETTER, receive_mode=mode),
            RESULTS_QUEUE: client.get_queue_receiver(RESULTS_QUEUE, receive_mode=mode),
            f"{RESULTS_QUEUE}/DLQ": client.get_queue_receiver(
                RESULTS_QUEUE, sub_queue=ServiceBusSubQueue.DEAD_LETTER, receive_mode=mode),
        }
        for sub in SUBSCRIPTIONS:
            jobs[f"{EVENTS_TOPIC}/{sub}"] = client.get_subscription_receiver(
                EVENTS_TOPIC, sub, receive_mode=mode)
            jobs[f"{EVENTS_TOPIC}/{sub}/DLQ"] = client.get_subscription_receiver(
                EVENTS_TOPIC, sub, sub_queue=ServiceBusSubQueue.DEAD_LETTER, receive_mode=mode)
        def pending(job: str) -> int:
            base = job.removesuffix("/DLQ")
            active, dead = present.get(base, (0, 0))
            return dead if job.endswith("/DLQ") else active

        busy = {name: r for name, r in jobs.items() if pending(name)}
        results = await asyncio.gather(*(drain(r, dry) for r in busy.values()))
        counts.update({name: 0 for name in jobs})
        counts.update(dict(zip(busy, results, strict=True)))
    return counts


def cancel_waiting_approval_runs(dry: bool) -> int:
    """Approval runs still waiting on a webhook would call back about cases that no longer exist."""
    az = shutil.which("az") or "az"
    rg = os.environ.get("AZURE_RESOURCE_GROUP", "")
    try:
        site = subprocess.run([az, "logicapp", "list", "-g", rg, "--query", "[0].id", "-o",
                               "tsv"], capture_output=True, text=True, timeout=60,
                              check=True).stdout.strip()
        base = (f"https://management.azure.com{site}/hostruntime/runtime/webhooks/workflow/api/"
                "management/workflows/approve-remediation/runs")
        listed = subprocess.run(
            [az, "rest", "--method", "get", "--url", f"{base}?api-version=2023-12-01",
             "-o", "json"], capture_output=True, text=True, timeout=60, check=True).stdout
    except (subprocess.SubprocessError, OSError):
        return 0
    import json
    running = [r["name"] for r in json.loads(listed or "{}").get("value", [])
               if (r.get("properties") or {}).get("status") in ("Running", "Waiting")]
    if not dry:
        for run in running:
            subprocess.run([az, "rest", "--method", "post", "--url",
                            f"{base}/{run}/cancel?api-version=2023-12-01"],
                           capture_output=True, text=True, timeout=60, check=False)
    return len(running)


async def reset(dry: bool) -> dict[str, Any]:
    settings = Settings.from_env()
    report: dict[str, Any] = {}
    async with AzureCliCredential() as cred, CosmosStore(settings, cred) as store:
        # All three at once: the Logic App cleanup is az CLI calls, slow and independent.
        cosmos, bus, runs = await asyncio.gather(
            asyncio.gather(*(clear_container(store, name, pk, dry)
                             for name, pk in RUNTIME_CONTAINERS.items())),
            purge_service_bus(cred, dry),
            asyncio.to_thread(cancel_waiting_approval_runs, dry))
        report["cosmos"] = dict(zip(RUNTIME_CONTAINERS, cosmos, strict=True))
        report["service_bus"] = bus
        if not dry:
            now = datetime.now(UTC).isoformat()
            for doc in BASELINE_CONTROL:
                await store.upsert("control", {**doc, "set_at": now,
                                               "set_by": "scripts/demo_reset.py"})
    report["approval_runs_cancelled"] = runs
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    load_local_env()
    started = time.perf_counter()
    report = asyncio.run(reset(args.dry_run))
    seconds = time.perf_counter() - started
    verb = "would clear" if args.dry_run else "cleared"
    print(f"demo reset {'(dry run) ' if args.dry_run else ''}in {seconds:.1f} s")
    for name, n in report["cosmos"].items():
        print(f"  cosmos   {name:22s} {verb} {n}")
    for name, n in report["service_bus"].items():
        print(f"  bus      {name:40s} {verb} {n}")
    print(f"  logic    approval runs waiting: {report['approval_runs_cancelled']} "
          f"{'would be ' if args.dry_run else ''}cancelled")
    if not args.dry_run:
        print("  control  RB-0099 fault ON, ops API chaos OFF")
    ok = seconds < 60
    print("within the 60 s budget" if ok else "OVER the 60 s budget")
    return 0 if ok else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
