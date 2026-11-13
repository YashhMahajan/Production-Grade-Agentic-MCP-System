"""Shared fixtures.

``app_client`` runs the real server (auth middleware, tenant middleware, policy.yaml,
tool registry, audit log, metrics) in-process over an ASGI transport. Only the things
that need a network are replaced: Redis (cache, rate limiter, approval store), the
embedding model, and the token validator, which maps test tokens to principals
instead of calling a JWKS endpoint.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from scout_mcp.agents.mcp_client import _decode_body
from scout_mcp.auth import middleware
from scout_mcp.auth.oauth import Principal
from scout_mcp.cache.manager import CacheManager
from scout_mcp.config import ServerSettings
from scout_mcp.governance.approval import ApprovalGate
from scout_mcp.ratelimit.limiter import RateLimiter
from scout_mcp.server import ScoutServer, build_http_app

ROOT = Path(__file__).resolve().parent.parent
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def principal(tenant="acme", scopes=(), roles=(), subject="agent:test") -> Principal:
    return Principal(
        subject=subject, delegator="user:ana", tenant=tenant, scopes=frozenset(scopes),
        token_id="jti-1", issued_at=0, expires_at=2**31, roles=frozenset(roles),
    )


PG, ES, VEC = "tool:postgres:read", "tool:elasticsearch:read", "tool:vector:read"
TOKENS = {
    "support": principal(scopes=(PG, ES, VEC), roles=("role:support_copilot",)),
    "analyst": principal(scopes=(PG,), roles=("role:analyst",)),
    "noscope": principal(scopes=(), roles=("role:support_copilot",)),
    "writer": principal(scopes=("tool:s3:write",), roles=("role:human_agent",)),
    # Reviewers hold approval:review and are a different subject from the writer.
    "reviewer": principal(scopes=("approval:review",), subject="user:reviewer"),
    "reviewer_globex": principal(tenant="globex", scopes=("approval:review",), subject="user:g"),
    # Same subject as "writer": used to show that nobody approves their own request.
    "self_reviewer": principal(scopes=("approval:review",)),
    "no_review_scope": principal(scopes=(), subject="user:nope"),
}


class FakeRedis:
    """Just the commands ApprovalGate uses."""

    def __init__(self):
        self.data: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.data[key] = value

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)

    async def ping(self):
        return True

    async def aclose(self):
        pass


class _Validator:
    def validate(self, bearer: str):
        from scout_mcp.errors.framework import AuthError

        if bearer not in TOKENS:
            raise AuthError("invalid_token")
        return TOKENS[bearer]


@pytest.fixture
async def app_client(monkeypatch, tmp_path):
    async def noop(self, *a, **k):
        return None

    monkeypatch.setattr(CacheManager, "connect", noop)
    monkeypatch.setattr(RateLimiter, "connect", noop)
    monkeypatch.setattr(RateLimiter, "disconnect", noop)
    monkeypatch.setattr(RateLimiter, "acquire", noop)
    monkeypatch.setattr(middleware, "get_validator", lambda settings: _Validator())
    monkeypatch.setattr("scout_mcp.tools.atomic.embeddings.get_embedder", lambda: object())
    monkeypatch.setattr("scout_mcp.server.init_tracing", lambda settings: None)  # no OTLP export

    settings = ServerSettings(
        _env_file=None,
        policy_file=str(ROOT / "config" / "policy.yaml"),
        audit_log_path=str(tmp_path / "audit.jsonl"),
        retry_base_delay_ms=1,
    )
    server = ScoutServer(settings)
    app = build_http_app(server)

    # The MCP session manager owns an anyio task group, which must be entered and
    # exited by the same task. pytest-asyncio runs fixture setup and teardown in
    # different tasks, so the lifespan lives in one background task, as it does
    # under uvicorn.
    started, stop = asyncio.Event(), asyncio.Event()

    async def lifespan():
        async with app.router.lifespan_context(app):
            server._redis = fake_redis = FakeRedis()
            server.approvals = ApprovalGate(fake_redis)
            started.set()
            await stop.wait()

    task = asyncio.create_task(lifespan())
    await started.wait()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.server = server  # type: ignore[attr-defined]
        client.transport_ = transport  # type: ignore[attr-defined]
        yield client
    stop.set()
    await task


async def rpc(client, method, params=None, token="support", tenant=None):
    headers = {**HEADERS, "Authorization": f"Bearer {token}"}
    if tenant:
        headers["X-Tenant-Id"] = tenant
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    resp = await client.post("/mcp", json=body, headers=headers)
    return resp, (_decode_body(resp) if resp.status_code == 200 else None)


async def call(client, name, arguments, token="support"):
    """tools/call, returning (is_error, payload dict)."""
    _, body = await rpc(client, "tools/call", {"name": name, "arguments": arguments}, token)
    result = body["result"]
    return result.get("isError", False), json.loads(result["content"][0]["text"])
