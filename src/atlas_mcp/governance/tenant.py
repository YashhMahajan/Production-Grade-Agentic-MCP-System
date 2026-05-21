"""Component 12 — Multi-tenancy boundary.

Asana's MCP-powered feature leaked customer data across 1,000 organisations
because tenancy was an afterthought. Atlas bakes it in at the middleware
layer so no code path can escape the boundary.

Three sources of tenant information, in priority order:

1. The ``tenant`` claim on the authenticated Principal. This is the only
   source that cannot be spoofed by the caller.
2. An explicit ``X-Tenant-Id`` header — allowed only when the Principal has
   the ``tenant:*:impersonate`` scope (used by internal admin tools).
3. ``"default"`` when tenancy is disabled by settings.

The chosen tenant is attached to ``request.state.tenant`` and threaded
through the dispatch pipeline. Every data tool MUST use this value to
filter backend queries. The atomic tools in ``tools/atomic/`` already do.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from atlas_mcp.config import ServerSettings


_EXEMPT_PATHS = frozenset({"/.well-known/mcp-server", "/healthz", "/readyz", "/metrics"})


class TenantMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: ServerSettings):
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        principal = getattr(request.state, "principal", None)
        if principal is None and self.settings.require_tenant:
            return JSONResponse({"error": "no_principal"}, status_code=401)

        # Start from the token's tenant claim.
        tenant = principal.tenant if principal is not None else "default"

        # Impersonation: allow an X-Tenant-Id header if the scope permits.
        header_tenant = request.headers.get(self.settings.tenant_header)
        if header_tenant and header_tenant != tenant:
            if principal is None or not principal.has_scope("tenant:*:impersonate"):
                return JSONResponse(
                    {"error": "tenant_mismatch"},
                    status_code=403,
                )
            tenant = header_tenant

        request.state.tenant = tenant
        return await call_next(request)
