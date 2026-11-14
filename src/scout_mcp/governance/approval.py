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
from datetime import UTC, datetime
from typing import Any, Literal

from redis.asyncio import Redis

from scout_mcp.errors.framework import PolicyError, ToolError

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
    state: Literal["pending", "approved", "denied", "consumed"] = "pending"
    approver: str | None = None


class PendingApprovalError(ToolError):
    """Raised to signal the agent that human approval is required.

    The ``hint`` tells the agent to surface the approval id to the user so
    they can go approve it. ``retryable`` is False because retrying without
    action will not change the outcome.
    """

    def __init__(self, approval: PendingApproval):
        super().__init__(
            code="pending_approval",
            # False because the human still hasnt approved yet and the outcome doesnt change. 
            retryable=False,
            hint=(
                f"tool {approval.tool!r} requires human approval; "
                f"approval_id={approval.id}. "
                "Surface the approval id to the user and wait."
            ),
            context={"approval_id": approval.id, "tool": approval.tool},
        )


class ApprovalGate:
    """Creates pending approvals and records who decided and who used them.

    The request record is written once and never changed. A decision and a claim
    are each a separate key written with ``SET NX``, so Redis picks exactly one
    winner when two reviewers decide at once, or when two calls try to spend the
    same approval. Neither key outlives the request record: they take its
    remaining TTL, so a decision never extends the hour the request had.
    """

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
            created_at=datetime.now(UTC).isoformat(),
        )
        await self._redis.set(
            self._key(approval.id), json.dumps(asdict(approval)), ex=APPROVAL_TTL_SECONDS
        )
        return approval

    async def check(self, approval_id: str, expected_tenant: str) -> PendingApproval:
        """Look up an approval, enforce the tenant, and report its current state."""
        approval = await self._load(approval_id)
        if approval.tenant != expected_tenant:
            raise PolicyError(
                "approval_tenant_mismatch",
                retryable=False,
                hint="approval belongs to a different tenant",
            )
        return await self._with_state(approval)

    async def approve(self, approval_id: str, approver: str) -> PendingApproval:
        return await self._decide(approval_id, approver, "approved")

    async def deny(self, approval_id: str, approver: str) -> PendingApproval:
        return await self._decide(approval_id, approver, "denied")

    async def claim(self, approval_id: str) -> None:
        """Spend the approval. Of any number of concurrent callers, one succeeds."""
        ttl = await self._remaining_ttl(approval_id)
        if not await self._redis.set(self._claim_key(approval_id), "1", nx=True, ex=ttl):
            raise PolicyError(
                "approval_already_used",
                retryable=False,
                hint="this approval has already been used; ask for a new one",
            )

    async def release(self, approval_id: str) -> None:
        """Hand the approval back after a call that never reached the backend."""
        await self._redis.delete(self._claim_key(approval_id))

    async def _decide(self, approval_id: str, approver: str, state: str) -> PendingApproval:
        approval = await self._load(approval_id)
        ttl = await self._remaining_ttl(approval_id)
        won = await self._redis.set(
            self._decision_key(approval_id),
            json.dumps({"state": state, "approver": approver}),
            nx=True, ex=ttl,
        )
        if not won:
            raise PolicyError(
                "approval_already_decided",
                retryable=False,
                hint="another reviewer decided this approval first",
            )
        return await self._with_state(approval)

    async def _with_state(self, approval: PendingApproval) -> PendingApproval:
        raw = await self._redis.get(self._decision_key(approval.id))
        if raw is not None:
            decision = json.loads(raw)
            approval.state = decision["state"]
            approval.approver = decision["approver"]
        if await self._redis.get(self._claim_key(approval.id)) is not None:
            approval.state = "consumed"
        return approval

    async def _load(self, approval_id: str) -> PendingApproval:
        raw = await self._redis.get(self._key(approval_id))
        if raw is None:
            raise PolicyError(
                "approval_not_found",
                retryable=False,
                hint=f"approval {approval_id!r} does not exist or has expired",
            )
        return PendingApproval(**json.loads(raw))

    async def _remaining_ttl(self, approval_id: str) -> int:
        ttl = await self._redis.ttl(self._key(approval_id))
        return ttl if ttl and ttl > 0 else 1

    @staticmethod
    def _key(approval_id: str) -> str:
        return f"scout:approval:{approval_id}"

    @classmethod
    def _decision_key(cls, approval_id: str) -> str:
        return f"{cls._key(approval_id)}:decision"

    @classmethod
    def _claim_key(cls, approval_id: str) -> str:
        return f"{cls._key(approval_id)}:claim"
