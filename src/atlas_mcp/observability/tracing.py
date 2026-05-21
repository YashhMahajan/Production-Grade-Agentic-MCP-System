"""Component 11 — Distributed tracing.

Every MCP tool call becomes an OpenTelemetry span with:

* ``atlas.tool`` — tool name
* ``atlas.tenant`` — tenant id
* ``atlas.cache`` — hit / miss / bypass
* ``atlas.retryable_error`` — if the error is one the agent could retry
* ``atlas.circuit_state`` — CLOSED / HALF_OPEN / OPEN at call time

Traces are exported via OTLP to whatever collector you point at
``ATLAS_OTEL_ENDPOINT``. In docker-compose that is a local OTel Collector
that forwards to Jaeger.

The point of tracing in an MCP server is not pretty graphs; it is so that
when an agent gets a weird answer you can find the specific span where it
went wrong and see the exact arguments, the exact upstream response, and
the exact latency.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from atlas_mcp.config import ServerSettings


_INITIALISED = False


def init_tracing(settings: ServerSettings) -> None:
    """Idempotent — safe to call multiple times."""
    global _INITIALISED
    if _INITIALISED:
        return

    resource = Resource.create({
        "service.name": settings.service_name,
        "service.version": "0.1.0",
    })
    provider = TracerProvider(resource=resource)

    exporter = OTLPSpanExporter(endpoint=settings.otel_endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)
    _INITIALISED = True


def get_tracer():
    return trace.get_tracer("atlas_mcp")


def current_trace_id() -> str | None:
    span = trace.get_current_span()
    if span is None or not span.is_recording():
        return None
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return None
    return f"{ctx.trace_id:032x}"
