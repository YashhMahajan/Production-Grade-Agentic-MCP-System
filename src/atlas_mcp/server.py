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
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass

import uvicorn
from mcp.server import Server
from mcp.server.stdio import stdio_server
from redis.asyncio import Redis
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from atlas_mcp.auth.middleware import AuthMiddleware
from atlas_mcp.auth.policy import PolicyEngine
from atlas_mcp.cache.manager import CacheManager
from atlas_mcp.config import ServerSettings, get_settings
from atlas_mcp.errors.framework import (
    AuthError,
    PolicyError,
    RateLimitError,
    ToolError,
    UpstreamError,
    to_call_tool_result,
)
from atlas_mcp.governance.approval import ApprovalGate, PendingApprovalError
from atlas_mcp.governance.tenant import TenantMiddleware
from atlas_mcp.observability.audit import AuditLogger
from atlas_mcp.observability.metrics import MetricsRegistry, metrics_endpoint_for
from atlas_mcp.observability.tracing import current_trace_id, get_tracer, init_tracing
from atlas_mcp.ratelimit.limiter import RateLimiter
from atlas_mcp.reliability.atba import ATBA
from atlas_mcp.reliability.circuit_breaker import CircuitBreakerRegistry, State
from atlas_mcp.reliability.retry import with_retry
from atlas_mcp.tools.registry import ToolRegistry
from atlas_mcp.validation.schemas import ToolCallEnvelope

logger = logging.getLogger(__name__)

# stdio has no token, so it runs as a local developer persona. The role is what
# policy.yaml rules match on; scopes are unrestricted because the process owner
# is the only caller.
_STDIO_ROLES = frozenset({"role:support_copilot"})
_STDIO_SCOPES = frozenset({"tool:*:admin"})

_CIRCUIT_GAUGE = {State.CLOSED: 0, State.HALF_OPEN: 1, State.OPEN: 2}

# Scope that lets a human review pending approvals.
APPROVAL_REVIEW_SCOPE = "approval:review"


@dataclass(frozen=True)
class CallContext:
    """Who is calling, resolved from the verified token for this request."""

    subject: str
    tenant: str
    scopes: frozenset[str]
    roles: frozenset[str]
    delegator: str | None = None


