"""Workflow tool — ``customer.build_context``.

A Level-3 tool: an agent-facing workflow that fans out to multiple atomic
tools concurrently, aggregates the results, and returns a single coherent
"customer context" document.

This is the tool a support-copilot agent calls first when a new ticket
comes in. It captures the deterministic portion of "look up everything
relevant" so the LLM does not have to construct that plan from scratch
on every ticket.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from scout_mcp.auth.policy import PolicyEngine
from scout_mcp.errors.framework import ToolError
from scout_mcp.tools.atomic.elasticsearch import ElasticsearchSearchInput, ElasticsearchSearchTool
from scout_mcp.tools.atomic.postgres import PostgresQueryInput, PostgresQueryTool
from scout_mcp.tools.base import Tool, ToolLevel, ToolMetadata
from scout_mcp.tools.composed.semantic_search import SemanticSearchInput, SemanticSearchTool

#  An agent from accidentally injecting SQL or path traversal characters into a customer id. Tight input validation is especially important for workflow tools because they fan out to multiple downstream systems.

class CustomerContextInput(BaseModel):
    customer_id: str = Field(..., pattern=r"^[A-Za-z0-9\-_]{1,64}$")
    question: str = Field(..., min_length=1, max_length=2000,
                          description="The support question being asked.")
    include_orders: bool = True
    include_tickets: bool = True
    include_docs: bool = True
    recent_orders_limit: int = Field(5, ge=1, le=20)


class CustomerContextTool(Tool):
    """Fan-out workflow that builds the canonical support context."""

    meta: ClassVar[ToolMetadata] = ToolMetadata(
        name="customer.build_context",
        description=(
            "Aggregate a customer's recent orders, open tickets, and relevant "
            "documentation passages for the given question. Returns a single "
            "structured context object."
        ),
        level=ToolLevel.WORKFLOW,
        scopes_required=(
            "tool:postgres:read",
            "tool:elasticsearch:read",
            "tool:vector:read",
        ),
        cacheable=True,
        cache_ttl_seconds=30,
        timeout_ms=15_000,
        tags=("support", "context", "workflow"),
        returns_untrusted_data=True,
    )
    input_schema: ClassVar[type[BaseModel]] = CustomerContextInput

    def __init__(self, policy: PolicyEngine):
        super().__init__(policy)
        self.postgres = PostgresQueryTool(policy)
        self.es = ElasticsearchSearchTool(policy)
        self.semantic = SemanticSearchTool(policy)

    # Run methods for fans out concurrently.
    async def run(self, tenant: str, args: CustomerContextInput) -> dict:  # type: ignore[override]
        # Build the list of sub-tasks conditionally.
        tasks: dict[str, asyncio.Task] = {}

        # Identity — always fetched.
        tasks["profile"] = asyncio.create_task(self._profile(tenant, args.customer_id))

        if args.include_orders:
            tasks["orders"] = asyncio.create_task(
                self._recent_orders(tenant, args.customer_id, args.recent_orders_limit)
            )
        if args.include_tickets:
            tasks["tickets"] = asyncio.create_task(
                self._open_tickets(tenant, args.customer_id)
            )
        if args.include_docs:
            tasks["docs"] = asyncio.create_task(
                self._relevant_docs(tenant, args.question)
            )

        # Gather with return_exceptions — a partial context is better than none.
        # Partial context is better than no context, because the agent can work with what it has and the partial_errors field tells it what is missing.
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)  # means a failure in one sub-query does not kill the others.
        keyed = dict(zip(tasks.keys(), results))

        # Separate successes from errors so the agent can see what failed.
        context: dict[str, Any] = {"customer_id": args.customer_id, "question": args.question}
        errors: dict[str, str] = {}
        for key, value in keyed.items():
            if isinstance(value, ToolError):
                errors[key] = value.code
            elif isinstance(value, Exception):
                errors[key] = f"internal_error: {type(value).__name__}"
            else:
                context[key] = value
        if errors:
            context["partial_errors"] = errors
        return context

    # Successful data lives under its key (profile, orders, tickets, docs)
    async def _profile(self, tenant: str, customer_id: str) -> dict:
        result = await self.postgres.run(
            tenant,
            PostgresQueryInput(
                sql="SELECT id, name, email, tier, created_at FROM customers WHERE id = $1",
                params=[customer_id],
                max_rows=1,
            ),
        )
        rows = result["rows"]
        return rows[0] if rows else {}

    async def _recent_orders(self, tenant: str, customer_id: str, limit: int) -> list[dict]:
        result = await self.postgres.run(
            tenant,
            PostgresQueryInput(
                sql=(
                    "SELECT id, status, total_cents, currency, created_at "
                    "FROM orders WHERE customer_id = $1 ORDER BY created_at DESC"
                ),
                params=[customer_id],
                max_rows=limit,
            ),
        )
        return result["rows"]

    async def _open_tickets(self, tenant: str, customer_id: str) -> list[dict]:
        result = await self.es.run(
            tenant,
            ElasticsearchSearchInput(
                index="tickets",
                query={
                    "bool": {
                        "must": [
                            {"term": {"customer_id": customer_id}},
                            {"terms": {"status": ["open", "pending"]}},
                        ]
                    }
                },
                size=10,
                fields=["subject", "status", "priority", "created_at"],
            ),
        )
        return [h["source"] | {"id": h["id"]} for h in result["hits"]]

    async def _relevant_docs(self, tenant: str, question: str) -> list[dict]:
        result = await self.semantic.run(
            tenant,
            SemanticSearchInput(
                query=question, collection="support_docs", top_k=3, hydrate_from_postgres=False
            ),
        )
        return result["results"]
