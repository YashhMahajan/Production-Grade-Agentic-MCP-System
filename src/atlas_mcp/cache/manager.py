"""Component 9 — Caching Layer.

Two tiers:

* **L1 (in-process):** an async-safe, TTL-aware LRU. Sub-millisecond hits,
  invalidated by process restart. Good for very hot keys (list_tools,
  catalog lookups).
* **L2 (Redis):** shared across replicas. ~1 ms hits. Survives deploys.

Reads check L1 → L2 → miss. Writes populate both tiers (write-through).

Cache stampede prevention: when N agents miss the same key simultaneously,
only one should compute the value. We take a short Redis lock keyed on the
cache key; losers briefly wait and re-read. This is the *probabilistic* style
described in the MintMCP and Composio gateway docs, kept simple.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from atlas_mcp.config import ServerSettings


@dataclass
class L1Entry:
    value: Any
    expires_at: float


class L1Cache:
    """Async-safe in-process LRU with per-entry TTL."""

    def __init__(self, max_items: int):
        self.max_items = max_items
        self._store: OrderedDict[str, L1Entry] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if entry.expires_at < time.monotonic():
                self._store.pop(key, None)
                return None
            # Move to end for LRU.
            self._store.move_to_end(key)
            return entry.value

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        async with self._lock:
            self._store[key] = L1Entry(value=value, expires_at=time.monotonic() + ttl_seconds)
            self._store.move_to_end(key)
            while len(self._store) > self.max_items:
                self._store.popitem(last=False)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)


class CacheManager:
    """Coordinates L1 + L2 with stampede locks."""

    STAMPEDE_LOCK_TTL_MS = 5_000
    STAMPEDE_WAIT_MS = 50

    def __init__(self, settings: ServerSettings):
        self.settings = settings
        self.l1 = L1Cache(max_items=settings.cache_l1_max_items)
        self._redis: Redis | None = None

    async def connect(self) -> None:
        self._redis = Redis.from_url(self.settings.redis_url, decode_responses=True)

    async def disconnect(self) -> None:
        if self._redis is not None:
            await self._redis.close()
            self._redis = None

    # ── Read path ─────────────────────────────────────────────────────────
    async def get(self, key: str) -> Any | None:
        # L1 hit?
        if (hit := await self.l1.get(key)) is not None:
            return hit
        # L2 hit?
        if self._redis is not None:
            raw = await self._redis.get(key)
            if raw is not None:
                value = json.loads(raw)
                # Populate L1 for next time.
                await self.l1.set(key, value, ttl_seconds=self.settings.cache_l1_ttl_seconds)
                return value
        return None

    # ── Write path ────────────────────────────────────────────────────────
    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        ttl_l2 = ttl or self.settings.cache_l2_ttl_seconds
        ttl_l1 = min(ttl_l2, self.settings.cache_l1_ttl_seconds)
        await self.l1.set(key, value, ttl_seconds=ttl_l1)
        if self._redis is not None:
            await self._redis.set(key, json.dumps(value, default=str), ex=ttl_l2)

    async def delete(self, key: str) -> None:
        await self.l1.delete(key)
        if self._redis is not None:
            await self._redis.delete(key)

    # ── Stampede protection ───────────────────────────────────────────────
    async def get_or_compute(self, key: str, compute, ttl: int | None = None) -> Any:
        """Read-through with single-flight semantics.

        ``compute`` is a zero-arg coroutine factory. Exactly one caller will
        invoke it on a miss; others wait briefly and re-read.
        """
        if (hit := await self.get(key)) is not None:
            return hit

        assert self._redis is not None, "cache not connected"
        lock_key = f"{key}:lock"
        # NX SET returns None if someone else holds the lock.
        got_lock = await self._redis.set(lock_key, "1", nx=True, px=self.STAMPEDE_LOCK_TTL_MS)
        if got_lock:
            try:
                value = await compute()
                await self.set(key, value, ttl=ttl)
                return value
            finally:
                await self._redis.delete(lock_key)

        # Someone else is computing — wait briefly, then re-read.
        for _ in range(20):  # up to ~1 s
            await asyncio.sleep(self.STAMPEDE_WAIT_MS / 1000)
            if (hit := await self.get(key)) is not None:
                return hit
        # Lock holder died or is slow — compute ourselves.
        value = await compute()
        await self.set(key, value, ttl=ttl)
        return value
