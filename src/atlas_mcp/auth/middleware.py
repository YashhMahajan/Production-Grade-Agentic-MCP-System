"""Starlette middleware that validates the bearer token on every request.

Reads the ``Authorization: Bearer <jwt>`` header, calls the token validator,
and stores the resulting :class:`Principal` on ``request.state`` for downstream
handlers, tenant middleware, and the dispatch pipeline.

Rejected requests return RFC 6750-compliant WWW-Authenticate headers so that
MCP hosts can trigger their OAuth flow.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from atlas_mcp.auth.oauth import get_validator
from atlas_mcp.config import ServerSettings
from atlas_mcp.errors.framework import AuthError


# Endpoints that don't require auth — discovery and liveness are public.
_PUBLIC_PATHS = frozenset({"/.well-known/mcp-server", "/healthz", "/readyz", "/metrics"})


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: ServerSettings):
        super().__init__(app)
        self.settings = settings
        self.validator = get_validator(settings)

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return self._unauthenticated("missing_token")

        bearer = header[7:].strip()
        try:
            principal = self.validator.validate(bearer)
        except AuthError as exc:
            return self._unauthenticated(exc.code)

        # Attach to request state so downstream handlers can read identity.
        request.state.principal = principal
        return await call_next(request)

    def _unauthenticated(self, error_code: str) -> JSONResponse:
        return JSONResponse(
            {"error": error_code},
            status_code=401,
            headers={
                "WWW-Authenticate": (
                    f'Bearer realm="{self.settings.auth_audience}", '
                    f'error="{error_code}", '
                    f'authorization_uri="{self.settings.auth_issuer}"'
                )
            },
        )
