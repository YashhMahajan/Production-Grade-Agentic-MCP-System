"""Component 7 — Circuit breaker.

An async circuit breaker, one per tool, with the classic three-state machine:

* ``CLOSED``      — requests flow, failures are counted in a sliding window.
* ``OPEN``        — requests short-circuit immediately with ``CircuitOpenError``
                    for ``recovery_seconds``.
* ``HALF_OPEN``   — one probe request is allowed through. Success closes the
                    breaker; failure re-opens it for another recovery window.

The breaker counts *only* retryable / transient upstream failures. A
deterministic error (bad SQL, 404) should not open a breaker because the same
call will keep failing; that is what the :class:`UpstreamError.retryable`
flag in Component 10 is for.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from enum import Enum
from typing import Any, Awaitable, Callable

from atlas_mcp.config import ServerSettings
from atlas_mcp.errors.framework import CircuitOpenError, ToolError, UpstreamError


class State(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int,
        recovery_seconds: int,
        window_seconds: int = 60,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.window_seconds = window_seconds

        self._state: State = State.CLOSED
        self._opened_at: float = 0.0
        self._failures: deque[float] = deque()
        self._half_open_probe_in_flight = False
        self._lock = asyncio.Lock()

    # ── Public API ────────────────────────────────────────────────────────
    async def call(self, fn: Callable[..., Awaitable[Any]], *args, **kwargs) -> Any:
        await self._check_transitions()

        if self._state is State.OPEN:
            raise CircuitOpenError(tool=self.name, recovery_seconds=self.recovery_seconds)

        if self._state is State.HALF_OPEN:
            async with self._lock:
                if self._half_open_probe_in_flight:
                    # Only one probe at a time; subsequent callers short-circuit.
                    raise CircuitOpenError(tool=self.name, recovery_seconds=self.recovery_seconds)
                self._half_open_probe_in_flight = True
            try:
                result = await fn(*args, **kwargs)
                await self._on_success()
                return result
            except ToolError as exc:
                await self._on_failure(exc)
                raise
            finally:
                async with self._lock:
                    self._half_open_probe_in_flight = False

        # CLOSED
        try:
            return await fn(*args, **kwargs)
        except ToolError as exc:
            await self._on_failure(exc)
            raise

    @property
    def state(self) -> State:
        return self._state

    # ── State transitions ─────────────────────────────────────────────────
    async def _check_transitions(self) -> None:
        now = time.monotonic()
        # OPEN → HALF_OPEN once the recovery window elapses.
        if self._state is State.OPEN and now - self._opened_at >= self.recovery_seconds:
            async with self._lock:
                if self._state is State.OPEN:
                    self._state = State.HALF_OPEN
                    self._half_open_probe_in_flight = False
        # Evict failures older than the window.
        cutoff = now - self.window_seconds
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()

    async def _on_failure(self, exc: ToolError) -> None:
        # Only open on genuinely transient upstream failures.
        if not (isinstance(exc, UpstreamError) and exc.retryable):
            return
        now = time.monotonic()
        async with self._lock:
            self._failures.append(now)
            if self._state is State.HALF_OPEN:
                # Probe failed — re-open immediately.
                self._state = State.OPEN
                self._opened_at = now
                self._failures.clear()
                return
            if len(self._failures) >= self.failure_threshold:
                self._state = State.OPEN
                self._opened_at = now
                self._failures.clear()

    async def _on_success(self) -> None:
        async with self._lock:
            if self._state is State.HALF_OPEN:
                self._state = State.CLOSED
                self._failures.clear()


class CircuitBreakerRegistry:
    """One breaker per tool — tools fail independently."""

    def __init__(self, settings: ServerSettings):
        self.settings = settings
        self._breakers: dict[str, CircuitBreaker] = {}

    def for_tool(self, tool_name: str) -> CircuitBreaker:
        if tool_name not in self._breakers:
            self._breakers[tool_name] = CircuitBreaker(
                name=tool_name,
                failure_threshold=self.settings.circuit_breaker_failure_threshold,
                recovery_seconds=self.settings.circuit_breaker_recovery_seconds,
            )
        return self._breakers[tool_name]

    def snapshot(self) -> dict[str, str]:
        return {name: b.state.value for name, b in self._breakers.items()}
