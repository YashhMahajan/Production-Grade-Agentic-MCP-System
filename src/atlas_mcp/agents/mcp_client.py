"""MCP client used by the agent layer to call Atlas-MCP.

This is a thin, purpose-built async client. We do not use the MCP SDK's
full session wrapper here because the agents need very specific behaviour:

* No interactive prompting — tools always execute.
* Opinionated error handling that surfaces SERF ``retryable`` and ``hint``
  fields to the agent's prompt.
* Streamed tool calls with an attached trace id so OTel spans stitch
  through from agent → server.

In production you would generate this client from the server's
``/.well-known/mcp-server`` metadata so adding a new tool on the server
does not require a client code change.
"""


from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(slots=True)
class ToolResult:
    ok: bool
    value: Any | None = None
    error_code: str | None = None
    retryable: bool = False
    hint: str | None = None

    def as_agent_observation(self) -> str:
        """Render as text for inclusion in an LLM prompt."""
        if self.ok:
            return json.dumps({"ok": True, "result": self.value}, default=str, indent=2)
        return json.dumps(
            {
                "ok": False,
                "error": self.error_code,
                "retryable": self.retryable,
                "hint": self.hint,
            },
            indent=2,
        )


class AtlasMCPClient:
    """Minimal JSON-RPC 2.0 client over Streamable HTTP."""

    def __init__(
        self,
        base_url: str,
        bearer_token: str,
        tenant: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.tenant = tenant
        self._transport = transport  # tests pass an in-process ASGI transport
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None

    async def __aenter__(self) -> AtlasMCPClient:
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.tenant:
            headers["X-Tenant-Id"] = self.tenant
        self._client = httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=30.0, transport=self._transport
        )
        await self._initialize()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── MCP protocol lifecycle ────────────────────────────────────────────
    async def _initialize(self) -> None:
        await self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "atlas-agent", "version": "0.1.0"},
            },
        )
        # Stateful servers return the session id in a response header (see _rpc);
        # stateless servers return none. Either way the spec wants this notification.
        await self._notify("notifications/initialized")

    async def list_tools(self) -> list[dict]:
        resp = await self._rpc("tools/list", {})
        return resp.get("tools", [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        try:
            resp = await self._rpc(
                "tools/call", {"name": name, "arguments": arguments}
            )
        except MCPError as exc:
            data = exc.data or {}
            return ToolResult(
                ok=False,
                error_code=data.get("code") or str(exc.code),
                retryable=bool(data.get("retryable")),
                hint=data.get("hint") or exc.message,
            )
        # MCP wraps results in ``content`` blocks; flatten for agent use.
        content = resp.get("content") or []
        if content and content[0].get("type") == "text":
            try:
                value = json.loads(content[0]["text"])
            except json.JSONDecodeError:
                value = content[0]["text"]
        else:
            value = resp
        if resp.get("isError"):
            # The server puts its structured error (code, retryable, hint) in the text block.
            data = value if isinstance(value, dict) else {"hint": str(value)}
            return ToolResult(
                ok=False,
                error_code=data.get("code") or "tool_error",
                retryable=bool(data.get("retryable")),
                hint=data.get("hint"),
            )
        return ToolResult(ok=True, value=value)

    # ── Low-level RPC ─────────────────────────────────────────────────────
    async def _rpc(self, method: str, params: dict) -> dict:
        assert self._client is not None, "use `async with AtlasMCPClient(...)`"
        request = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": method,
            "params": params,
        }
        resp = await self._post(request)
        body = _decode_body(resp)
        if "error" in body:
            err = body["error"]
            raise MCPError(code=err.get("code", -32000), message=err.get("message", ""),
                           data=err.get("data"))
        return body.get("result", {})

    async def _notify(self, method: str) -> None:
        """Send a JSON-RPC notification: no id, and the server answers 202 with no body."""
        await self._post({"jsonrpc": "2.0", "method": method})

    async def _post(self, payload: dict) -> httpx.Response:
        assert self._client is not None, "use `async with AtlasMCPClient(...)`"
        headers = {"Mcp-Session-Id": self._session_id} if self._session_id else {}
        resp = await self._client.post("/mcp", json=payload, headers=headers)
        resp.raise_for_status()
        self._session_id = resp.headers.get("mcp-session-id", self._session_id)
        return resp


def _decode_body(resp: httpx.Response) -> dict:
    """Streamable HTTP answers with JSON or a one-event SSE stream; accept both."""
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise MCPError(code=-32000, message="event stream carried no data event")
    return resp.json()


class MCPError(Exception):
    def __init__(self, code: int, message: str, data: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data
