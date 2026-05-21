"""Component 7 — Retry policy.

Only retryable errors get retried. A retryable error is one whose
:class:`ToolError.retryable` attribute is True — rate limits, 5xx, timeouts,
circuit probes. Everything else fails fast so the agent can adapt.

Uses tenacity under the hood but wraps it in an async-friendly helper that
respects a per-call deadline. The deadline is an ATBA budget (see
``reliability/atba.py``); we will not retry past it.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable

from atlas_mcp.errors.framework import ToolError


async def with_retry(
    fn: Callable[..., Awaitable[Any]],
    *args,
    max_attempts: int = 3,
    base_delay_ms: int = 100,
    max_delay_ms: int = 2_000,
    deadline_s: float | None = None,
    **kwargs,
) -> Any:
    """Call ``fn`` with capped exponential backoff and jitter.

    Parameters
    ----------
    deadline_s
        Absolute deadline (``asyncio.get_event_loop().time()`` units). If
        set, the retry loop bails out before the deadline instead of sleeping
        past it.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn(*args, **kwargs)
        except ToolError as exc:
            last_exc = exc
            if not exc.retryable or attempt == max_attempts:
                raise
            delay = _backoff(attempt, base_delay_ms, max_delay_ms)
            if deadline_s is not None:
                remaining = deadline_s - asyncio.get_event_loop().time()
                if remaining <= delay:
                    raise
            await asyncio.sleep(delay)
    # Unreachable — keeps the type checker happy.
    assert last_exc is not None
    raise last_exc


def _backoff(attempt: int, base_ms: int, max_ms: int) -> float:
    """Full-jitter backoff: delay = random(0, min(max, base*2^attempt))."""
    cap = min(max_ms, base_ms * (2 ** (attempt - 1)))
    return random.uniform(0, cap) / 1000.0
