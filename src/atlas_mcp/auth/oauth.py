"""Component 2 — OAuth 2.1 authentication for MCP.

The MCP 2025-11 spec makes OAuth 2.1 + PKCE the recommended authentication
flow for remote servers, replacing the static-API-key pattern that 53% of
deployed servers still use.

Atlas-MCP acts as an OAuth 2.1 **resource server** — it does NOT issue tokens.
A dedicated authorization server (Auth0, WorkOS AuthKit, Descope, or your own)
handles login, consent, client registration, and token issuance. Atlas only
validates access tokens that show up in the Authorization header.

This separation matters: if the MCP server issues its own tokens, the team
ends up reinventing identity. That is months of plumbing that has nothing to
do with your actual tools.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from functools import lru_cache

import httpx
import jwt
from jwt import PyJWKClient

from atlas_mcp.config import ServerSettings
from atlas_mcp.errors.framework import AuthError


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is calling — the result of successful token validation.

    An MCP principal is not a user. It is an *agent* acting with delegated
    authority from a user. The ``delegator`` attribute identifies the human
    who authorized the agent; ``subject`` identifies the agent itself.
    """

    subject: str                    # Agent identity, e.g. "agent:claude:abc123"
    delegator: str | None           # Human who authorized the agent
    tenant: str                     # Multi-tenancy scope
    scopes: frozenset[str]          # Tool-level permissions
    token_id: str                   # jti — for revocation and audit
    issued_at: int
    expires_at: int

    def has_scope(self, required: str) -> bool:
        # Scope strings follow `tool:<name>:<action>` convention, e.g. `tool:postgres:read`.
        return required in self.scopes or "tool:*:admin" in self.scopes


class TokenValidator:
    """Validates MCP access tokens against the authorization server's JWKS.

    Uses PyJWKClient's built-in cache for signing keys so we do not refetch
    on every request. Validation is the hot path — keep it fast.
    """

    def __init__(self, settings: ServerSettings):
        self.settings = settings
        self._jwks = PyJWKClient(settings.auth_jwks_url, cache_keys=True, max_cached_keys=16)

    def validate(self, bearer: str) -> Principal:
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(bearer)
            claims = jwt.decode(
                bearer,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=self.settings.auth_audience,
                issuer=self.settings.auth_issuer,
                options={"require": ["exp", "iat", "sub", "jti", "aud", "iss"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("token_expired", retryable=True, hint="refresh your token") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthError("invalid_token", retryable=False, hint=str(exc)) from exc

        # MCP-specific claims. The authorization server populates these during
        # the consent flow — they travel with the token as agent metadata.
        scopes = claims.get("scope", "")
        return Principal(
            subject=claims["sub"],
            delegator=claims.get("act", {}).get("sub"),  # RFC 8693 actor claim
            tenant=claims.get("tenant", "default"),
            scopes=frozenset(scopes.split() if isinstance(scopes, str) else scopes),
            token_id=claims["jti"],
            issued_at=claims["iat"],
            expires_at=claims["exp"],
        )


@lru_cache(maxsize=1)
def get_validator(settings: ServerSettings) -> TokenValidator:
    return TokenValidator(settings)


# ── Authorization Server Discovery (RFC 8414) ────────────────────────────
async def discover_authorization_server(issuer_url: str) -> dict:
    """Fetch the authorization server's metadata document.

    Clients use this to discover the authorization endpoint, token endpoint,
    registration endpoint, supported grant types, and supported PKCE methods.
    Atlas-MCP exposes this URL in its /.well-known/mcp-server response so
    MCP hosts can perform Dynamic Client Registration without hard-coding.
    """
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{issuer_url.rstrip('/')}/.well-known/oauth-authorization-server")
        resp.raise_for_status()
        return resp.json()


def now() -> int:
    return int(time.time())
