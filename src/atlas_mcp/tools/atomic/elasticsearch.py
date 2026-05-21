"""Atomic Elasticsearch search tool."""

from __future__ import annotations

from typing import Any, ClassVar

from elasticsearch import AsyncElasticsearch, ApiError, TransportError
from pydantic import BaseModel, Field

from atlas_mcp.auth.policy import PolicyEngine
from atlas_mcp.config import get_settings
from atlas_mcp.errors.framework import UpstreamError
from atlas_mcp.tools.base import Tool, ToolLevel, ToolMetadata


class ElasticsearchSearchInput(BaseModel):
    index: str = Field(..., description="Index or index pattern.")
    query: dict = Field(..., description="Elasticsearch DSL query body.")
    size: int = Field(20, ge=1, le=200)
    fields: list[str] | None = Field(default=None)


class ElasticsearchSearchTool(Tool):
    meta: ClassVar[ToolMetadata] = ToolMetadata(
        name="elasticsearch.search",
        description="Run a DSL search against an Elasticsearch index.",
        level=ToolLevel.ATOMIC,
        scopes_required=("tool:elasticsearch:read",),
        cacheable=True,
        cache_ttl_seconds=30,
        timeout_ms=3_000,
        tags=("elasticsearch", "search", "read"),
    )
    input_schema: ClassVar[type[BaseModel]] = ElasticsearchSearchInput

    _client: AsyncElasticsearch | None = None

    def __init__(self, policy: PolicyEngine):
        super().__init__(policy)
        self.settings = get_settings()

    def _es(self) -> AsyncElasticsearch:
        if ElasticsearchSearchTool._client is None:
            ElasticsearchSearchTool._client = AsyncElasticsearch(self.settings.elasticsearch_url)
        return ElasticsearchSearchTool._client

    async def run(self, tenant: str, args: ElasticsearchSearchInput) -> dict:  # type: ignore[override]
        # Tenant isolation: every tenant's documents are tagged, and we
        # inject a mandatory filter so a tenant can never read another's data.
        body = {
            "query": {"bool": {"must": [args.query], "filter": [{"term": {"_tenant": tenant}}]}},
            "size": args.size,
        }
        if args.fields:
            body["_source"] = args.fields

        try:
            resp = await self._es().search(index=args.index, body=body)
        except (ApiError, TransportError) as exc:
            status = getattr(exc, "status_code", None)
            retryable = status in (408, 429, 500, 502, 503, 504)
            raise UpstreamError(
                code="elasticsearch_error",
                retryable=retryable,
                hint=str(exc)[:200],
                context={"status": status},
            ) from exc

        hits = resp["hits"]["hits"]
        return {
            "total": resp["hits"]["total"]["value"],
            "took_ms": resp["took"],
            "hits": [
                {"id": h["_id"], "score": h["_score"], "source": h.get("_source", {})}
                for h in hits
            ],
        }
