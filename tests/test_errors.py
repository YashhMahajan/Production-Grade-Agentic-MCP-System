"""Structured Error Recovery Framework — wire format and retry semantics."""

from __future__ import annotations

from atlas_mcp.errors.framework import (
    AuthError,
    CircuitOpenError,
    RateLimitError,
    ToolError,
    UpstreamError,
    to_mcp_error,
)


def test_retryable_flag_propagates_through_to_dict():
    exc = UpstreamError(code="es_timeout", retryable=True, hint="elasticsearch slow")
    payload = exc.to_dict()
    assert payload["code"] == "es_timeout"
    assert payload["retryable"] is True
    assert "elasticsearch" in payload["hint"]


def test_rate_limit_carries_retry_after():
    exc = RateLimitError(retry_after_seconds=2.5)
    assert exc.retryable is True
    assert exc.context["retry_after_seconds"] == 2.5
    assert "2.5" in exc.hint


def test_circuit_open_error_has_tool_and_recovery():
    exc = CircuitOpenError(tool="postgres.query", recovery_seconds=30)
    assert exc.context["tool"] == "postgres.query"
    assert exc.context["recovery_seconds"] == 30


def test_mcp_wire_format_includes_serf_payload():
    exc = AuthError(code="token_expired", retryable=True, hint="refresh")
    mcp_err = to_mcp_error(exc)
    assert mcp_err.code == -32000
    assert "token_expired" in mcp_err.message
    assert mcp_err.data["retryable"] is True
    assert mcp_err.data["hint"] == "refresh"
