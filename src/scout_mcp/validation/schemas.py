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


# Names that end up inside a URL path or an index expression. Anything outside these
# patterns ("../x", "a?b", ".security") could steer the request at another endpoint.
COLLECTION_NAME = r"^[A-Za-z0-9_\-]{1,64}$"
INDEX_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_\-*]{0,127}$"


# slots=True makes it memory-efficient, which matters when you are processing thousands of calls per second.
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
    # What the token grants. Authorization re-checks these on every call.
    scopes: frozenset[str] = frozenset()
    roles: frozenset[str] = frozenset()