class AtlasServer:
    """The glue that holds the twelve components together.

    This is intentionally thin — each component is its own module. The server
    owns the lifecycle (startup / shutdown) and the request path (validate →
    authorize → rate-limit → approve → cache → execute → audit).
    """

    def __init__(self, settings: ServerSettings):
        self.settings = settings
        self.mcp = Server(settings.service_name)

        # Shared singletons used across requests.
        self.registry = ToolRegistry(issuer=settings.auth_issuer)
        self.cache = CacheManager(settings)
        self.limiter = RateLimiter(settings)
        self.breakers = CircuitBreakerRegistry(settings)
        self.atba = ATBA(settings.atba_total_budget_ms)
        self.metrics = MetricsRegistry()
        # Built in startup(): they open files and connections.
        self.audit: AuditLogger | None = None
        self.approvals: ApprovalGate | None = None
        self._redis: Redis | None = None
        self.ready = False

        self._register_mcp_handlers()

    # ── Request context ────────────────────────────────────────────────────
    def _current_context(self) -> CallContext:
        """Read the identity the auth and tenant middleware attached to this request."""
        try:
            request = self.mcp.request_context.request
        except LookupError:
            request = None
        if request is None:
            # stdio transport: no HTTP request, no token.
            return CallContext("stdio:local", self.settings.tenant, _STDIO_SCOPES, _STDIO_ROLES)
        principal = getattr(request.state, "principal", None)
        if principal is None:
            raise AuthError("no_principal", retryable=False, hint="request carried no identity")
        return CallContext(
            subject=principal.subject,
            tenant=getattr(request.state, "tenant", principal.tenant),
            scopes=principal.scopes,
            roles=principal.roles,
            delegator=principal.delegator,
        )

    # ── MCP protocol handlers ──────────────────────────────────────────────
    def _register_mcp_handlers(self) -> None:
        @self.mcp.list_tools()
        async def list_tools():  # type: ignore[misc]
            ctx = self._current_context()
            return self.registry.list_visible(tenant=ctx.tenant, scopes=ctx.scopes)

        @self.mcp.call_tool()
        async def call_tool(name: str, arguments: dict):  # type: ignore[misc]
            try:
                ctx = self._current_context()
                envelope = ToolCallEnvelope(
                    tool=name,
                    arguments=dict(arguments),
                    tenant=ctx.tenant,
                    caller=ctx.subject,
                    delegator=ctx.delegator,
                    scopes=ctx.scopes,
                    roles=ctx.roles,
                )
                return await self._dispatch(envelope)
            except ToolError as exc:
                # Component 10: keep code / retryable / hint intact on the wire.
                return to_call_tool_result(exc)
            except Exception:
                logger.exception("unhandled error in tool call", extra={"tool": name})
                return to_call_tool_result(
                    ToolError("internal_error", retryable=False, hint="unexpected server error")
                )

    async def _dispatch(self, envelope: ToolCallEnvelope) -> dict:
        """One span and one audit record per call, whatever the outcome."""
        started = time.monotonic()
        status, error_code = "ok", None
        # Label by registered name only: callers choose the tool string, and an
        # unbounded set of span names or metric labels would grow without limit.
        label = envelope.tool if envelope.tool in self.registry else "unknown"
        with get_tracer().start_as_current_span(
            f"tool {label}", attributes={"atlas.tool": label, "atlas.tenant": envelope.tenant}
        ) as span:
            trace_id = current_trace_id()
            try:
                return await self._run_pipeline(envelope)
            except ToolError as exc:
                status, error_code = "error", exc.code
                span.set_attribute("atlas.error_code", exc.code)
                self.metrics.calls_total.labels(tool=label, status=exc.code).inc()
                raise
            finally:
                if self.audit is not None:
                    self.audit.record(
                        trace_id=trace_id,
                        tenant=envelope.tenant,
                        caller=envelope.caller,
                        delegator=envelope.delegator,
                        tool=envelope.tool,
                        arguments=envelope.arguments,
                        duration_ms=(time.monotonic() - started) * 1000,
                        status=status,
                        error_code=error_code,
                    )

    async def _run_pipeline(self, envelope: ToolCallEnvelope) -> dict:
        tool = self.registry.get(envelope.tool)
        self.atba.begin()  # a fresh time budget for this call
        # 5. Validate. The approval id is not a tool argument, so lift it out first.
        raw_args = dict(envelope.arguments)
        approval_id = raw_args.pop("approval_id", None)
        validated_args = tool.validate(raw_args)

        # 3. Authorize: token scopes, then policy.yaml rules for this caller's roles.
        tool.authorize(
            subjects=(envelope.caller, *envelope.roles),
            scopes=envelope.scopes,
            tenant=envelope.tenant,
            args=validated_args,
        )

        # 8. Rate limit, keyed on tenant + tool.
        try:
            await self.limiter.acquire(envelope.tenant, envelope.tool)
        except RateLimitError:
            self.metrics.rate_limited.labels(tool=envelope.tool).inc()
            raise

        # 12. Destructive tools wait for a human.
        if tool.meta.destructive and self.settings.destructive_tool_requires_approval:
            await self._require_approval(envelope, tool.meta.name, validated_args, approval_id)

        # 9. Cache: hash of (tool, tenant, args), so tenants never share entries.
        cache_key = tool.cache_key(envelope.tenant, validated_args)
        if tool.cacheable:
            if (cached := await self.cache.get(cache_key)) is not None:
                self.metrics.cache_hit.labels(tool=envelope.tool).inc()
                self.metrics.calls_total.labels(tool=envelope.tool, status="ok").inc()
                return cached
            self.metrics.cache_miss.labels(tool=envelope.tool).inc()

        # 7. Circuit breaker around the budgeted, retried execution. Retries and the
        # timeout sit inside the breaker so one logical failure counts once.
        breaker = self.breakers.for_tool(envelope.tool)
        try:
            with self.metrics.latency.labels(tool=envelope.tool).time():
                result = await breaker.call(self._execute, tool, envelope.tenant, validated_args)
        finally:
            self.metrics.circuit_state.labels(tool=envelope.tool).set(
                _CIRCUIT_GAUGE[breaker.state]
            )

        # Backends return datetimes, UUIDs and Decimals, which the MCP SDK cannot encode.
        # Normalise once here so the response and the cache hold the same plain JSON.
        result = json.loads(json.dumps(result, default=str))
        if tool.cacheable:
            await self.cache.set(cache_key, result, ttl=tool.cache_ttl_seconds)
        if approval_id and self.approvals is not None:
            await self.approvals.consume(approval_id)

        self.metrics.calls_total.labels(tool=envelope.tool, status="ok").inc()
        return result

    async def _execute(self, tool, tenant: str, args) -> dict:
        """Run the tool with retries, inside a timeout from the ATBA budget."""
        try:
            return await self.atba.call_with_budget(
                tool.meta.name,
                with_retry(
                    tool.execute, tenant, args,
                    max_attempts=self.settings.retry_max_attempts,
                    base_delay_ms=self.settings.retry_base_delay_ms,
                ),
                cap_s=tool.meta.timeout_ms / 1000,
            )
        except TimeoutError as exc:
            # An UpstreamError, so a backend that keeps timing out opens the breaker.
            raise UpstreamError(
                "timeout", retryable=True,
                hint=f"{tool.meta.name} did not answer within its time budget",
                context={"tool": tool.meta.name},
            ) from exc

    async def _require_approval(
        self, envelope: ToolCallEnvelope, tool_name: str, args, approval_id: str | None
    ) -> None:
        if self.approvals is None:
            raise PolicyError("approvals_unavailable", retryable=False,
                              hint="approval store is not connected")
        payload = args.model_dump(mode="json")
        if approval_id is None:
            approval = await self.approvals.request(
                tenant=envelope.tenant, caller=envelope.caller, delegator=envelope.delegator,
                tool=tool_name, arguments=payload,
            )
            raise PendingApprovalError(approval)
        approval = await self.approvals.check(approval_id, envelope.tenant)
        if approval.state != "approved":
            raise PolicyError("approval_not_granted", retryable=False,
                              hint=f"approval is {approval.state}")
        # The approved call is the one that runs: same tool, same arguments.
        if approval.tool != tool_name or approval.arguments != payload:
            raise PolicyError("approval_mismatch", retryable=False,
                              hint="arguments differ from what was approved")

    # ── Approval review (HTTP) ─────────────────────────────────────────────
    def _reviewer(self, request: Request):
        principal = request.state.principal
        if APPROVAL_REVIEW_SCOPE not in principal.scopes:
            raise PolicyError("missing_scope", retryable=False,
                              hint=f"token lacks scope {APPROVAL_REVIEW_SCOPE}")
        return principal, request.state.tenant

    async def approval_detail(self, request: Request) -> JSONResponse:
        """Show a reviewer exactly what they would be approving."""
        try:
            _principal, tenant = self._reviewer(request)
            approval = await self.approvals.check(request.path_params["approval_id"], tenant)
        except ToolError as exc:
            return _error_response(exc)
        return JSONResponse(asdict(approval))

    async def approval_decide(self, request: Request) -> JSONResponse:
        action = request.path_params["action"]
        if action not in ("approve", "deny"):
            return JSONResponse({"error": "unknown_action"}, status_code=404)
        try:
            principal, tenant = self._reviewer(request)
            approval = await self.approvals.check(request.path_params["approval_id"], tenant)
            if approval.caller == principal.subject:
                raise PolicyError("self_approval", retryable=False,
                                  hint="the caller that asked cannot approve its own request")
            if approval.state != "pending":
                raise PolicyError("approval_already_decided", retryable=False,
                                  hint=f"approval is already {approval.state}")
            decide = self.approvals.approve if action == "approve" else self.approvals.deny
            approval = await decide(approval.id, principal.subject)
        except ToolError as exc:
            return _error_response(exc)
        if self.audit is not None:
            self.audit.record(
                trace_id=None, tenant=tenant, caller=principal.subject,
                delegator=principal.delegator, tool=f"approval.{action}",
                arguments={"approval_id": approval.id, "tool": approval.tool},
                duration_ms=0.0, status="ok",
            )
        return JSONResponse(asdict(approval))

    # ── Lifecycle ──────────────────────────────────────────────────────────
    async def startup(self) -> None:
        init_tracing(self.settings)
        self.audit = AuditLogger(self.settings.audit_log_path)
        await self.cache.connect()
        await self.limiter.connect()
        self._redis = Redis.from_url(self.settings.redis_url, decode_responses=True)
        self.approvals = ApprovalGate(self._redis)
        # Fail closed: a missing policy file stops the server rather than allowing everything.
        policy = PolicyEngine.from_file(
            self.settings.policy_file, default_deny=self.settings.policy_default_deny
        )
        await self.registry.discover(policy)
        await self._preload_embedder()
        self.ready = True
        logger.info("atlas-mcp ready", extra={"tools": len(self.registry)})

    async def _preload_embedder(self) -> None:
        """Load the embedding model now so the first search is not the one that pays."""
        from atlas_mcp.tools.atomic.embeddings import get_embedder

        try:
            await asyncio.to_thread(get_embedder)
        except Exception:
            logger.exception("embedding model failed to load; search tools will fail")

    async def shutdown(self) -> None:
        self.ready = False
        await self.cache.disconnect()
        await self.limiter.disconnect()
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def check_ready(self) -> bool:
        if not self.ready or self._redis is None:
            return False
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False


