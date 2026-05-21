"""Short- and long-term memory for the copilot.

* **Short-term memory (STM)** — the running conversation inside a single
  user session. Stored in Redis with a short TTL, keyed on session id.
* **Long-term memory (LTM)** — durable facts about a customer (known
  preferences, past resolutions). Written to the same vector collection
  that the retriever reads from, so the next time this customer opens a
  ticket, semantic_search surfaces it automatically.

The copilot does not need memory to function; the orchestrator works with
no memory at all. But in production, adding STM gives the agents
conversational follow-up ("what about my other order?") and LTM turns the
copilot from reactive to proactive over time.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from redis.asyncio import Redis


# ── Short-term (conversation) memory ──────────────────────────────────────
@dataclass
class Turn:
    role: str  # "user" or "assistant"
    content: str
    created_at: float = field(default_factory=time.time)


class ShortTermMemory:
    """Per-session conversation buffer stored in Redis."""

    TTL_SECONDS = 3600  # Sessions expire after an hour of inactivity.
    MAX_TURNS = 20      # Trim to the most recent 20 turns.

    def __init__(self, redis: Redis):
        self._redis = redis

    async def append(self, session_id: str, turn: Turn) -> None:
        key = self._key(session_id)
        await self._redis.rpush(key, json.dumps(asdict(turn)))
        await self._redis.ltrim(key, -self.MAX_TURNS, -1)
        await self._redis.expire(key, self.TTL_SECONDS)

    async def history(self, session_id: str) -> list[Turn]:
        raw = await self._redis.lrange(self._key(session_id), 0, -1)
        return [Turn(**json.loads(r)) for r in raw]

    async def clear(self, session_id: str) -> None:
        await self._redis.delete(self._key(session_id))

    @staticmethod
    def _key(session_id: str) -> str:
        return f"atlas:stm:{session_id}"


# ── Long-term (semantic) memory ───────────────────────────────────────────
@dataclass
class MemoryRecord:
    customer_id: str
    tenant: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class LongTermMemory:
    """Writes durable facts to the Atlas vector store via MCP.

    Why go through MCP rather than calling Qdrant directly: policy. A fact
    written by the copilot must pass the same authorisation rules as any
    other write. No back doors into the tenant-isolated data plane.
    """

    def __init__(self, mcp_client, collection: str = "support_memory"):
        self._mcp = mcp_client
        self.collection = collection

    async def remember(self, record: MemoryRecord, embedding: list[float]) -> None:
        # In a full impl we would expose `vector.upsert` as a destructive MCP
        # tool behind the approval gate. For brevity we just persist via
        # postgres.query using a read-only path — a production system would
        # swap this for a real write tool.
        _ = (record, embedding)  # referenced to silence linters in the stub.
        raise NotImplementedError(
            "vector.upsert is not yet exposed as an MCP tool; "
            "add it with destructive=True and route via governance.approval."
        )
