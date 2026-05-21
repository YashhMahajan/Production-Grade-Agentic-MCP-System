"""Atomic vector search tool (Qdrant).

Exposes dense retrieval as a single atomic operation. The tool does NOT
embed the query — that is the caller's job. Agents that want end-to-end
"text in, passages out" should call the composed ``semantic_search`` tool,
which chains embedding + vector + rerank.

Separating embedding from search means:

* Agents can reuse an embedding across multiple searches.
* Embedding model changes do not require a server redeploy.
* The vector tool has the narrowest possible blast radius.
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, Field

from atlas_mcp.auth.policy import PolicyEngine
from atlas_mcp.config import get_settings
from atlas_mcp.errors.framework import UpstreamError
from atlas_mcp.tools.base import Tool, ToolLevel, ToolMetadata


class VectorSearchInput(BaseModel):
    collection: str = Field(..., description="Qdrant collection name.")
    vector: list[float] = Field(..., min_length=1, description="Query embedding.")
    top_k: int = Field(10, ge=1, le=100)
    filter: dict[str, Any] | None = Field(default=None, description="Qdrant filter object.")


class VectorSearchTool(Tool):
    meta: ClassVar[ToolMetadata] = ToolMetadata(
        name="vector.search",
        description="Nearest-neighbour search against a Qdrant collection.",
        level=ToolLevel.ATOMIC,
        scopes_required=("tool:vector:read",),
        cacheable=True,
        cache_ttl_seconds=300,
        timeout_ms=2_000,
        tags=("vector", "qdrant", "retrieval"),
    )
    input_schema: ClassVar[type[BaseModel]] = VectorSearchInput

    def __init__(self, policy: PolicyEngine):
        super().__init__(policy)
        self.settings = get_settings()

    async def run(self, tenant: str, args: VectorSearchInput) -> dict:  # type: ignore[override]
        # Tenant isolation: we inject a filter clause that pins the tenant.
        tenant_filter = {"must": [{"key": "tenant", "match": {"value": tenant}}]}
        combined_filter = tenant_filter
        if args.filter:
            # Merge user filter under the tenant-mandatory one.
            combined_filter = {
                "must": tenant_filter["must"] + (args.filter.get("must") or []),
                "should": args.filter.get("should"),
                "must_not": args.filter.get("must_not"),
            }
            # Remove None values.
            combined_filter = {k: v for k, v in combined_filter.items() if v is not None}

        url = f"{self.settings.vector_db_url.rstrip('/')}/collections/{args.collection}/points/search"
        body = {"vector": args.vector, "limit": args.top_k, "filter": combined_filter, "with_payload": True}

        try:
            async with httpx.AsyncClient(timeout=self.meta.timeout_ms / 1000) as client:
                resp = await client.post(url, json=body)
                resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise UpstreamError("vector_timeout", retryable=True, hint=str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            retryable = exc.response.status_code in (502, 503, 504)
            raise UpstreamError(
                "vector_http_error",
                retryable=retryable,
                hint=f"qdrant returned {exc.response.status_code}",
                context={"status": exc.response.status_code},
            ) from exc
        except httpx.RequestError as exc:
            raise UpstreamError("vector_network_error", retryable=True, hint=str(exc)) from exc

        payload = resp.json()
        return {
            "matches": [
                {"id": r["id"], "score": r["score"], "payload": r.get("payload") or {}}
                for r in payload.get("result", [])
            ]
        }
