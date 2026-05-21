"""Component 8 — Rate Limiting & Quotas.

A Redis-backed token bucket with atomic refill. Keyed on
``(tenant, tool)`` so noisy agents cannot starve other tenants and runaway
loops on one tool cannot starve calls to another.

Why Redis: the server is horizontally scaled. An in-process limiter on each
replica would let an agent N-x its quota by getting load-balanced across N
replicas. Redis is the shared truth.

Why token bucket over leaky bucket or fixed window: it permits bursts
naturally, which is how agents actually behave — a burst of six tool calls
while planning, then quiet while reasoning.

The refill logic is a single Lua script so check-and-consume is atomic.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from redis.asyncio import Redis

from atlas_mcp.config import ServerSettings
from atlas_mcp.errors.framework import RateLimitError


# Lua script: atomic token-bucket refill + consume.
# Returns {allowed, retry_after_ms, tokens_remaining}
_LUA_SCRIPT = """
local key         = KEYS[1]
local capacity    = tonumber(ARGV[1])
local refill_per_s= tonumber(ARGV[2])
local now_ms      = tonumber(ARGV[3])
local cost        = tonumber(ARGV[4])

local bucket = redis.call('HMGET', key, 'tokens', 'last_ms')
local tokens  = tonumber(bucket[1]) or capacity
local last_ms = tonumber(bucket[2]) or now_ms

-- Refill based on elapsed time.
local elapsed_s = math.max(0, (now_ms - last_ms) / 1000.0)
tokens = math.min(capacity, tokens + elapsed_s * refill_per_s)

local allowed = 0
local retry_after_ms = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  local deficit = cost - tokens
  retry_after_ms = math.ceil((deficit / refill_per_s) * 1000)
end

redis.call('HMSET', key, 'tokens', tokens, 'last_ms', now_ms)
redis.call('EXPIRE', key, 3600)

return {allowed, retry_after_ms, tokens}
"""


@dataclass(frozen=True, slots=True)
class Quota:
    capacity: int        # Maximum tokens in the bucket.
    refill_per_minute: int  # How many tokens are replenished per minute.


class RateLimiter:
    """Per-(tenant, tool) Redis token bucket."""

    def __init__(self, settings: ServerSettings):
        self.settings = settings
        self._redis: Redis | None = None
        self._script_sha: str | None = None

        # Default quota — overridden per-tool below.
        self._default = Quota(
            capacity=settings.rate_limit_burst,
            refill_per_minute=settings.rate_limit_default_rpm,
        )
        # Per-tool overrides. Extend this table as you add expensive tools.
        self._overrides: dict[str, Quota] = {
            # Expensive workflow tools get tighter quotas.
            "research.topic": Quota(capacity=3, refill_per_minute=10),
            "semantic_search": Quota(capacity=30, refill_per_minute=120),
        }

    async def connect(self) -> None:
        self._redis = Redis.from_url(self.settings.redis_url, decode_responses=True)
        self._script_sha = await self._redis.script_load(_LUA_SCRIPT)

    async def disconnect(self) -> None:
        if self._redis is not None:
            await self._redis.close()
            self._redis = None

    def quota_for(self, tool: str) -> Quota:
        return self._overrides.get(tool, self._default)

    async def acquire(self, tenant: str, tool: str, cost: int = 1) -> None:
        """Consume ``cost`` tokens; raise :class:`RateLimitError` on exhaustion."""
        assert self._redis is not None and self._script_sha is not None, "call connect() first"

        quota = self.quota_for(tool)
        refill_per_s = quota.refill_per_minute / 60.0
        key = f"atlas:rl:{tenant}:{tool}"

        now_ms = int(time.time() * 1000)
        allowed, retry_after_ms, _tokens = await self._redis.evalsha(
            self._script_sha,
            1,
            key,
            quota.capacity,
            refill_per_s,
            now_ms,
            cost,
        )

        if int(allowed) == 0:
            raise RateLimitError(retry_after_seconds=int(retry_after_ms) / 1000.0)
