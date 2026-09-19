"""Tracing for every ATLAS process, and the W3C `traceparent` helpers every hop uses.

CLAUDE.md section 1 and 7.13: a correlation id rides every hop (web, orchestrator, APIM, Functions,
Service Bus, worker) from Phase 03 on. This module is the only place that configures OpenTelemetry,
so every process exports to the same Application Insights resource with the same resource shape,
and every hop injects and extracts context the same way.

`configure_telemetry()` is idempotent and never raises: a process with no Application Insights
connection string still runs, still propagates `traceparent`, and says once that it is not
exporting.

Prompt and completion capture is OFF unless `ATLAS_CAPTURE_CONTENT=true`. Traces then carry token
counts, tool names, durations and errors, but not what the user typed. Phase 12 trace evaluation
turns it on for the evaluation run only.

Ingestion auth: in Azure, the exporter authenticates with the process's managed identity (each
compute identity holds Monitoring Metrics Publisher on the component). Locally it uses the
connection string's instrumentation key, which is not a secret in the credential sense; it is the
documented exception in CLAUDE.md section 1.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.trace import Span, Status, StatusCode

from .auth import credential_kind, get_credential

_log = logging.getLogger("atlas.telemetry")

SERVICE_NAMESPACE = "atlas"
TRACEPARENT = "traceparent"
TRACESTATE = "tracestate"

# Chatty at INFO: every Cosmos request and every HTTP pipeline hop, which drowns the traces a
# presenter wants to show.
_QUIET_LOGGERS = (
    "azure.cosmos",
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.identity",
    "azure.monitor.opentelemetry.exporter",
)

_state: dict[str, Any] = {"configured": False, "exporting": False}


def capture_content() -> bool:
    return os.environ.get("ATLAS_CAPTURE_CONTENT", "").strip().lower() in ("1", "true", "yes")


def configure_telemetry(service_name: str, *, service_version: str | None = None) -> bool:
    """Configure tracing, metrics and logs for this process. Returns True if exporting.

    Call once, at process start, before creating clients. Safe to call again (no-op).
    """
    if _state["configured"]:
        return bool(_state["exporting"])
    _state["configured"] = True

    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    connection_string = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if not connection_string:
        _log.warning("APPLICATIONINSIGHTS_CONNECTION_STRING not set: traces are not exported")
    else:
        try:
            from agent_framework.observability import create_resource
            from azure.monitor.opentelemetry import configure_azure_monitor

            resource = create_resource(
                service_name=service_name,
                service_version=service_version or os.environ.get("ATLAS_VERSION", "dev"),
                attributes={
                    "service.namespace": SERVICE_NAMESPACE,
                    "deployment.environment": os.environ.get("AZURE_ENV_NAME", "local"),
                },
            )
            options: dict[str, Any] = {
                "connection_string": connection_string,
                "resource": resource,
                # Only ATLAS's own loggers become App Insights traces; library logs stay local.
                "logger_name": "atlas",
            }
            if credential_kind() == "managed":
                options["credential"] = get_credential()
            configure_azure_monitor(**options)
            _state["exporting"] = True
        except Exception as exc:  # telemetry must never take the process down
            _log.warning("Azure Monitor export disabled: %s", type(exc).__name__)

    try:
        from agent_framework.observability import enable_instrumentation

        enable_instrumentation(enable_sensitive_data=capture_content())
    except Exception as exc:
        _log.warning("Agent Framework instrumentation disabled: %s", type(exc).__name__)
    return bool(_state["exporting"])


def tracer(name: str = "atlas") -> trace.Tracer:
    return trace.get_tracer(name)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Span]:
    """A span with ATLAS attributes. Exceptions mark it failed and propagate."""
    with tracer().start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(f"atlas.{key}", value)
        try:
            yield current
        except Exception as exc:
            current.record_exception(exc)
            current.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise


# -------------------------------------------------------------------- W3C propagation --


def inject_context(carrier: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """Headers (or Service Bus application properties) carrying the current trace context."""
    out: dict[str, str] = dict(carrier or {})
    propagate.inject(out)
    return out


def current_traceparent() -> str | None:
    return inject_context().get(TRACEPARENT)


def trace_id() -> str | None:
    """The current trace id as 32 hex chars, for logs, UI and the approval record."""
    ctx = trace.get_current_span().get_span_context()
    return f"{ctx.trace_id:032x}" if ctx.is_valid else None


@contextmanager
def continued_from(carrier: Mapping[str, Any] | None) -> Iterator[None]:
    """Make an inbound `traceparent` the parent of everything inside the block.

    Use at every inbound hop that is not auto-instrumented: Service Bus receivers, queue triggers,
    Logic Apps callbacks. Inbound HTTP on FastAPI and Functions is instrumented already.
    """
    headers = {str(k).lower(): str(v) for k, v in (carrier or {}).items() if v is not None}
    token = otel_context.attach(propagate.extract(headers))
    try:
        yield
    finally:
        otel_context.detach(token)
