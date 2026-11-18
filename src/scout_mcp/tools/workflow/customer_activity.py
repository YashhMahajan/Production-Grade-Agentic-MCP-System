"""Workflow tool: ``customer.activity_since``.

What is new for a customer since a moment: the orders and tickets created after it. A
support agent coming back to a case asks this instead of reading the whole history again.
It reads the same two stores as ``customer.build_context`` and fails the same way: a store
that is down is named in ``partial_errors`` and the other half is still returned.

Only creation times are recorded, so a change to an existing order or ticket is not seen.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import ClassVar

from pydantic import BaseModel, Field, field_validator

from scout_mcp.auth.policy import PolicyEngine
from scout_mcp.errors.framework import ToolError
from scout_mcp.tools.atomic.elasticsearch import ElasticsearchSearchInput, ElasticsearchSearchTool
from scout_mcp.tools.atomic.postgres import PostgresQueryInput, PostgresQueryTool
from scout_mcp.tools.base import Tool, ToolLevel, ToolMetadata
from scout_mcp.tools.workflow.customer_context import OrderSummary, TicketSummary


class ActivitySinceInput(BaseModel):
    customer_id: str = Field(..., pattern=r"^[A-Za-z0-9\-_]{1,64}$")
    since: datetime = Field(..., description="ISO 8601 time. Only things created after it are returned.")
    limit: int = Field(20, ge=1, le=50, description="At most this many orders and this many tickets.")

    @field_validator("since")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        """A time with no zone is read as UTC, so the database never compares it to a zoned column."""
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class ActivitySinceOutput(BaseModel):
    customer_id: str
    since: str
    orders: list[OrderSummary] | None = None
    tickets: list[TicketSummary] | None = None
    # Store name -> error code, for each store that failed. The other half is still good.
    partial_errors: dict[str, str] | None = None


class CustomerActivityTool(Tool):
    meta: ClassVar[ToolMetadata] = ToolMetadata(
        name="customer.activity_since",
        description="Orders and tickets created for a customer after a given time, newest first.",
        level=ToolLevel.WORKFLOW,
        scopes_required=("tool:postgres:read", "tool:elasticsearch:read"),
        cacheable=True,
        cache_ttl_seconds=30,
        timeout_ms=10_000,
        tags=("support", "workflow"),
        returns_untrusted_data=True,
    )
    input_schema: ClassVar[type[BaseModel]] = ActivitySinceInput
    output_schema: ClassVar[type[BaseModel] | None] = ActivitySinceOutput

    def __init__(self, policy: PolicyEngine):
        super().__init__(policy)
        self.postgres = PostgresQueryTool(policy)
        self.es = ElasticsearchSearchTool(policy)

    async def run(self, tenant: str, args: ActivitySinceInput) -> dict:  # type: ignore[override]
        found = await asyncio.gather(
            self._orders(tenant, args), self._tickets(tenant, args), return_exceptions=True)
        out: dict = {"customer_id": args.customer_id, "since": args.since.isoformat()}
        errors: dict[str, str] = {}
        for name, value in zip(("orders", "tickets"), found):
            if isinstance(value, ToolError):
                errors[name] = value.code
            elif isinstance(value, Exception):
                errors[name] = f"internal_error: {type(value).__name__}"
            else:
                out[name] = value
        if errors:
            out["partial_errors"] = errors
        return out

    async def _orders(self, tenant: str, args: ActivitySinceInput) -> list[dict]:
        result = await self.postgres.run(tenant, PostgresQueryInput(
            sql=("SELECT id, status, total_cents, currency, created_at FROM orders "
                 "WHERE customer_id = $1 AND created_at > $2 ORDER BY created_at DESC"),
            params=[args.customer_id, args.since], max_rows=args.limit))
        return result["rows"]

    async def _tickets(self, tenant: str, args: ActivitySinceInput) -> list[dict]:
        result = await self.es.run(tenant, ElasticsearchSearchInput(
            index="tickets",
            query={"bool": {"must": [{"term": {"customer_id": args.customer_id}},
                                     {"range": {"created_at": {"gt": args.since.isoformat()}}}]}},
            size=args.limit, fields=["subject", "status", "priority", "created_at"]))
        return [h["source"] | {"id": h["id"]} for h in result["hits"]]
