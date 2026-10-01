"""Replay a dead-lettered remediation command after the cause is fixed.

    uv run python -m scripts.dlq_replay --command-id OP-XXXX --fix
    uv run python -m scripts.dlq_replay --command-id OP-XXXX          # cause already fixed

What it does, in order:
1. `--fix` turns the RB-0099 fault switch off (a data write in the `control` container).
2. Finds the dead-lettered message for that operation.
3. Re-arms the operation: status back to `queued`, succeeded steps KEPT - the worker resumes after
   the last good step instead of re-running what already happened.
4. Sends a fresh copy to `remediation-commands` with a NEW MessageId (`<plan>:replay:<n>`) - the
   original MessageId is still inside the 10-minute duplicate-detection window and would be dropped
   silently, which is exactly the behaviour that makes a normal duplicate harmless.
5. Completes the dead-lettered original.

Authorisation is untouched: the replay carries the same operation id, and the worker still proves
it from the token that operation spent. A replay can never authorise anything new.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime

from atlas_common.approval_tokens import APPROVALS
from atlas_common.config import Settings, load_local_env
from atlas_common.remediation import COMMANDS_QUEUE, child_traceparent, service_bus_namespace
from atlas_common.repositories.base import CosmosStore
from azure.identity.aio import AzureCliCredential
from azure.servicebus import ServiceBusMessage, ServiceBusSubQueue
from azure.servicebus.aio import ServiceBusClient

CONTROL = "control"
FAULT_DOC = "fault-rb-0099"


async def replay(operation_id: str, fix: bool) -> int:
    settings = Settings.from_env()
    async with AzureCliCredential() as cred, CosmosStore(settings, cred) as store, \
            ServiceBusClient(service_bus_namespace(), credential=cred) as client:
        if fix:
            await store.upsert(CONTROL, {"id": FAULT_DOC, "enabled": False,
                                         "set_at": datetime.now(UTC).isoformat(),
                                         "set_by": "scripts/dlq_replay.py --fix"})
            print("fixed: RB-0099 fault switch OFF")

        dlq = client.get_queue_receiver(COMMANDS_QUEUE, sub_queue=ServiceBusSubQueue.DEAD_LETTER,
                                        max_wait_time=10)
        async with dlq:
            target = None
            for message in await dlq.receive_messages(max_message_count=100, max_wait_time=10):
                body = json.loads(str(message))
                if body.get("operation_id") == operation_id and target is None:
                    target = (message, body)
                else:
                    await dlq.abandon_message(message)  # leave everyone else where they were
            if target is None:
                print(f"no dead-lettered command for {operation_id}")
                return 1
            message, body = target
            case_id = str(body["case_id"])

            rows = await store.query(APPROVALS, "SELECT * FROM c WHERE c.id = @id",
                                     [{"name": "@id", "value": f"operation:{operation_id}"}],
                                     partition_key=case_id)
            if rows:
                op = dict(rows[0])
                replays = int(op.get("replays", 0)) + 1
                op.update({"status": "queued", "replays": replays,
                           "detail": f"replayed from the DLQ ({replays})",
                           "updated_at": datetime.now(UTC).isoformat()})
                await store.upsert(APPROVALS, op)
            else:
                replays = 1
            # Re-open the case's remediation so the replay's progress shows (it is closed to
            # progress while dead-lettered - API-NOTES 17.9).
            case_rows = await store.query("cases", "SELECT * FROM c WHERE c.id = @id",
                                          [{"name": "@id", "value": case_id}],
                                          partition_key=case_id)
            if case_rows:
                case_doc = dict(case_rows[0])
                remediation = dict(case_doc.get("remediation") or {})
                remediation.update({"status": "replaying", "replays": replays})
                await store.upsert("cases", {**case_doc, "remediation": remediation})

            props = {k.decode() if isinstance(k, bytes) else k: v.decode()
                     if isinstance(v, bytes) else v
                     for k, v in (message.application_properties or {}).items()}
            sender = client.get_queue_sender(COMMANDS_QUEUE)
            async with sender:
                await sender.send_messages(ServiceBusMessage(
                    json.dumps(body), message_id=f"{body['plan_id']}:replay:{replays}",
                    session_id=message.session_id, correlation_id=case_id,
                    content_type="application/json",
                    application_properties={"traceparent": child_traceparent(
                        str(props.get("traceparent", "")) or None)}))
            await dlq.complete_message(message)
            print(f"replayed {operation_id} (case {case_id}, session {message.session_id}) "
                  f"as {body['plan_id']}:replay:{replays}; the dead-lettered copy is removed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command-id", required=True, help="the operation id, OP-...")
    parser.add_argument("--fix", action="store_true", help="turn the RB-0099 fault off first")
    args = parser.parse_args(argv)
    load_local_env()
    return asyncio.run(replay(args.command_id, args.fix))


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
