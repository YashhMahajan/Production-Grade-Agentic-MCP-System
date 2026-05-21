"""Component 5 — Validation schemas.

The tool envelope is the one shape the dispatch pipeline speaks. Every MCP
request that reaches ``_dispatch`` has been normalised into this dataclass,
so auth, rate limiting, caching, and execution all read from the same object.

Per-tool input validation lives on the :class:`Tool` subclasses themselves.
This module only describes the envelope around the tool call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolCallEnvelope:
    """Normalised shape of an incoming MCP tool call."""

    tool: str
    arguments: dict[str, Any]
    tenant: str
    caller: str
    # The trace ID threads through metrics, logs, and OTel spans so you can
    # pivot from a Grafana panel to the Jaeger trace to the audit log line
    # that describes a single agent call.
    trace_id: str | None = None
    # Delegator is the human who authorised the agent. Audit logs include it
    # so "who actually asked for this" survives the agent layer.
    delegator: str | None = None
