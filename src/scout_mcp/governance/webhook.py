"""Tell an operator that a call is waiting for a human.

One signed POST per new approval request. The body holds ids and the tool name only: the
arguments of a write can hold customer data, and a reviewer reads them from the server, where
tenant and scope checks apply. A receiver verifies the call by recomputing

    HMAC-SHA256(secret, "<X-Scout-Timestamp>." + raw body)

and comparing it to ``X-Scout-Signature`` (``sha256=<hex>``); it should also reject a timestamp
that is old. Sending never blocks or fails the agent's call: a slow or dead receiver is logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time

import httpx

from scout_mcp.governance.approval import PendingApproval

logger = logging.getLogger(__name__)


class ApprovalNotifier:
    def __init__(self, url: str, secret: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout_s: float = 5.0):
        self.url, self._secret = url, secret.encode()
        self._transport, self.timeout_s = transport, timeout_s
        self._tasks: set[asyncio.Task] = set()

    def notify(self, approval: PendingApproval) -> None:
        """Send in the background and return at once."""
        task = asyncio.create_task(self._send(approval))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for sends in flight: at shutdown, and in tests."""
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _send(self, approval: PendingApproval) -> None:
        body = json.dumps({
            "event": "approval.requested", "approval_id": approval.id, "tenant": approval.tenant,
            "tool": approval.tool, "created_at": approval.created_at,
        }, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(self._secret, f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json", "X-Scout-Timestamp": timestamp,
                   "X-Scout-Signature": f"sha256={signature}"}
        try:
            # The whole exchange is bounded, not just each socket operation.
            async with asyncio.timeout(self.timeout_s):
                async with httpx.AsyncClient(transport=self._transport) as client:
                    response = await client.post(self.url, content=body, headers=headers)
            if response.status_code >= 300:
                logger.warning("approval webhook answered %s for approval %s", response.status_code, approval.id)
        except Exception as exc:
            logger.warning("approval webhook for %s failed: %s", approval.id, exc)
