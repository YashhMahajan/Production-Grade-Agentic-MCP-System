"""Timeouts, the circuit breaker and its gauge, as the server wires them."""

from __future__ import annotations

import asyncio
import dataclasses

from scout_mcp.tools.atomic.postgres import PostgresQueryTool

from tests.conftest import call

SQL = {"sql": "SELECT id FROM orders"}


async def test_slow_tool_hits_its_timeout_then_the_circuit_opens(app_client, monkeypatch):
    # The tool declares 100 ms; the fake takes a second.
    monkeypatch.setattr(PostgresQueryTool, "meta",
                        dataclasses.replace(PostgresQueryTool.meta, timeout_ms=100))
    runs = []

    async def slow(self, tenant, args):
        runs.append(1)
        await asyncio.sleep(1)
        return {}
    monkeypatch.setattr(PostgresQueryTool, "run", slow)

    for _ in range(5):  # failure threshold is 5
        is_error, payload = await call(app_client, "postgres.query", SQL)
        assert is_error and payload["code"] == "timeout" and payload["retryable"] is True

    ran_before = len(runs)
    is_error, payload = await call(app_client, "postgres.query", SQL)
    assert is_error and payload["code"] == "circuit_open"
    assert len(runs) == ran_before  # the open circuit never reached the tool

    text = (await app_client.get("/metrics")).text
    assert 'scout_circuit_state{tool="postgres.query"} 2.0' in text


async def test_circuit_gauge_is_zero_while_healthy(app_client, monkeypatch):
    async def fast(self, tenant, args):
        return {"columns": [], "rows": [], "row_count": 0, "truncated": False}
    monkeypatch.setattr(PostgresQueryTool, "run", fast)
    await call(app_client, "postgres.query", SQL)
    assert 'scout_circuit_state{tool="postgres.query"} 0.0' in (await app_client.get("/metrics")).text


async def test_transient_failures_are_retried_inside_one_call(app_client, monkeypatch):
    from scout_mcp.errors.framework import UpstreamError
    attempts = []

    async def flaky(self, tenant, args):
        attempts.append(1)
        if len(attempts) < 3:
            raise UpstreamError("postgres_error", retryable=True, hint="blip")
        return {"columns": [], "rows": [], "row_count": 0, "truncated": False}
    monkeypatch.setattr(PostgresQueryTool, "run", flaky)
    is_error, _ = await call(app_client, "postgres.query", SQL)
    assert not is_error and len(attempts) == 3
