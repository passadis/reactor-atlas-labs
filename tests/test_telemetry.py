"""W3C trace context propagation: what every hop in ATLAS relies on (CLAUDE.md section 1)."""

from __future__ import annotations

import re

from atlas_common.telemetry import (
    TRACEPARENT,
    continued_from,
    current_traceparent,
    inject_context,
    span,
    trace_id,
)
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

# The SDK provider is what configure_telemetry installs via Azure Monitor. Without one, the API
# no-op provider produces invalid spans and nothing propagates, so install a plain one for tests.
if not isinstance(trace.get_tracer_provider(), TracerProvider):
    trace.set_tracer_provider(TracerProvider())

# Flags may be 01 or 03: the SDK sets the W3C Level 2 "random trace id" bit alongside "sampled".
TRACEPARENT_SHAPE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


def test_no_active_span_means_no_traceparent() -> None:
    assert current_traceparent() is None
    assert trace_id() is None


def test_inject_produces_a_w3c_traceparent_for_the_current_span() -> None:
    with span("unit", case="inject"):
        header = current_traceparent()
        assert header is not None
        assert TRACEPARENT_SHAPE.match(header)
        assert header.split("-")[1] == trace_id()


def test_inject_preserves_existing_carrier_entries() -> None:
    with span("unit"):
        carrier = inject_context({"x-atlas-case": "INC-1"})
    assert carrier["x-atlas-case"] == "INC-1"
    assert TRACEPARENT in carrier


def test_an_inbound_traceparent_parents_the_next_hop() -> None:
    with span("producer"):
        outbound = inject_context()
        producer_trace = trace_id()

    # A different process, a different hop: Service Bus application properties arrive as-is,
    # sometimes with a capitalised key.
    inbound = {"Traceparent": outbound[TRACEPARENT], "unrelated": 3}
    with continued_from(inbound), span("consumer"):
        assert trace_id() == producer_trace
    assert trace_id() is None


def test_bound_attributes_are_stamped_on_every_span_started_inside() -> None:
    """Phase 11 span taxonomy: bind once, every span (the framework's too) carries it."""
    from atlas_common.telemetry import _span_enricher, bound
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(_span_enricher())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    with bound(case_id="CASE-T1"), tracer.start_as_current_span("outer"):
        with bound(node="triage_agent"), tracer.start_as_current_span("inner"):
            pass
    with tracer.start_as_current_span("unbound"):
        pass

    spans = {s.name: dict(s.attributes or {}) for s in exporter.get_finished_spans()}
    assert spans["outer"]["atlas.case_id"] == "CASE-T1" and "atlas.node" not in spans["outer"]
    assert spans["inner"]["atlas.case_id"] == "CASE-T1"
    assert spans["inner"]["atlas.node"] == "triage_agent"
    assert "atlas.case_id" not in spans["unbound"]
    assert "atlas.agent_version" in spans["unbound"]