_STATUS_BY_CODE = {"approval_not_found": 404, "missing_scope": 403, "self_approval": 403,
                   "approval_tenant_mismatch": 403, "approval_already_decided": 409}


def _error_response(exc: ToolError) -> JSONResponse:
    return JSONResponse(exc.to_dict(), status_code=_STATUS_BY_CODE.get(exc.code, 400))


# ── HTTP transport (Streamable HTTP per MCP 2025-11) ──────────────────────
class _MCPEndpoint:
    """ASGI callable for /mcp.

    Starlette wraps plain functions and bound methods as request handlers, which
    is not what the session manager is. A class instance with ``__call__`` is
    mounted as-is. Using a Route rather than Mount also avoids the 307 redirect
    from /mcp to /mcp/ that clients did not follow.
    """

    def __init__(self, manager) -> None:
        self._manager = manager

    async def __call__(self, scope, receive, send) -> None:
        await self._manager.handle_request(scope, receive, send)


def build_http_app(server: AtlasServer) -> Starlette:
    """Wraps the MCP server in a Starlette app with all middleware applied."""
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    # Stateless mode keeps no conversation in memory, so a load balancer can send
    # any request to any replica.
    session_manager = StreamableHTTPSessionManager(
        app=server.mcp, stateless=server.settings.stateless_mode
    )

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        await server.startup()
        try:
            # run() starts the task group the session manager needs. Without it every
            # /mcp request failed with "Task group is not initialized".
            async with session_manager.run():
                yield
        finally:
            await server.shutdown()

    middleware = [
        # Order matters: auth populates identity, then tenant middleware reads it.
        Middleware(AuthMiddleware, settings=server.settings),
        Middleware(TenantMiddleware, settings=server.settings),
    ]

    async def healthz(_request):
        return JSONResponse({"status": "ok"})

    async def readyz(_request):
        if await server.check_ready():
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "not_ready"}, status_code=503)

    routes = [
        # Discovery without a live session (public).
        Route("/.well-known/mcp-server", endpoint=server.registry.well_known_endpoint),
        Route("/healthz", endpoint=healthz),  # liveness probe
        Route("/readyz", endpoint=readyz),  # readiness probe: Redis reachable, tools loaded
        Route("/metrics", endpoint=metrics_endpoint_for(server.metrics)),
        Route("/mcp", endpoint=_MCPEndpoint(session_manager)),
        # Human review of pending destructive calls. Needs the approval:review scope.
        Route("/approvals/{approval_id}", endpoint=server.approval_detail, methods=["GET"]),
        Route("/approvals/{approval_id}/{action}", endpoint=server.approval_decide, methods=["POST"]),
    ]
    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)


# ── stdio transport (for local MCP hosts) ─────────────────────────────────
async def run_stdio(server: AtlasServer) -> None:
    await server.startup()
    try:
        async with stdio_server() as (read, write):
            await server.mcp.run(read, write, server.mcp.create_initialization_options())
    finally:
        await server.shutdown()


# ── Entrypoint ─────────────────────────────────────────────────────────────
def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    server = AtlasServer(settings)

    if settings.transport == "stdio":  # local IDEs (Claude Desktop)
        asyncio.run(run_stdio(server))
    else:  # Starlette under uvicorn: Kubernetes, remote deployments
        app = build_http_app(server)
        uvicorn.run(app, host=settings.http_host, port=settings.http_port)


if __name__ == "__main__":
    main()
