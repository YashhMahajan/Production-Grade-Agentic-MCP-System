"""Fixes from the security review: each test fails without the change it names."""

from __future__ import annotations

import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import NoScriptError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from scout_mcp.auth.oauth import TokenValidator
from scout_mcp.auth.origin import OriginMiddleware
from scout_mcp.cache.manager import CacheManager
from scout_mcp.config import ServerSettings
from scout_mcp.errors.framework import AuthError, RateLimitError
from scout_mcp.ratelimit.limiter import RateLimiter
from scout_mcp.tools.atomic.elasticsearch import ElasticsearchSearchInput
from scout_mcp.tools.atomic.postgres import PostgresQueryInput
from scout_mcp.tools.atomic.vector_search import VectorSearchInput
from scout_mcp.tools.composed.hybrid_search import HybridSearchInput
from scout_mcp.tools.composed.semantic_search import SemanticSearchInput
from scout_mcp.tools.workflow.customer_context import CustomerContextTool
from tests.conftest import call

# ── Names that end up in a URL path ────────────────────────────────────────
BAD_NAMES = ["../snapshots", "a/b", "a?x=1", "x#y", "a b", "", ".security", "a,b", "x" * 200]


@pytest.mark.parametrize("name", BAD_NAMES)
def test_collection_names_cannot_steer_the_request_path(name):
    with pytest.raises(ValidationError):
        VectorSearchInput(collection=name, vector=[0.1])
    with pytest.raises(ValidationError):
        SemanticSearchInput(query="q", collection=name)
    with pytest.raises(ValidationError):
        HybridSearchInput(query="q", es_index="tickets", vector_collection=name)


@pytest.mark.parametrize("name", [n for n in BAD_NAMES if n not in ("a b", "x" * 200)] + ["_all"])
def test_index_names_cannot_reach_system_or_multiple_indices(name):
    with pytest.raises(ValidationError):
        ElasticsearchSearchInput(index=name, query={"match_all": {}})
    with pytest.raises(ValidationError):
        HybridSearchInput(query="q", es_index=name, vector_collection="support_docs")


def test_ordinary_names_still_pass():
    VectorSearchInput(collection="support_docs", vector=[0.1])
    ElasticsearchSearchInput(index="tickets-2026*", query={"match_all": {}})
    HybridSearchInput(query="q", es_index="tickets", vector_collection="support_docs")


# ── SQL that could move the tenant setting or hide what it does ────────────
BAD_SQL = [
    "SELECT set_config('app.tenant_id', 'globex', true)",
    "SELECT c.* FROM set_config('app.tenant_id', 'globex', true) s, customers c",
    "SELECT current_setting('app.tenant_id')",
    "SELECT pg_sleep(30)",
    "SELECT pg_catalog.set_config('app.tenant_id', 'globex', true)",
    'SELECT "set_config"(\'app.tenant_id\', \'globex\', true)',
    "SELECT 1 -- DROP",
    "SELECT /* x */ 1",
    "SELECT $$x$$",
    "SELECT $q$x$q$",
    "SELECT query_to_xml('select 1', true, true, '')",
    "SELECT 1\nDELETE FROM orders",
    "SELECT 1\tUPDATE orders SET status = 'x'",
]


@pytest.mark.parametrize("sql", BAD_SQL)
def test_sql_that_can_change_the_tenant_or_hide_itself_is_refused(sql):
    with pytest.raises(ValidationError):
        PostgresQueryInput(sql=sql)


@pytest.mark.parametrize("sql", [
    "SELECT id, created_at, updated_ts FROM orders WHERE customer_id = $1 ORDER BY created_at",
    "SELECT status, count(*) FROM orders GROUP BY status",
    "WITH recent AS (SELECT * FROM orders LIMIT 5) SELECT id FROM recent",
])
def test_ordinary_selects_still_pass(sql):
    PostgresQueryInput(sql=sql)


# ── A token with no tenant is not a member of some default tenant ──────────
@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return pem, key.public_key()


