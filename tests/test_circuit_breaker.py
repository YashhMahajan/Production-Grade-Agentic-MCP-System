"""Circuit breaker state machine tests."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from atlas_mcp.errors.framework import CircuitOpenError, UpstreamError, ValidationError
from atlas_mcp.reliability.circuit_breaker import CircuitBreaker, State


async def _transient_failure():
    raise UpstreamError(code="transient", retryable=True, hint="backend down")


async def _deterministic_failure():
    raise ValidationError(code="bad_input", retryable=False)


async def _success():
    return {"ok": True}


async def test_opens_after_threshold_transient_failures():
    cb = CircuitBreaker("test", failure_threshold=3, recovery_seconds=1)
    for _ in range(3):
        with pytest.raises(UpstreamError):
            await cb.call(_transient_failure)
    assert cb.state is State.OPEN

    # Further calls short-circuit without invoking the backend.
    with pytest.raises(CircuitOpenError):
        await cb.call(_transient_failure)


async def test_deterministic_errors_do_not_open_circuit():
    cb = CircuitBreaker("test", failure_threshold=3, recovery_seconds=1)
    for _ in range(10):
        with pytest.raises(ValidationError):
            await cb.call(_deterministic_failure)
    assert cb.state is State.CLOSED


async def test_half_open_probe_closes_on_success():
    cb = CircuitBreaker("test", failure_threshold=2, recovery_seconds=1)
    for _ in range(2):
        with pytest.raises(UpstreamError):
            await cb.call(_transient_failure)
    assert cb.state is State.OPEN

    # Wait past the recovery window.
    await asyncio.sleep(1.1)

    # The next call probes in HALF_OPEN; a success should close the breaker.
    result = await cb.call(_success)
    assert result == {"ok": True}
    assert cb.state is State.CLOSED


async def test_half_open_probe_reopens_on_failure():
    cb = CircuitBreaker("test", failure_threshold=2, recovery_seconds=1)
    for _ in range(2):
        with pytest.raises(UpstreamError):
            await cb.call(_transient_failure)

    await asyncio.sleep(1.1)

    with pytest.raises(UpstreamError):
        await cb.call(_transient_failure)
    assert cb.state is State.OPEN
