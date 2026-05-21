"""Component 10 — Structured Error Recovery Framework.

Agents cannot recover from Python tracebacks. They cannot recover from plain
English error strings either. What they can recover from is *structured*
errors — a small, stable vocabulary that tells the agent:

* what went wrong (``code``),
* whether retrying will help (``retryable``),
* and what to try differently (``hint``).

This module defines that vocabulary. Every other component in Atlas-MCP
raises one of these error types; nothing else leaks to the agent.

Inspired by the Structured Error Recovery Framework (SERF) described in
*Bridging Protocol and Production: Design Patterns for Deploying AI Agents
with Model Context Protocol* (arXiv, March 2026).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp.types import ErrorData


# ── Error taxonomy ────────────────────────────────────────────────────────
# A flat, fixed set of codes. Do not introduce new codes casually — each one
# is effectively a new branch the agent's prompt must learn to handle.


@dataclass
class ToolError(Exception):
    """Base class for every error Atlas-MCP surfaces to an agent."""

    code: str
    retryable: bool = False
    hint: str | None = None
    context: dict[str, Any] | None = None

    def __str__(self) -> str:
        return f"{self.code}: {self.hint or ''}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "retryable": self.retryable,
            "hint": self.hint,
            "context": self.context or {},
        }


class AuthError(ToolError):
    """Token missing, expired, or invalid."""


class PolicyError(ToolError):
    """The caller is authenticated but not permitted."""


class ValidationError(ToolError):
    """Input failed schema or constraint validation."""


class RateLimitError(ToolError):
    """Per-tenant or per-tool quota exceeded."""

    def __init__(self, retry_after_seconds: float, hint: str | None = None):
        super().__init__(
            code="rate_limited",
            retryable=True,
            hint=hint or f"retry after {retry_after_seconds:.1f}s",
            context={"retry_after_seconds": retry_after_seconds},
        )


class UpstreamError(ToolError):
    """An upstream system (Postgres, Elasticsearch, S3) failed.

    ``retryable`` is True for transient failures (timeout, 503) and False for
    deterministic failures (syntax error, 400). The agent uses this to decide
    whether to re-issue the same call or back off.
    """


class CircuitOpenError(ToolError):
    """Circuit breaker is open — stop calling this tool for now."""

    def __init__(self, tool: str, recovery_seconds: int):
        super().__init__(
            code="circuit_open",
            retryable=True,
            hint=f"tool {tool!r} is temporarily disabled; retry after {recovery_seconds}s",
            context={"tool": tool, "recovery_seconds": recovery_seconds},
        )


class TimeoutError_(ToolError):  # trailing underscore avoids shadowing builtin
    """ATBA budget or per-tool timeout was exhausted."""

    def __init__(self, tool: str, budget_ms: int):
        super().__init__(
            code="timeout",
            retryable=True,
            hint=f"tool {tool!r} exceeded {budget_ms}ms budget",
            context={"tool": tool, "budget_ms": budget_ms},
        )


class ToolNotFoundError(ToolError):
    def __init__(self, name: str):
        super().__init__(
            code="tool_not_found",
            retryable=False,
            hint=f"unknown tool {name!r}",
            context={"tool": name},
        )


# ── Wire-format conversion ────────────────────────────────────────────────
def to_mcp_error(exc: ToolError) -> ErrorData:
    """Convert an Atlas :class:`ToolError` into the MCP on-the-wire shape.

    The MCP protocol allows structured ``data`` on errors. We use it to carry
    our full SERF payload, so MCP hosts that understand Atlas can parse the
    hints, and hosts that do not still get a human-readable ``message``.
    """
    return ErrorData(
        code=-32000,  # JSON-RPC application error
        message=f"{exc.code}: {exc.hint or ''}",
        data=exc.to_dict(),
    )
