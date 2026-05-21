"""Composed semantic search — Level 2.

Chains three atomic operations behind a single agent-facing tool:

1. Embed the query text.
2. Vector-search a given collection.
3. Hydrate the top results with their full document bodies from Postgres.

Why this exists as its own tool: an agent that did the same thing using
three atomic calls would burn context, make three authorisation decisions,
and get three separate cache lines. The composed tool does it once, with
one policy check and one cache entry. Agents overwhelmingly reach for the
composed tool when it exists — exactly the behaviour we want.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from atlas_mcp.auth.policy import PolicyEngine
from atlas_mcp.errors.framework import ToolError
from atlas_mcp.tools.atomic.embeddings import EmbeddingClient
from atlas_mcp.tools.atomic.postgres import PostgresQueryInput, PostgresQueryTool
from atlas_mcp.tools.atomic.vector_search import VectorSearchInput, VectorSearchTool
from atlas_mcp.tools.base import Tool, ToolLevel, ToolMetadata


class SemanticSearchInput(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000)
    collection: str = Field(..., description="Vector collection to search.")
    top_k: int = Field(5, ge=1, le=25)
    hydrate_from_postgres: bool = Field(
        True, description="If true, fetch full document bodies by id from Postgres."
    )


class SemanticSearchTool(Tool):
    meta: ClassVar[ToolMetadata] = ToolMetadata(
        name="semantic_search",
        description=(
            "Retrieve passages from the knowledge base using dense vector search. "
            "Prefer this over vector.search unless you have a pre-computed embedding."
        ),
        level=ToolLevel.COMPOSED,
        scopes_required=("tool:vector:read", "tool:postgres:read"),
        cacheable=True,
        cache_ttl_seconds=120,
        timeout_ms=8_000,
        tags=("rag", "retrieval", "semantic"),
    )
    input_schema: ClassVar[type[BaseModel]] = SemanticSearchInput

    def __init__(self, policy: PolicyEngine):
        super().__init__(policy)
        self.embedder = EmbeddingClient()
        self.vector_tool = VectorSearchTool(policy)
        self.postgres_tool = PostgresQueryTool(policy)

    async def run(self, tenant: str, args: SemanticSearchInput) -> dict:  # type: ignore[override]
        # 1. Embed.
        vectors = await self.embedder.embed([args.query])
        query_vector = vectors[0]

        # 2. Vector search.
        vs_args = VectorSearchInput(
            collection=args.collection, vector=query_vector, top_k=args.top_k
        )
        vs_result = await self.vector_tool.run(tenant, vs_args)
        matches = vs_result["matches"]
        if not matches:
            return {"query": args.query, "results": []}

        # 3. Optional hydration from Postgres — bulk lookup by id.
        if not args.hydrate_from_postgres:
            return {
                "query": args.query,
                "results": [
                    {
                        "id": m["id"],
                        "score": m["score"],
                        "preview": (m["payload"].get("text") or "")[:300],
                        "metadata": {k: v for k, v in m["payload"].items() if k != "text"},
                    }
                    for m in matches
                ],
            }

        ids = [str(m["id"]) for m in matches]
        # Use ANY array binding; we validated earlier that pg_query permits SELECT.
        pg_args = PostgresQueryInput(
            sql="SELECT id, title, body, url FROM documents WHERE id = ANY($1::text[])",
            params=[ids],
            max_rows=args.top_k,
        )
        pg_result = await self.postgres_tool.run(tenant, pg_args)
        by_id = {str(r["id"]): r for r in pg_result["rows"]}

        results = []
        for m in matches:
            doc = by_id.get(str(m["id"]), {})
            results.append({
                "id": m["id"],
                "score": m["score"],
                "title": doc.get("title"),
                "body": doc.get("body"),
                "url": doc.get("url"),
                "metadata": {k: v for k, v in m["payload"].items() if k != "text"},
            })
        return {"query": args.query, "results": results}
