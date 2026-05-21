"""Atomic HTTP fetch tool.

Controlled outbound HTTP. The tool refuses to hit any domain not on the
per-tenant allowlist — this is how you stop an agent from exfiltrating
data to attacker-controlled hosts after a successful prompt injection.

The allowlist is loaded from ``config/http_allowlist.yaml`` at startup. A
missing config file means an empty allowlist, which means no outbound
HTTP. That is deliberately the safe default.
"""

from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import urlparse

import httpx
import yaml
from pydantic import BaseModel, Field, field_validator

from atlas_mcp.auth.policy import PolicyEngine
from atlas_mcp.config import get_settings
from atlas_mcp.errors.framework import PolicyError, UpstreamError
from atlas_mcp.tools.base import Tool, ToolLevel, ToolMetadata


class HTTPFetchInput(BaseModel):
    url: str = Field(..., description="Fully-qualified https:// URL.")
    method: str = Field("GET", description="GET, POST, PUT, DELETE, PATCH.")
    headers: dict[str, str] | None = Field(default=None)
    body: dict | None = Field(default=None, description="JSON body (for POST/PUT/PATCH).")
    timeout_s: float = Field(10.0, ge=0.5, le=30.0)

    @field_validator("url")
    @classmethod
    def must_be_https(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https":
            raise ValueError("only https:// URLs are permitted")
        if not parsed.hostname:
            raise ValueError("missing hostname")
        return value

    @field_validator("method")
    @classmethod
    def method_in_allowlist(cls, value: str) -> str:
        upper = value.upper()
        if upper not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("unsupported method")
        return upper


class HTTPFetchTool(Tool):
    meta: ClassVar[ToolMetadata] = ToolMetadata(
        name="http.fetch",
        description="Make an outbound HTTPS request to an allowlisted domain.",
        level=ToolLevel.ATOMIC,
        scopes_required=("tool:http:fetch",),
        destructive=False,  # destructiveness depends on the target, not our tool.
        cacheable=False,  # network side effects — never cache.
        timeout_ms=15_000,
        tags=("http", "network"),
    )
    input_schema: ClassVar[type[BaseModel]] = HTTPFetchInput

    def __init__(self, policy: PolicyEngine):
        super().__init__(policy)
        self.settings = get_settings()
        self._allowlist = _load_allowlist()

    def _allowed(self, tenant: str, host: str) -> bool:
        per_tenant = self._allowlist.get(tenant, []) or []
        globals_ = self._allowlist.get("*", []) or []
        for pattern in (*per_tenant, *globals_):
            if pattern == host:
                return True
            if pattern.startswith("*.") and host.endswith(pattern[1:]):
                return True
        return False

    async def run(self, tenant: str, args: HTTPFetchInput) -> dict:  # type: ignore[override]
        host = urlparse(args.url).hostname or ""
        if not self._allowed(tenant, host):
            raise PolicyError(
                "host_not_allowlisted",
                retryable=False,
                hint=f"{host!r} is not on this tenant's outbound allowlist",
                context={"host": host},
            )
        try:
            async with httpx.AsyncClient(timeout=args.timeout_s, follow_redirects=False) as client:
                resp = await client.request(
                    args.method, args.url, headers=args.headers or {}, json=args.body
                )
        except httpx.TimeoutException as exc:
            raise UpstreamError("http_timeout", retryable=True, hint=str(exc)) from exc
        except httpx.RequestError as exc:
            raise UpstreamError("http_network_error", retryable=True, hint=str(exc)) from exc

        # Cap response size to protect the agent's context window.
        body_bytes = resp.content[:64 * 1024]
        return {
            "status": resp.status_code,
            "headers": dict(resp.headers),
            "body": body_bytes.decode("utf-8", errors="replace"),
            "truncated": len(resp.content) > len(body_bytes),
        }


def _load_allowlist() -> dict[str, list[str]]:
    import pathlib
    path = pathlib.Path("config/http_allowlist.yaml")
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}
