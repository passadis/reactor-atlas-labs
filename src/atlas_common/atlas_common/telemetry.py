"""Tracing for every ATLAS process, and the W3C `traceparent` helpers every hop uses.

CLAUDE.md section 1 and 7.13: a correlation id rides every hop (web, orchestrator, APIM, Functions,
Service Bus, worker) from Phase 03 on. This module is the only place that configures OpenTelemetry,
so every process exports to the same Application Insights resource with the same resource shape,
and every hop injects and extracts context the same way.

`configure_telemetry()` is idempotent and never raises: a process with no Application Insights
connection string still runs, still propagates `traceparent`, and says once that it is not
exporting.

Prompt and completion capture is OFF unless `ATLAS_CAPTURE_CONTENT=true`. Traces then carry token
counts, tool names, durations and errors, but not what the user typed. The DEMO environment turns
it on (Phase 11) so a trace can answer "why did the agent do that?" - and every process says so,
loudly, at startup. Synthetic data only; never in production.

Span taxonomy (Phase 11): business code binds `atlas.*` attributes once with `bound(case_id=...)`;
`SpanEnricher` stamps them onto every span started inside, including the framework's own `gen_ai`
spans. Nobody hand-sets `atlas.case_id` on individual spans.

Ingestion auth: in Azure, the exporter authenticates with the process's managed identity (each
compute identity holds Monitoring Metrics Publisher on the component). Locally it uses the
connection string's instrumentation key, which is not a secret in the credential sense; it is the
documented exception in CLAUDE.md section 1.
"""

from __future__ import annotations

import importlib
import logging
import os
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar
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
    # Logs every AMQP link state change at INFO - hundreds of lines a minute per receiver.
    "azure.servicebus",
)

_state: dict[str, Any] = {"configured": False, "exporting": False}

# The ATLAS attributes in force for the current task, copied onto every span started inside it.
# Immutable default (None), so no task can mutate a shared dict by accident.
_bound: ContextVar[dict[str, str] | None] = ContextVar("atlas_span_attributes", default=None)


@contextmanager
def bound(**attributes: Any) -> Iterator[None]:
    """Bind `atlas.<name>` attributes to every span started inside this block (and its tasks).

    Values are strings; None is ignored. Nested blocks add to, and may override, the outer ones.
    """
    merged = {**(_bound.get() or {}),
              **{k: str(v) for k, v in attributes.items() if v is not None}}
    token = _bound.set(merged)
    try:
        yield
    finally:
        _bound.reset(token)


def bound_attributes() -> dict[str, str]:
    return dict(_bound.get() or {})


def _span_enricher() -> Any:
    from opentelemetry.sdk.trace import SpanProcessor

    class SpanEnricher(SpanProcessor):
        """Stamp the bound `atlas.*` attributes - and the version - onto every span at start."""

        def __init__(self) -> None:
            self._version = build_version()

        def on_start(self, span: Any, parent_context: Any = None) -> None:
            span.set_attribute("atlas.agent_version", self._version)
            for key, value in (_bound.get() or {}).items():
                span.set_attribute(f"atlas.{key}", value)

        def on_end(self, span: Any) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    return SpanEnricher()


def capture_content() -> bool:
    return os.environ.get("ATLAS_CAPTURE_CONTENT", "").strip().lower() in ("1", "true", "yes")


def build_version() -> str:
    """The git commit this build came from: ATLAS_VERSION, else the stamped file, else "dev"."""
    explicit = os.environ.get("ATLAS_VERSION", "").strip()
    if explicit:
        return explicit
    # Imported by name: the file exists only after packaging (it is gitignored), and a static
    # import would fail type-checking on a fresh clone.
    try:
        stamped = importlib.import_module("atlas_common._version")
    except ImportError:
        return "dev"
    return str(getattr(stamped, "VERSION", "dev"))


def _resource(service_name: str, service_version: str | None) -> Any:
    """The OpenTelemetry resource for this process.

    Agent Framework's helper where it is installed (web, orchestrator, hosted agent); plain
    OpenTelemetry where it is not (the worker, the Functions). Importing the helper
    unconditionally made the worker's whole exporter setup fail on ModuleNotFoundError - its spans
    silently never reached App Insights and the trace stopped at the Service Bus hop.
    """
    version = service_version or build_version()
    attributes = {"service.namespace": SERVICE_NAMESPACE,
                  "deployment.environment": os.environ.get("AZURE_ENV_NAME", "local")}
    try:
        from agent_framework.observability import create_resource
    except ImportError:
        from opentelemetry.sdk.resources import Resource

        return Resource.create({"service.name": service_name, "service.version": version,
                                **attributes})
    return create_resource(service_name=service_name, service_version=version,
                           attributes=attributes)


