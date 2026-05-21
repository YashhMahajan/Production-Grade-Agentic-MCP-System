"""Component 7 — Adaptive Timeout Budget Allocation.

An agent that chains five tools with a 10-second timeout on each can block
for 50 seconds before giving up. That is fine in isolation and a disaster
inside a user-facing chat. ATBA fixes this by allocating a *total* budget
for the entire agent turn and spending it across tool calls proportional
to their observed latency.

The idea follows the production pattern described in *Bridging Protocol
and Production: Design Patterns for Deploying AI Agents with Model Context
Protocol* (arXiv 2026): the server tracks p95 latency per tool, and each
tool call's timeout is set to ``max(p95 * safety, remaining_budget / calls_left)``.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import ContextManager


@dataclass
class BudgetContext:
    """Lifespan of a single agent request's time budget."""

    total_budget_s: float
    started_at: float
    calls_made: int = 0
    spent_s: float = 0.0

    @property
    def remaining_s(self) -> float:
        return max(0.0, self.total_budget_s - (time.monotonic() - self.started_at))


_current_budget: contextvars.ContextVar[BudgetContext | None] = contextvars.ContextVar(
    "atlas_budget", default=None
)


class LatencyTracker:
    """Keeps a rolling window of per-tool durations for p95 estimation."""

    def __init__(self, window: int = 500):
        self.window = window
        self._samples: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=window))

    def record(self, tool: str, duration_s: float) -> None:
        self._samples[tool].append(duration_s)

    def p95(self, tool: str, default_s: float = 5.0) -> float:
        samples = self._samples.get(tool)
        if not samples or len(samples) < 20:
            return default_s
        sorted_samples = sorted(samples)
        idx = int(len(sorted_samples) * 0.95)
        return sorted_samples[min(idx, len(sorted_samples) - 1)]


class ATBA:
    """Allocates tool-call timeouts from a shared budget."""

    SAFETY_FACTOR = 1.5  # allow 50% headroom over p95
    MIN_CALL_TIMEOUT_S = 0.5
    EXPECTED_CALLS_PER_TURN = 5

    def __init__(self, total_budget_ms: int):
        self.total_budget_s = total_budget_ms / 1000.0
        self.tracker = LatencyTracker()

    def begin(self) -> BudgetContext:
        ctx = BudgetContext(
            total_budget_s=self.total_budget_s, started_at=time.monotonic()
        )
        _current_budget.set(ctx)
        return ctx

    def timeout_for(self, tool: str) -> float:
        """Compute a per-call timeout given current budget + historical latency."""
        ctx = _current_budget.get()
        p95 = self.tracker.p95(tool)
        target = p95 * self.SAFETY_FACTOR
        if ctx is None:
            return max(self.MIN_CALL_TIMEOUT_S, target)

        remaining = ctx.remaining_s
        # Estimate how many calls are likely still to come.
        calls_left = max(1, self.EXPECTED_CALLS_PER_TURN - ctx.calls_made)
        fair_share = remaining / calls_left
        # Take the smaller of "fair share of remaining budget" and "p95 * safety".
        # If the budget is tight, shrink; if the tool is fast, do not over-allocate.
        return max(self.MIN_CALL_TIMEOUT_S, min(target, fair_share))

    async def call_with_budget(self, tool: str, coro) -> any:
        """Run ``coro`` under the per-call timeout derived from the budget."""
        timeout = self.timeout_for(tool)
        ctx = _current_budget.get()
        if ctx is not None:
            ctx.calls_made += 1

        started = time.monotonic()
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        finally:
            duration = time.monotonic() - started
            self.tracker.record(tool, duration)
            if ctx is not None:
                ctx.spent_s += duration
