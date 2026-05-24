"""Component 1 — Transport & Session Layer.

Atlas-MCP ships two transports from day one:

* stdio, for local development and single-user MCP hosts like Claude Desktop.
* Streamable HTTP, for remote deployments, multi-user access, and horizontal scaling.

The same tool registry, auth, and middleware feed both transports. Only the
wiring at the edge changes.

Session handling follows the 2026 MCP roadmap's stateless guidance: the server
does not hold conversation state in memory between requests, so a load balancer
can route any request to any replica. Session-scoped data lives in Redis.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

import uvicorn
from mcp.server import Server
from mcp.server.stdio import stdio_server
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Mount, Route

from atlas_mcp.auth.middleware import AuthMiddleware
from atlas_mcp.cache.manager import CacheManager
from atlas_mcp.config import ServerSettings, get_settings
from atlas_mcp.errors.framework import ToolError, to_mcp_error
from atlas_mcp.governance.tenant import TenantMiddleware
from atlas_mcp.observability.metrics import MetricsRegistry, metrics_endpoint
from atlas_mcp.observability.tracing import init_tracing
from atlas_mcp.ratelimit.limiter import RateLimiter
from atlas_mcp.reliability.circuit_breaker import CircuitBreakerRegistry
from atlas_mcp.tools.registry import ToolRegistry
from atlas_mcp.validation.schemas import ToolCallEnvelope

logger = logging.getLogger(__name__)


class AtlasServer:
    """The glue that holds the twelve components together.

    This is intentionally thin — each component is its own module. The server
    owns the lifecycle (startup / shutdown) and the request path (validate →
    authorize → rate-limit → cache → execute → audit).
    """

    def __init__(self, settings: ServerSettings):
        self.settings = settings
        self.mcp = Server(settings.service_name)

        # Shared singletons used across requests.
        self.registry = ToolRegistry()
        self.cache = CacheManager(settings)
        self.limiter = RateLimiter(settings)
        self.breakers = CircuitBreakerRegistry(settings)
        self.metrics = MetricsRegistry()

        self._register_mcp_handlers()

    # ── MCP protocol handlers ──────────────────────────────────────────────
    # list_tools (what to call)
    # call_tool (what to execute)

    def _register_mcp_handlers(self) -> None:
        @self.mcp.list_tools()
        async def list_tools():  # type: ignore[misc]
            # Component 4: surface only tools the caller is allowed to see.
            ctx = _current_context()
            return self.registry.list_visible(tenant=ctx.tenant, scopes=ctx.scopes)

        @self.mcp.call_tool()
        async def call_tool(name: str, arguments: dict):  # type: ignore[misc]
            ctx = _current_context()
            envelope = ToolCallEnvelope(
                tool=name, arguments=arguments, tenant=ctx.tenant, caller=ctx.subject
            )
            try:
                return await self._dispatch(envelope)
            except ToolError as exc:
                # Component 10: translate our structured error into MCP's wire format.
                return to_mcp_error(exc)

    async def _dispatch(self, envelope: ToolCallEnvelope):
        """The request pipeline. Read it top-to-bottom; it is the whole story."""
        # 5. Validate input against the tool's declared schema.
        tool = self.registry.get(envelope.tool)
        validated_args = tool.validate(envelope.arguments)

        # 3. Authorize — tool-level scopes + tenant-scoped ABAC.
        tool.policy.check(envelope.caller, envelope.tenant, validated_args)

        # 8. Rate limit — Redis token bucket keyed on tenant + tool.
        await self.limiter.acquire(envelope.tenant, envelope.tool)

        # 9. Cache — deterministic hash of (tool, tenant, args).
        cache_key = tool.cache_key(envelope.tenant, validated_args)
        if cached := await self.cache.get(cache_key):
            self.metrics.cache_hit.labels(tool=envelope.tool).inc()
            return cached

        # 7. Circuit breaker wraps the actual execution.
        breaker = self.breakers.for_tool(envelope.tool)
        with self.metrics.latency.labels(tool=envelope.tool).time():
            result = await breaker.call(tool.execute, envelope.tenant, validated_args)

        # 9 (cont.) write-through on success.
        if tool.cacheable:
            await self.cache.set(cache_key, result, ttl=tool.cache_ttl_seconds)

        # 11. Audit log — outside the hot path but before the response returns.
        self.metrics.calls_total.labels(tool=envelope.tool, status="ok").inc()
        return result

    # ── Lifecycle ──────────────────────────────────────────────────────────
    async def startup(self) -> None:
        init_tracing(self.settings)
        await self.cache.connect()
        await self.limiter.connect()
        await self.registry.discover()
        logger.info("atlas-mcp ready", extra={"tools": len(self.registry)})

    async def shutdown(self) -> None:
        await self.cache.disconnect()
        await self.limiter.disconnect()


# ── HTTP transport (Streamable HTTP per MCP 2025-11) ──────────────────────

# This wraps the MCP server in a Starlette app with middleware:

def build_http_app(server: AtlasServer) -> Starlette:
    """Wraps the MCP server in a Starlette app with all middleware applied."""

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        await server.startup()
        try:
            yield
        finally:
            await server.shutdown()

    middleware = [
        # Order matters: auth populates identity, then tenant middleware reads it.
        Middleware(AuthMiddleware, settings=server.settings),
        Middleware(TenantMiddleware, settings=server.settings),
    ]

    # flag that we set into .env (stateless mode doesnt let hold conversations between requests)
    # so that load balancer can end any requst to any replica

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    session_manager = StreamableHTTPSessionManager(
        app=server.mcp, stateless=server.settings.stateless_mode
    )

    routes = [
        # /.well-known/mcp-server — Component 4: discovery without a live connection.
        Route("/.well-known/mcp-server", endpoint=server.registry.well_known_endpoint),  #what the server offers without session
        Route("/healthz", endpoint=_healthz),   #kubernetes liveness probe
        Route("/readyz", endpoint=_readyz),     #kubernetes readiness probe
        Route("/metrics", endpoint=metrics_endpoint),  #prometheus metrics
        Mount("/mcp", app=session_manager.handle_request),   #mcp traffic goes here
    ]
    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)


# kubternetes liveness 
async def _healthz(_request):
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok"})

# kubernetes readiness 
async def _readyz(_request):
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ready"})


# ── stdio transport (for local MCP hosts) ─────────────────────────────────
# wires MCP with stdin/stdout
async def run_stdio(server: AtlasServer) -> None:
    await server.startup()
    try:
        async with stdio_server() as (read, write):
            await server.mcp.run(read, write, server.mcp.create_initialization_options())
    finally:
        await server.shutdown()


# ── Request-scoped context ─────────────────────────────────────────────────
# In real code this is a contextvars.ContextVar populated by middleware.
# Shown here inline so the request path in _dispatch is easy to read.
class _Ctx:
    subject: str = "anonymous"
    tenant: str = "default"
    scopes: list[str] = []


def _current_context() -> _Ctx:
    # See auth/middleware.py and governance/tenant.py for the real implementation.
    return _Ctx()


# ── Entrypoint ───────────────────────────────────────────────────────────── 
def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    server = AtlasServer(settings)

    if settings.transport == "stdio":    # run for local IDEs (claude desktop)
        asyncio.run(run_stdio(server))
    else:   
        app = build_http_app(server)   # starlette server, run in kubernetes, remote deployments
        uvicorn.run(app, host=settings.http_host, port=settings.http_port)


if __name__ == "__main__":
    main()
