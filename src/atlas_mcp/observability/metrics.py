"""Component 11 — Prometheus metrics.

Each metric has *just enough* labels to be useful and not a cardinality bomb.
Notably, we do NOT label by tenant or caller — those explode the series
count and belong in logs and traces, not gauges.

Grafana dashboard consuming these metrics ships in ``deploy/grafana/``.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.requests import Request
from starlette.responses import Response


class MetricsRegistry:
    """Holds all named Prometheus instruments used throughout the server."""

    def __init__(self, registry: CollectorRegistry | None = None):
        self.registry = registry or CollectorRegistry()

        self.calls_total = Counter(
            "atlas_tool_calls_total",
            "Total tool invocations.",
            ["tool", "status"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "atlas_tool_latency_seconds",
            "End-to-end tool latency.",
            ["tool"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
            registry=self.registry,
        )
        self.cache_hit = Counter(
            "atlas_cache_hits_total",
            "Cache hits (L1 + L2).",
            ["tool"],
            registry=self.registry,
        )
        self.cache_miss = Counter(
            "atlas_cache_misses_total",
            "Cache misses that hit the tool.",
            ["tool"],
            registry=self.registry,
        )
        self.rate_limited = Counter(
            "atlas_rate_limited_total",
            "Requests rejected by the rate limiter.",
            ["tool"],
            registry=self.registry,
        )
        self.circuit_state = Gauge(
            "atlas_circuit_state",
            "Circuit breaker state (0=closed, 1=half_open, 2=open).",
            ["tool"],
            registry=self.registry,
        )
        self.active_sessions = Gauge(
            "atlas_active_sessions",
            "Active MCP sessions on this replica.",
            registry=self.registry,
        )


# Global registry used by the Starlette endpoint. The server owns a reference
# and should keep all instruments on it; for brevity the HTTP handler pulls
# the default global registry created on first use.
_registry_singleton: MetricsRegistry | None = None


def _get_registry() -> MetricsRegistry:
    global _registry_singleton
    if _registry_singleton is None:
        _registry_singleton = MetricsRegistry()
    return _registry_singleton


async def metrics_endpoint(_request: Request) -> Response:
    registry = _get_registry().registry
    return Response(
        content=generate_latest(registry),
        media_type=CONTENT_TYPE_LATEST,
    )
