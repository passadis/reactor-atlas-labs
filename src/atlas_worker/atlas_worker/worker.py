"""The remediation worker: a session-aware Service Bus consumer.

    python -m atlas_worker

- **Sessions.** `remediation-commands` requires sessions (SessionId = target service), so commands
  against one service run strictly in order while different services run in parallel. A bounded
  number of session loops (`ATLAS_WORKER_SESSIONS`, default 4) each take the next free session.
- **Locks renewed** for long steps by an `AutoLockRenewer` on the session.
- **Settlement follows the executor's verdict**: complete, abandon (redelivered; the 5th failed
  delivery dead-letters, as configured on the queue), or dead-letter with a reason.
- **One trace.** `traceparent` from the message's application properties is the parent of the
  worker's span, so web -> orchestrator -> APIM -> Function -> Service Bus -> worker is one
  waterfall in Application Insights.
- **Identity only.** The worker's managed identity for Service Bus and Cosmos; no connection string.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from contextlib import suppress
from typing import Any

from atlas_common.auth import get_async_credential
from atlas_common.config import Settings, load_local_env
from atlas_common.remediation import (
    COMMANDS_QUEUE,
    EVENTS_TOPIC,
    RESULTS_QUEUE,
    ExecutionRecord,
    ProgressEvent,
    RemediationCommand,
    service_bus_namespace,
)
from atlas_common.repositories.base import CosmosStore
from atlas_common.telemetry import bound, configure_telemetry
from azure.servicebus import NEXT_AVAILABLE_SESSION, ServiceBusMessage
from azure.servicebus.aio import AutoLockRenewer, ServiceBusClient
from azure.servicebus.exceptions import OperationTimeoutError, SessionCannotBeLockedError
from opentelemetry import trace
from opentelemetry.propagate import extract

from .executor import CommandExecutor

log = logging.getLogger("atlas.worker")
tracer = trace.get_tracer("atlas.worker")

SESSION_IDLE_S = 20
BACKOFF_CAP_S = 8.0


class Worker:
    def __init__(self, settings: Settings) -> None:
        self._credential = get_async_credential()
        self._store = CosmosStore(settings, self._credential)
        self._client = ServiceBusClient(service_bus_namespace(), credential=self._credential)
        self._stopping = asyncio.Event()
        self._events: Any = None
        self._results: Any = None
        self._executor = CommandExecutor(self._store, publish=self._publish,
                                         send_result=self._send_result)

    async def _publish(self, event: ProgressEvent) -> None:
        await self._events.send_messages(ServiceBusMessage(
            event.model_dump_json(), correlation_id=event.case_id,
            content_type="application/json",
            application_properties={"operation_id": event.operation_id,
                                    "status": event.step.status}))

    async def _send_result(self, record: ExecutionRecord) -> None:
        await self._results.send_messages(ServiceBusMessage(
            record.model_dump_json(), correlation_id=record.case_id,
            message_id=f"{record.operation_id}:{record.status}:{record.delivery_count}",
            content_type="application/json"))

    async def run(self) -> None:
        sessions = max(1, min(int(os.environ.get("ATLAS_WORKER_SESSIONS", "4")), 16))
        async with self._client:
            self._events = self._client.get_topic_sender(EVENTS_TOPIC)
            self._results = self._client.get_queue_sender(RESULTS_QUEUE)
            async with self._events, self._results:
                log.info("worker listening on %s with %d session loop(s)", COMMANDS_QUEUE,
                         sessions)
                await asyncio.gather(*(self._session_loop(i) for i in range(sessions)))
        await self._store.close()
        await self._credential.close()

    def stop(self) -> None:
        self._stopping.set()

    async def _session_loop(self, index: int) -> None:
        backoff = 1.0
        while not self._stopping.is_set():
            try:
                receiver = self._client.get_queue_receiver(
                    COMMANDS_QUEUE, session_id=NEXT_AVAILABLE_SESSION,
                    max_wait_time=SESSION_IDLE_S)
                async with receiver, AutoLockRenewer(max_lock_renewal_duration=900) as renewer:
                    renewer.register(receiver, receiver.session, max_lock_renewal_duration=900)
                    log.info("loop %d took session %s", index, receiver.session.session_id)
                    async for message in receiver:
                        await self._handle(receiver, message)
                        if self._stopping.is_set():
                            break
                backoff = 1.0
            except (OperationTimeoutError, SessionCannotBeLockedError):
                continue  # no free session right now; ask again
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("session loop %d failed; retrying in %.0f s", index, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _handle(self, receiver: Any, message: Any) -> None:
        props = {k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes)
                 else v for k, v in (message.application_properties or {}).items()}
        parent = extract({"traceparent": str(props.get("traceparent", ""))})
        delivery = int(message.delivery_count or 0) + 1
        # CorrelationId is the case (atlas_common.remediation): bound before the span starts, so the
        # worker span and every Cosmos and Service Bus span under it carry atlas.case_id.
        case_id = str(message.correlation_id or "") or None
        with bound(case_id=case_id, node="worker"), tracer.start_as_current_span(
            "atlas.worker.remediation", context=parent,
            attributes={"messaging.system": "servicebus", "messaging.destination": COMMANDS_QUEUE,
                        "messaging.message.id": str(message.message_id),
                        "atlas.delivery": delivery,
                        "atlas.case_id": str(message.correlation_id or "")},
        ) as span:
            try:
                command = RemediationCommand.model_validate(json.loads(str(message)))
            except Exception as exc:
                span.set_attribute("atlas.outcome", "malformed")
                await receiver.dead_letter_message(
                    message, reason="malformed", error_description=str(exc)[:1000])
                return
            span.set_attribute("atlas.operation_id", command.operation_id)
            try:
                outcome = await self._executor.execute(
                    command, message_id=str(message.message_id), delivery_count=delivery)
            except Exception as exc:
                # Unexpected (Cosmos down, a bug): retry like a transient failure.
                log.exception("operation %s errored on delivery %d", command.operation_id,
                              delivery)
                span.record_exception(exc)
                await asyncio.sleep(min(2.0 ** delivery, BACKOFF_CAP_S))
                await receiver.abandon_message(message)
                return
            span.set_attribute("atlas.outcome", outcome.settle)
            log.info("operation %s delivery %d: %s (%s)", command.operation_id, delivery,
                     outcome.settle, outcome.reason)
            if outcome.settle == "complete":
                await receiver.complete_message(message)
            elif outcome.settle == "abandon":
                await asyncio.sleep(min(2.0 ** delivery, BACKOFF_CAP_S))
                await receiver.abandon_message(message)
            else:
                await receiver.dead_letter_message(message, reason=outcome.reason[:128],
                                                   error_description=outcome.reason)


def main() -> None:
    load_local_env()
    configure_telemetry("atlas-worker")
    worker = Worker(Settings.from_env())
    loop = asyncio.new_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):  # Windows
            loop.add_signal_handler(sig, worker.stop)
    try:
        loop.run_until_complete(worker.run())
    finally:
        loop.close()


__all__ = ["Worker", "main"]