def _validator(public_key, **settings) -> TokenValidator:
    validator = TokenValidator(ServerSettings(_env_file=None, **settings))

    class Jwks:
        def get_signing_key_from_jwt(self, _token):
            return type("Key", (), {"key": public_key})()
    validator._jwks = Jwks()
    return validator


def _token(pem, **extra) -> str:
    now = int(time.time())
    claims = {"sub": "agent:a", "jti": "j1", "iat": now, "exp": now + 300,
              "aud": "scout-mcp", "iss": "https://auth.scout.local", "scope": "tool:postgres:read"}
    return jwt.encode({**claims, **extra}, pem, algorithm="RS256")


def test_a_token_with_a_tenant_is_accepted(keypair):
    pem, public = keypair
    principal = _validator(public).validate(_token(pem, tenant="acme"))
    assert principal.tenant == "acme"


def test_a_token_without_a_tenant_is_rejected(keypair):
    pem, public = keypair
    with pytest.raises(AuthError) as err:
        _validator(public).validate(_token(pem))
    assert err.value.code == "missing_tenant"


def test_tenancy_can_be_switched_off_for_a_single_tenant_deployment(keypair):
    pem, public = keypair
    principal = _validator(public, require_tenant=False).validate(_token(pem))
    assert principal.tenant == "default"


# ── An unknown signing key id cannot make Scout hammer the authorization server ──
def test_unknown_key_ids_trigger_one_jwks_fetch_per_cooldown(keypair):
    from jwt import PyJWKClientError

    pem, public = keypair
    fetches = []

    class Jwks:
        def get_signing_key_from_jwt(self, token):
            fetches.append(1)
            if jwt.get_unverified_header(token)["kid"] != "real":
                raise PyJWKClientError("no key")
            return type("Key", (), {"key": public})()
    validator = TokenValidator(ServerSettings(_env_file=None, auth_jwks_refetch_cooldown_s=3600))
    validator._jwks = Jwks()

    def token(kid):
        now = int(time.time())
        claims = {"sub": "a", "jti": "j", "iat": now, "exp": now + 300, "tenant": "acme",
                  "aud": "scout-mcp", "iss": "https://auth.scout.local"}
        return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": kid})

    # Cold start: nothing has verified yet, so a wrong id cannot lock out the first real one.
    with pytest.raises(AuthError):
        validator.validate(token("wrong-realm"))
    assert validator.validate(token("real")).tenant == "acme"
    assert len(fetches) == 2

    for guess in ("a", "b", "c", "d"):  # four made-up ids once a real key is known
        with pytest.raises(AuthError):
            validator.validate(token(guess))
    assert len(fetches) == 3  # only the first of them reached the authorization server

    # A key that has verified a token stays usable: no cooldown applies to it.
    assert validator.validate(token("real")).tenant == "acme"
    assert len(fetches) == 4


# ── Origin (MCP spec: 403 for an Origin that is present and not allowed) ───
async def test_a_browser_origin_is_refused_and_a_client_without_one_is_not(app_client):
    refused = await app_client.get("/healthz", headers={"Origin": "https://evil.example"})
    assert refused.status_code == 403 and refused.json() == {"error": "origin_not_allowed"}
    assert (await app_client.get("/healthz")).status_code == 200


async def test_a_listed_origin_gets_through():
    async def ok(_request):
        return PlainTextResponse("ok")
    settings = ServerSettings(_env_file=None, allowed_origins=["https://app.example/"])
    app = Starlette(routes=[Route("/", ok)],
                    middleware=[Middleware(OriginMiddleware, settings=settings)])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/", headers={"Origin": "https://APP.example"})).status_code == 200
        assert (await c.get("/", headers={"Origin": "https://other.example"})).status_code == 403
        assert (await c.get("/", headers={"Origin": "null"})).status_code == 403


# ── RFC 9728 protected resource metadata ───────────────────────────────────
async def test_protected_resource_metadata_names_the_authorization_server(app_client):
    resp = await app_client.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resource"].endswith("/mcp")
    assert body["authorization_servers"] == [app_client.server.settings.auth_issuer]
    assert "tool:postgres:read" in body["scopes_supported"]


