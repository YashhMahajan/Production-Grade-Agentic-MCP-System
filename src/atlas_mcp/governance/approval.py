"""Component 12 — Human-in-the-loop approval gate.

Destructive tool calls (writes, deletes, payments, emails) do not execute
immediately. They create a pending approval record in Redis and return a
structured ``pending_approval`` error to the agent. A human operator reviews
the pending record in a dashboard and approves or denies it; the agent can
then call ``approval.resume`` with the approval id.

This is the pattern that lets you ship agents without betting the company.
It mirrors what Claude Code, Cursor, and Copilot do for file edits — the
confirmation step is not optional ceremony, it is the control surface.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from redis.asyncio import Redis

from atlas_mcp.errors.framework import PolicyError, ToolError


APPROVAL_TTL_SECONDS = 3600  # An unreviewed approval expires in one hour.


@dataclass
class PendingApproval:
    id: str
    tenant: str
    caller: str
    delegator: str | None
    tool: str
    arguments: dict[str, Any]
    created_at: str
    state: Literal["pending", "approved", "denied", "expired"] = "pending"


class PendingApprovalError(ToolError):
    """Raised to signal the agent that human approval is required.

    The ``hint`` tells the agent to surface the approval id to the user so
    they can go approve it. ``retryable`` is False because retrying without
    action will not change the outcome.
    """

    def __init__(self, approval: PendingApproval):
        super().__init__(
            code="pending_approval",
            retryable=False,
            hint=(
                f"tool {approval.tool!r} requires human approval; "
                f"approval_id={approval.id}. "
                "Surface the approval id to the user and wait."
            ),
            context={"approval_id": approval.id, "tool": approval.tool},
        )


class ApprovalGate:
    """Creates pending approvals and checks their state on resume."""

    def __init__(self, redis: Redis):
        self._redis = redis

    async def request(self, *, tenant: str, caller: str, delegator: str | None,
                      tool: str, arguments: dict[str, Any]) -> PendingApproval:
        approval = PendingApproval(
            id=uuid.uuid4().hex,
            tenant=tenant,
            caller=caller,
            delegator=delegator,
            tool=tool,
            arguments=arguments,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        key = self._key(approval.id)
        await self._redis.set(key, json.dumps(asdict(approval)), ex=APPROVAL_TTL_SECONDS)
        return approval

    async def check(self, approval_id: str, expected_tenant: str) -> PendingApproval:
        """Look up an approval and enforce that the tenant matches."""
        raw = await self._redis.get(self._key(approval_id))
        if raw is None:
            raise PolicyError(
                "approval_not_found",
                retryable=False,
                hint=f"approval {approval_id!r} does not exist or has expired",
            )
        approval = PendingApproval(**json.loads(raw))
        if approval.tenant != expected_tenant:
            raise PolicyError(
                "approval_tenant_mismatch",
                retryable=False,
                hint="approval belongs to a different tenant",
            )
        return approval

    async def approve(self, approval_id: str, approver: str) -> PendingApproval:
        approval = await self._load(approval_id)
        approval.state = "approved"
        await self._save(approval, add_approver=approver)
        return approval

    async def deny(self, approval_id: str, approver: str) -> PendingApproval:
        approval = await self._load(approval_id)
        approval.state = "denied"
        await self._save(approval, add_approver=approver)
        return approval

    # ── internals ─────────────────────────────────────────────────────────
    async def _load(self, approval_id: str) -> PendingApproval:
        raw = await self._redis.get(self._key(approval_id))
        if raw is None:
            raise PolicyError("approval_not_found", retryable=False)
        return PendingApproval(**json.loads(raw))

    async def _save(self, approval: PendingApproval, add_approver: str | None = None) -> None:
        payload = asdict(approval)
        if add_approver:
            payload["approver"] = add_approver
        await self._redis.set(self._key(approval.id), json.dumps(payload), ex=APPROVAL_TTL_SECONDS)

    @staticmethod
    def _key(approval_id: str) -> str:
        return f"atlas:approval:{approval_id}"
