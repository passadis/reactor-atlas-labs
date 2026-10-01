"""List dead-lettered remediation commands: why each one died, and which case it belongs to.

    uv run python -m scripts.dlq_inspect            # remediation-commands DLQ
    uv run python -m scripts.dlq_inspect --json

Peeks only - nothing is received, completed or moved. The DLQ of a session queue is not itself
session-enabled, so it can be browsed without knowing the sessions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from atlas_common.config import load_local_env
from atlas_common.remediation import COMMANDS_QUEUE, service_bus_namespace
from azure.identity.aio import AzureCliCredential
from azure.servicebus import ServiceBusSubQueue
from azure.servicebus.aio import ServiceBusClient


def describe(message: Any) -> dict[str, Any]:
    try:
        body = json.loads(str(message))
    except ValueError:
        body = {}
    return {
        "operation_id": body.get("operation_id"), "case_id": message.correlation_id,
        "plan_id": body.get("plan_id"), "message_id": message.message_id,
        "session_id": message.session_id, "delivery_count": message.delivery_count,
        "reason": message.dead_letter_reason, "description": message.dead_letter_error_description,
        "enqueued": message.enqueued_time_utc.isoformat() if message.enqueued_time_utc else None,
        "sequence": message.sequence_number,
    }


async def peek(limit: int = 100) -> list[dict[str, Any]]:
    async with AzureCliCredential() as cred, ServiceBusClient(
            service_bus_namespace(), credential=cred) as client:
        receiver = client.get_queue_receiver(COMMANDS_QUEUE,
                                             sub_queue=ServiceBusSubQueue.DEAD_LETTER)
        async with receiver:
            return [describe(m) for m in await receiver.peek_messages(max_message_count=limit)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    load_local_env()
    rows = asyncio.run(peek())
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"{COMMANDS_QUEUE} dead-letter queue: {len(rows)} message(s)")
    for r in rows:
        print(f"  {r['operation_id'] or '?':18s} case {r['case_id'] or '?':16s} "
              f"deliveries {r['delivery_count']}  {r['reason']}")
        if r["description"] and r["description"] != r["reason"]:
            print(f"      {str(r['description'])[:110]}")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