async def test_a_401_points_the_client_at_that_metadata(app_client):
    resp = await app_client.post("/mcp", json={})
    assert resp.status_code == 401
    assert 'resource_metadata="' in resp.headers["www-authenticate"]
    assert resp.headers["www-authenticate"].count("/.well-known/oauth-protected-resource") == 1


# ── A half-failed result is not cached ─────────────────────────────────────
async def test_partial_results_are_not_cached_but_complete_ones_are(app_client, monkeypatch):
    runs = []
    outcomes = iter([
        {"customer_id": "CUST-1", "partial_errors": {"tickets": "elasticsearch_error"}},
        {"customer_id": "CUST-1"},
    ])

    async def run(self, tenant, args):
        runs.append(1)
        return next(outcomes)
    monkeypatch.setattr(CustomerContextTool, "run", run)
    args = {"customer_id": "CUST-1", "question": "where is my order"}

    await call(app_client, "customer.build_context", args)
    await call(app_client, "customer.build_context", args)  # not served from the first, bad run
    assert len(runs) == 2
    await call(app_client, "customer.build_context", args)  # the complete one was stored
    assert len(runs) == 2


# ── Redis trouble ──────────────────────────────────────────────────────────
class DownRedis:
    """Every command fails, as it does when the connection is gone."""

    async def get(self, *a, **k): raise RedisConnectionError("down")
    async def set(self, *a, **k): raise RedisConnectionError("down")
    async def evalsha(self, *a, **k): raise RedisConnectionError("down")


async def test_a_cache_that_is_down_is_a_miss_not_an_error():
    cache = CacheManager(ServerSettings(_env_file=None))
    cache._redis = DownRedis()
    assert await cache.get("k") is None
    await cache.set("k", {"v": 1}, ttl=60)  # does not raise
    assert await cache.get("k") == {"v": 1}  # kept in memory


async def test_a_rate_limiter_that_loses_redis_limits_locally():
    limiter = RateLimiter(ServerSettings(_env_file=None, rate_limit_burst=3, rate_limit_default_rpm=60))
    limiter._redis, limiter._script_sha = DownRedis(), "sha"
    for _ in range(3):
        await limiter.acquire("acme", "postgres.query")
    with pytest.raises(RateLimitError) as err:
        await limiter.acquire("acme", "postgres.query")
    assert err.value.context["retry_after_seconds"] > 0
    await limiter.acquire("globex", "postgres.query")  # another tenant has its own bucket


async def test_a_rate_limiter_reloads_its_script_after_redis_restarts():
    class Restarted:
        def __init__(self): self.loads, self.calls = 0, 0
        async def script_load(self, _src):
            self.loads += 1
            return f"sha{self.loads}"
        async def evalsha(self, sha, *a):
            self.calls += 1
            if sha == "sha0":
                raise NoScriptError("NOSCRIPT")
            return [1, 0, 5]
    redis = Restarted()
    limiter = RateLimiter(ServerSettings(_env_file=None))
    limiter._redis, limiter._script_sha = redis, "sha0"
    await limiter.acquire("acme", "postgres.query")
    assert redis.loads == 1 and limiter._script_sha == "sha1" and redis.calls == 2


async def test_an_approval_store_that_is_down_says_so_and_runs_nothing(app_client, monkeypatch):
    ran = []

    async def put(self, tenant, args):
        ran.append(1)
        return {}
    from scout_mcp.tools.atomic.s3_storage import S3PutTool
    monkeypatch.setattr(S3PutTool, "run", put)
    monkeypatch.setattr(app_client.server._redis, "set", DownRedis().set)
    is_error, payload = await call(
        app_client, "s3.put_object", {"key": "attachments/a.txt", "content": "x"}, token="writer")
    assert is_error and payload["code"] == "approvals_unavailable" and payload["retryable"]
    assert not ran
