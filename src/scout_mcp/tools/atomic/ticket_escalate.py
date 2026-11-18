"""Atomic tool: ``ticket.escalate``.

Raises a ticket to high priority and records when and why. It writes, so it is ``destructive``:
the server holds every call for a human to approve, and the approved call is the one that runs,
with the same arguments, once. The copilot may propose it and has no way to approve it.

The ticket is looked up by id and by the caller's tenant in one query, so a ticket in another
tenant is simply not found.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import ClassVar

from elasticsearch import ApiError, TransportError
from pydantic import BaseModel, Field, field_validator

from scout_mcp.auth.policy import PolicyEngine
from scout_mcp.errors.framework import UpstreamError, ValidationError
from scout_mcp.tools.atomic.elasticsearch import ElasticsearchSearchTool
from scout_mcp.tools.base import Tool, ToolLevel, ToolMetadata

_RAISE = (
    "ctx._source.priority = 'high'; "
    "ctx._source.escalated_at = params.at; "
    "ctx._source.escalated_reason = params.reason"
)


class TicketEscalateInput(BaseModel):
    ticket_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,64}$")
    reason: str = Field(..., min_length=1, max_length=500, description="Why this ticket needs a person's attention.")

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a reason is required")
        return value.strip()


class TicketEscalateOutput(BaseModel):
    ticket_id: str
    priority: str
    escalated_at: str


class TicketEscalateTool(Tool):
    meta: ClassVar[ToolMetadata] = ToolMetadata(
        name="ticket.escalate",
        description="Raise a ticket to high priority and record why. Requires human approval.",
        level=ToolLevel.ATOMIC,
        scopes_required=("tool:tickets:escalate",),
        destructive=True,
        cacheable=False,
        timeout_ms=5_000,
        tags=("tickets", "write"),
        read_only=False,
    )
    input_schema: ClassVar[type[BaseModel]] = TicketEscalateInput
    output_schema: ClassVar[type[BaseModel] | None] = TicketEscalateOutput

    def __init__(self, policy: PolicyEngine):
        super().__init__(policy)
        self._es = ElasticsearchSearchTool(policy)

    def policy_resource(self, args: TicketEscalateInput) -> str:  # type: ignore[override]
        return f"tickets/{args.ticket_id}"

    async def run(self, tenant: str, args: TicketEscalateInput) -> dict:  # type: ignore[override]
        at = datetime.now(timezone.utc).isoformat()
        try:
            resp = await self._es.client().update_by_query(
                index="tickets",
                query={"bool": {"filter": [{"ids": {"values": [args.ticket_id]}}, {"term": {"_tenant": tenant}}]}},
                script={"lang": "painless", "source": _RAISE, "params": {"at": at, "reason": args.reason}},
                refresh=True,
                conflicts="proceed",
            )
        except (ApiError, TransportError) as exc:
            status = getattr(exc, "status_code", None)
            raise UpstreamError(
                code="elasticsearch_error",
                retryable=status in (408, 429, 500, 502, 503, 504),
                hint=str(exc)[:200],
                context={"status": status},
            ) from exc
        if not resp["updated"]:
            raise ValidationError("invalid_arguments", retryable=False,
                                  hint=f"there is no ticket {args.ticket_id} for this tenant")
        return {"ticket_id": args.ticket_id, "priority": "high", "escalated_at": at}
