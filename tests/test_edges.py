"""Outbound fetch safety, LLM provider fallback, token fetch, version."""

from __future__ import annotations

import socket
import tomllib
from pathlib import Path

import httpx
import pytest

from scout_mcp import __version__
from scout_mcp.agents import base
from scout_mcp.agents.mcp_client import fetch_token
from scout_mcp.auth.policy import PolicyEngine
from scout_mcp.errors.framework import PolicyError
from scout_mcp.tools.atomic import http_client


def fake_dns(ip: str):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return lambda host, port: [(family, socket.SOCK_STREAM, 6, "", (ip, 0))]


@pytest.mark.parametrize("ip", ["127.0.0.1", "::1", "169.254.169.254", "0.0.0.0",
                                "::ffff:127.0.0.1", "::ffff:169.254.169.254"])
async def test_fetch_refuses_loopback_and_metadata_addresses(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", fake_dns(ip))
    with pytest.raises(PolicyError) as exc:
        await http_client._refuse_unsafe_addresses("api.stripe.com")
    assert exc.value.code == "host_resolves_to_unsafe_address"


@pytest.mark.parametrize("ip", ["10.1.2.3", "93.184.216.34"])
async def test_fetch_allows_internal_and_public_addresses(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", fake_dns(ip))
    await http_client._refuse_unsafe_addresses("api.acme.internal")  # does not raise


async def test_fetch_connects_to_the_address_it_checked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", fake_dns("93.184.216.34"))
    seen = {}

    class Resp:
        status_code, headers, content = 200, {}, b"ok"

    class Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

        async def request(self, method, url, headers=None, json=None, extensions=None):
            seen.update(url=url, headers=headers, extensions=extensions)
            return Resp()
    monkeypatch.setattr(http_client.httpx, "AsyncClient", Client)
    tool = http_client.HTTPFetchTool(PolicyEngine(rules=[], default_deny=False))
    await tool.run("acme", http_client.HTTPFetchInput(
        url="https://api.company-status-page.internal:8443/x?y=1",
        headers={"host": "evil.example", "X-A": "1"},
    ))
    assert seen["url"] == "https://93.184.216.34:8443/x?y=1"  # the checked address, not a new lookup
    assert seen["headers"]["Host"] == "api.company-status-page.internal:8443"
    assert "host" not in seen["headers"] and seen["headers"]["X-A"] == "1"
    assert seen["extensions"] == {"sni_hostname": "api.company-status-page.internal"}


class RecordingClient:
    urls: list[str] = []

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass

    async def post(self, url, json, headers):
        RecordingClient.urls.append(url)
        raise httpx.ConnectError("down")


async def attempts(monkeypatch, fallback: str | None) -> list[str]:
    RecordingClient.urls = []
    monkeypatch.setattr(base.httpx, "AsyncClient", RecordingClient)
    monkeypatch.setenv("SCOUT_LLM_API_KEY", "k")
    monkeypatch.setenv("SCOUT_LLM_GROQ_KEY", "g")
    if fallback is None:
        monkeypatch.delenv("SCOUT_LLM_FALLBACK", raising=False)
    else:
        monkeypatch.setenv("SCOUT_LLM_FALLBACK", fallback)
    with pytest.raises(httpx.ConnectError):
        await base.LLM().complete("sys", [{"role": "user", "content": "hi"}])
    return RecordingClient.urls


async def test_llm_sends_prompts_to_one_provider_by_default(monkeypatch):
    assert len(await attempts(monkeypatch, None)) == 1


async def test_llm_fallback_tries_every_configured_provider_when_enabled(monkeypatch):
    urls = await attempts(monkeypatch, "true")
    assert len(urls) == 4 and any("groq.com" in u for u in urls)


async def test_fetch_token_uses_the_client_credentials_grant():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["form"] = dict(x.split("=") for x in request.content.decode().split("&"))
        return httpx.Response(200, json={"access_token": "tok-123"})
    token = await fetch_token("http://kc/token", "copilot-acme", "s3cret",
                              transport=httpx.MockTransport(handler))
    assert token == "tok-123"
    assert seen["form"] == {"grant_type": "client_credentials",
                            "client_id": "copilot-acme", "client_secret": "s3cret"}


def test_reported_version_matches_pyproject():
    root = Path(__file__).resolve().parent.parent
    declared = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assert __version__ == declared


def test_roles_claim_accepts_a_list_or_a_string():
    from scout_mcp.auth.oauth import _roles
    assert _roles("role:a b") == {"role:a", "role:b"}
    assert _roles(["role:a", "b"]) == {"role:a", "role:b"}
    assert _roles([]) == frozenset()


def test_clients_send_credentials_to_elasticsearch_and_qdrant(monkeypatch):
    import scout_mcp.tools.atomic.elasticsearch as es_mod
    from scout_mcp.config import ServerSettings
    from scout_mcp.auth.policy import PolicyEngine
    seen = {}

    class Fake:
        def __init__(self, url, basic_auth=None):
            seen["auth"] = basic_auth
    monkeypatch.setattr(es_mod, "AsyncElasticsearch", Fake)
    es_mod.ElasticsearchSearchTool._client = None
    tool = es_mod.ElasticsearchSearchTool(PolicyEngine(rules=[], default_deny=False))
    tool.settings = ServerSettings(_env_file=None, elasticsearch_password="pw")
    tool._es()
    assert seen["auth"] == ("elastic", "pw")
    es_mod.ElasticsearchSearchTool._client = None


def test_a_token_whose_key_is_unknown_is_rejected_not_a_server_error():
    # A token from another issuer has a key id this JWKS does not contain. PyJWT raises
    # PyJWKClientError for that, which used to escape as an HTTP 500.
    import jwt

    from scout_mcp.auth.oauth import TokenValidator
    from scout_mcp.config import ServerSettings
    from scout_mcp.errors.framework import AuthError

    validator = TokenValidator(ServerSettings(_env_file=None))

    def unknown_key(_token):
        raise jwt.PyJWKClientError("Unable to find a signing key that matches: 'abc'")
    validator._jwks.get_signing_key_from_jwt = unknown_key
    with pytest.raises(AuthError) as exc:
        validator.validate("any.token.here")
    assert exc.value.code == "invalid_token"
