"""Origin validation for the HTTP transport.

The MCP spec requires a server to check the ``Origin`` header and answer 403 when it
is present and not allowed. It stops a web page in a user's browser from reaching an
MCP server on a private address through DNS rebinding. Clients that are not browsers
send no ``Origin`` and are not affected. A browser-based client has to be listed in
``SCOUT_ALLOWED_ORIGINS``.
"""

from __future__ import annotations

from starlette.responses import JSONResponse

from scout_mcp.config import ServerSettings


class OriginMiddleware:
    def __init__(self, app, settings: ServerSettings):
        self.app = app
        self.allowed = frozenset(o.rstrip("/").lower() for o in settings.allowed_origins)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            origin = dict(scope["headers"]).get(b"origin")
            if origin is not None and origin.decode("latin-1").rstrip("/").lower() not in self.allowed:
                response = JSONResponse({"error": "origin_not_allowed"}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