def configure_telemetry(service_name: str, *, service_version: str | None = None) -> bool:
    """Configure tracing, metrics and logs for this process. Returns True if exporting.

    Call once, at process start, before creating clients. Safe to call again (no-op).
    """
    if _state["configured"]:
        return bool(_state["exporting"])
    _state["configured"] = True

    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    # Console logs too: Container Apps log streaming and local runs read stdout/stderr, and the
    # Azure Monitor handler on the `atlas` logger would otherwise swallow them.
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
        )

    connection_string = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if not connection_string:
        _log.warning("APPLICATIONINSIGHTS_CONNECTION_STRING not set: traces are not exported")
        # Still install a real provider: spans must be valid for `traceparent` to propagate,
        # exported or not. The correlation id is an invariant, the exporter is not.
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        trace.set_tracer_provider(
            TracerProvider(resource=Resource.create({"service.name": service_name}))
        )
    else:
        try:
            from azure.monitor.opentelemetry import configure_azure_monitor

            resource = _resource(service_name, service_version)
            options: dict[str, Any] = {
                "connection_string": connection_string,
                "resource": resource,
                # Only ATLAS's own loggers become App Insights traces; library logs stay local.
                "logger_name": "atlas",
                "span_processors": [_span_enricher()],
            }
            if credential_kind() == "managed":
                options["credential"] = get_credential()
            # The distro (1.8.x) defaults to a rate-limited sampler at 5 traces/s. ATLAS's volume is
            # a demo's, and the whole point of Episode 4 is a complete waterfall, so keep every
            # trace unless the environment says otherwise (API-NOTES 18).
            os.environ.setdefault("OTEL_TRACES_SAMPLER", "always_on")
            configure_azure_monitor(**options)
            _state["exporting"] = True
        except Exception as exc:  # telemetry must never take the process down
            _log.warning("Azure Monitor export disabled: %s", type(exc).__name__)

    if capture_content():
        # Said out loud at every start, on purpose: an audience should hear this is a choice.
        _log.warning("CONTENT CAPTURE IS ON (ATLAS_CAPTURE_CONTENT=true): prompts, completions, "
                     "tool arguments and results are recorded in traces for %s. Demo environment, "
                     "synthetic data only - never enable this in production.", service_name)
        # The same switch for the instrumentations that read their own variable: Foundry's
        # hosting libraries and the GenAI semantic-convention instrumentations.
        os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
        os.environ.setdefault("ENABLE_SENSITIVE_DATA", "true")
    try:
        from agent_framework.observability import enable_instrumentation
    except ImportError:
        return bool(_state["exporting"])  # no agents in this process; nothing to instrument
    try:
        enable_instrumentation(enable_sensitive_data=capture_content())
    except Exception as exc:
        _log.warning("Agent Framework instrumentation disabled: %s", type(exc).__name__)
    return bool(_state["exporting"])


_events = logging.getLogger("atlas.events")


def event(name: str, **attributes: Any) -> None:
    """One structured business event: an App Insights `traces` row the workbook can query.

    `atlas.event` names it; every other field becomes `atlas.<name>` in customDimensions, along with
    whatever `bound()` attributes are in force (the case id, mostly). It carries the current trace
    id, so the event sits inside the case's waterfall.
    """
    extra: dict[str, Any] = {"atlas.event": name,
                             **{f"atlas.{k}": v for k, v in bound_attributes().items()}}
    extra.update({f"atlas.{k}": (v if isinstance(v, (int, float, bool)) else str(v))
                  for k, v in attributes.items() if v is not None})
    _events.info(name, extra=extra)


def flush_telemetry(timeout_ms: int = 15000) -> None:
    """Push buffered spans and log records out now. Short-lived scripts (the evaluation runner)
    must call it before exiting, or their last events die in the batch processor."""
    from opentelemetry import _logs

    for provider in (trace.get_tracer_provider(), _logs.get_logger_provider()):
        flush = getattr(provider, "force_flush", None)
        if flush is not None:
            with suppress(Exception):
                flush(timeout_ms)


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
